"""Page quality alarms: measured, idempotent, and never a rewrite of the original."""
import io
import math
import os
import tempfile
import unittest

TEMP = tempfile.TemporaryDirectory(prefix='evidenceflow-document-signals-')
os.environ['EVIDENCEFLOW_DATA_DIR'] = TEMP.name
from backend import document_quality as dq


def sloping_chars(chars=10, degrees=5.0, width=1000, height=1000):
    """Glyph boxes whose baseline really slopes, in the page's normalized space."""
    step=0.05;slope=step*math.tan(math.radians(degrees))*width/height
    return [{"start":i,"end":i+1,"text":"字","box":[i*step,(0.2+i*slope),i*step+0.012,(0.2+i*slope)+0.02]}
            for i in range(chars)]


def page(**overrides):
    base={"number":1,"width":1000,"height":1000,"rotation":0,"text":"正文内容","text_available":True,"formulas":[],"issues":[],
          "blocks":[{"status":"confirmed","text":"正文内容","chars":[]}]}
    base.update(overrides)
    return base


def codes(value):
    return [issue["code"] for issue in value["issues"] if isinstance(issue,dict)]


class DocumentSignalsTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):TEMP.cleanup()

    def test_raster_signals_measure_sharpness_and_deskew_without_raising(self):
        from PIL import Image, ImageDraw
        image=Image.new('L',(600,400),255);draw=ImageDraw.Draw(image)
        for i in range(14):draw.rectangle((60,60+i*22,540,72+i*22),fill=0)
        buffer=io.BytesIO();image.save(buffer,format='PNG')
        clean=dq.page_signals(buffer.getvalue())
        self.assertGreater(clean["sharpness"],0)
        self.assertAlmostEqual(clean["deskew_degrees"],0.0,places=1)
        skewed=io.BytesIO();image.rotate(4,resample=Image.BILINEAR,fillcolor=255).save(skewed,format='PNG')
        self.assertAlmostEqual(dq.page_signals(skewed.getvalue())["deskew_degrees"],-4.0,places=1)
        self.assertEqual({},dq.page_signals(b'not an image at all'))

    def test_text_skew_needs_real_glyph_geometry(self):
        self.assertAlmostEqual(dq.text_skew(page(blocks=[{"status":"confirmed","text":"x","chars":sloping_chars(degrees=3.0)}])),3.0,places=1)
        # An OCR-style axis-aligned line box carries no skew information at all.
        self.assertIsNone(dq.text_skew(page(blocks=[{"status":"confirmed","text":"x","location_precision":"line","box":[.1,.2,.8,.25],"chars":[]}])))

    def test_rotation_and_upside_down_are_alarmed_from_metadata(self):
        tilted=dq.inspect(page(rotation=90))
        self.assertIn("page_rotated",codes(tilted))
        self.assertNotIn("page_upside_down",codes(tilted))
        self.assertEqual("needs_review",next(c for c in tilted["quality_checks"] if c["name"]=="原件旋转")["status"])
        flipped=dq.inspect(page(rotation=180))
        self.assertIn("page_upside_down",codes(flipped))
        self.assertEqual("needs_review",next(c for c in flipped["quality_checks"] if c["name"]=="页面颠倒")["status"])
        upright=dq.inspect(page())
        self.assertNotIn("page_rotated",codes(upright))
        self.assertEqual("observed",next(c for c in upright["quality_checks"] if c["name"]=="页面颠倒")["status"])

    def test_skew_is_alarmed_from_text_geometry_and_reports_the_correction(self):
        result=dq.inspect(page(blocks=[{"status":"confirmed","text":"x","chars":sloping_chars(degrees=5.0)}]))
        self.assertIn("page_skewed",codes(result))
        issue=next(i for i in result["issues"] if i["code"]=="page_skewed")
        self.assertIn("5.0",issue["message"])
        self.assertIn("校正",issue["message"])
        self.assertEqual("needs_review",next(c for c in result["quality_checks"] if c["name"]=="页面倾斜")["status"])
        straight=dq.inspect(page(blocks=[{"status":"confirmed","text":"x","chars":sloping_chars(degrees=0.0)}]))
        self.assertNotIn("page_skewed",codes(straight))

    def test_a_catastrophic_sharpness_value_is_alarmed_with_its_threshold(self):
        poor=dq.inspect(page(quality_signals={"sharpness":100.0}))
        self.assertIn("low_image_sharpness",codes(poor))
        issue=next(i for i in poor["issues"] if i["code"]=="low_image_sharpness")
        self.assertIn("不代表内容不可用",issue["message"])
        self.assertEqual("needs_review",next(c for c in poor["quality_checks"] if c["name"]=="插图清晰度")["status"])
        fine=dq.inspect(page(quality_signals={"sharpness":3400.0}))
        self.assertNotIn("low_image_sharpness",codes(fine))
        # A page that could not be measured reports nothing rather than "healthy".
        unmeasured=dq.inspect(page())
        self.assertEqual("observed",next(c for c in unmeasured["quality_checks"] if c["name"]=="插图清晰度")["status"])
        self.assertIsNone(next(c for c in unmeasured["quality_checks"] if c["name"]=="插图清晰度")["value"])

    def test_traditional_characters_are_counted_and_stay_manual(self):
        result=dq.inspect(page(text="統計總額為100萬元"))
        self.assertIn("traditional_characters",codes(result))
        issue=next(i for i in result["issues"] if i["code"]=="traditional_characters")
        self.assertIn("6 个繁体字",issue["message"])
        self.assertIn("統",issue["message"])
        self.assertIn("对照原件复核",issue["message"])
        self.assertEqual("統計總額為100萬元",result["text"],"原文与偏移不得被自动改写")

    def test_inspecting_an_already_inspected_page_does_not_duplicate_alarms(self):
        once=dq.inspect(page(rotation=180,text="統計總額"))
        twice=dq.inspect(dict(once))
        self.assertEqual(codes(once),codes(twice))
        self.assertEqual(sorted(codes(once)),sorted(set(codes(once))))

    def test_blur_is_judged_against_the_document_not_a_fixed_number(self):
        def measured(number,sharpness):
            return page(number=number,quality_signals={"sharpness":sharpness})
        result=dq.assess([measured(1,5000.0),measured(2,5000.0),measured(3,100.0)],outline=[{"title":"x","level":1}])
        self.assertEqual([3],result["blurry_pages"])
        self.assertEqual(3,result["sharpness_measured_pages"])
        self.assertTrue(any("清晰度明显低于" in note for note in result["notes"]))
        # Fewer than three measured pages cannot establish the document's baseline.
        short=dq.assess([measured(1,5000.0),measured(2,100.0)],outline=[{"title":"x","level":1}])
        self.assertEqual([],short["blurry_pages"])
        self.assertEqual([],short["notes"])


if __name__=='__main__':
    unittest.main()
