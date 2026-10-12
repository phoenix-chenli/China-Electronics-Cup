"""Document complexity: measurable factors only, bounded, explainable, no semantics."""
import os
import tempfile
import unittest

TEMP = tempfile.TemporaryDirectory(prefix='evidenceflow-document-complexity-')
os.environ['EVIDENCEFLOW_DATA_DIR'] = TEMP.name
from backend import document_quality as dq, store as s


def page(number=1, text=True, blocks=None, images=None, tables=None, issues=None, headings=None, rotation=0):
    return {"number":number,"text_available":text,"rotation":rotation,
            "blocks":blocks if blocks is not None else ([{"status":"confirmed","text":"正文内容"}] if text else []),
            "images":images or [],"tables":tables or [],"issues":issues or [],"headings":headings or []}


class DocumentComplexityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):s.init()
    @classmethod
    def tearDownClass(cls):TEMP.cleanup()

    def test_weights_sum_to_one_hundred_and_the_score_is_their_weighted_sum(self):
        result=dq.assess([page()])
        self.assertEqual(100,sum(factor["weight"] for factor in result["factors"]))
        self.assertEqual(round(sum(factor["contribution"] for factor in result["factors"])),result["score"])
        self.assertTrue(0<=result["score"]<=100)
        for factor in result["factors"]:
            self.assertTrue(0.0<=factor["severity"]<=1.0,factor)
            self.assertTrue(factor["detail"])
        self.assertIn("权重",result["rules"])
        self.assertIn("不预测问答准确率",result["boundary"])

    def test_pages_without_text_drive_the_local_recognition_suggestion(self):
        result=dq.assess([page(number=1,text=False),page(number=2,text=False)])
        self.assertEqual(40,result["score"])
        self.assertEqual("中",result["level"])
        self.assertEqual("ocr",result["suggested_mode"])
        scanned=next(f for f in result["factors"] if f["name"]=="无文字层页面")
        self.assertEqual(1.0,scanned["severity"])
        self.assertEqual(30.0,scanned["contribution"])
        self.assertTrue(any("没有可用文字" in note for note in result["notes"]))

    def test_a_clean_text_document_stays_low_and_keeps_the_native_mode(self):
        result=dq.assess([page()],outline=[{"title":"一、综合","level":1,"page":1}])
        self.assertLess(result["score"],25)
        self.assertEqual("低",result["level"])
        self.assertEqual("low",result["grade"])
        self.assertEqual("native",result["suggested_mode"])
        self.assertEqual([],result["notes"])
        self.assertEqual(0.0,next(f for f in result["factors"] if f["name"]=="缺少目录层级")["contribution"])

    def test_rotation_and_a_damaged_text_layer_both_contribute(self):
        damaged=[{"code":"damaged_text_layer","message":"含替换字符"}]
        result=dq.assess([page(rotation=90,issues=damaged),page(number=2)],outline=[{"title":"x","level":1}])
        rotated=next(f for f in result["factors"] if f["name"]=="页面旋转")
        broken=next(f for f in result["factors"] if f["name"]=="文字层损坏")
        self.assertEqual(0.5,rotated["severity"])
        self.assertEqual(5.0,rotated["contribution"])
        self.assertEqual(0.5,broken["severity"])
        self.assertEqual(5.0,broken["contribution"])
        self.assertEqual(1,rotated["detail"].count("页带有旋转"))

    def test_a_missing_outline_is_reported_without_inventing_structure(self):
        result=dq.assess([page()])
        missing=next(f for f in result["factors"] if f["name"]=="缺少目录层级")
        self.assertEqual(10.0,missing["contribution"])
        self.assertEqual("目录条目 0 条",missing["detail"])
        self.assertTrue(any("没有识别到目录层级" in note for note in result["notes"]))
        # The assessment reports what it measured; it never fabricates an outline.
        self.assertNotIn("headings",result)
        self.assertNotIn("outline",result)

    def test_table_and_image_heaviness_asks_for_a_previewed_visual_step(self):
        heavy=[page(images=[{"box":[0,0,1,1]}],tables=[{"id":"t1","box":[0,0,1,.4]}]) for _ in range(2)]
        result=dq.assess(heavy,outline=[{"title":"x","level":1}])
        self.assertTrue(any("视觉服务" in note for note in result["notes"]))
        self.assertEqual("native",result["suggested_mode"])

    def test_quality_for_parse_carries_complexity_only_when_pages_exist(self):
        body={"pages":[page()],"outline":[],"parser":"fixture","structure":"按页"}
        quality=s.quality_for_parse(body)
        self.assertIn("complexity",quality)
        self.assertEqual("低",quality["complexity"]["level"])
        self.assertEqual(quality["complexity"],s.quality_for_parse(body)["complexity"])
        failed=s.quality_for_parse({"pages":[]},errors=["解析失败"])
        self.assertNotIn("complexity",failed)
        self.assertIn("解析失败",failed["quality_flags"])


if __name__=='__main__':
    unittest.main()
