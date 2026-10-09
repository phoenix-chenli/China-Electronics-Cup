"""OCR skeleton: model-pack verification, capability states, and page wiring.

Recognition needs vetted weights that are not bundled here, so these tests cover
everything deterministic: manifest validation, SHA-256 enforcement before
installation, the honest "not installed" states, the connected-component and CTC
post-processing, and the document wiring that turns recognized lines into a
version-pinned parse with boxes. The recognition call itself is stubbed; no test
claims that real OCR accuracy was measured.
"""
import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory(prefix="evidenceflow-ocr-")
os.environ["EVIDENCEFLOW_DATA_DIR"] = TEMP.name
os.environ["EVIDENCEFLOW_OCR_MODEL_DIR"] = str(Path(TEMP.name) / "models" / "ocr")
os.environ["EVIDENCEFLOW_OCR_MANIFEST"] = str(Path(TEMP.name) / "OCR模型包.manifest.json")
os.environ["EVIDENCEFLOW_OCR_MODEL_PACK"] = str(Path(TEMP.name) / "OCR模型包.zip")

import numpy as np
from PIL import Image

from backend import documents, ocr, document_providers as providers, document_tasks as tasks, store as s

DETECTION_BYTES = b"fixture-detection-weights"
RECOGNITION_BYTES = b"fixture-recognition-weights"
DICTIONARY_TEXT = "a\nb\nc\n"


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def build_pack(detection=DETECTION_BYTES, recognition=RECOGNITION_BYTES, dictionary=DICTIONARY_TEXT):
    """Assemble a verifiable fixture pack; returns (pack_bytes, manifest)."""
    files = [
        {"path": "det.onnx", "sha256": digest(detection), "role": "detection"},
        {"path": "rec.onnx", "sha256": digest(recognition), "role": "recognition"},
        {"path": "dict.txt", "sha256": digest(dictionary.encode()), "role": "dictionary"},
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("det.onnx", detection)
        archive.writestr("rec.onnx", recognition)
        archive.writestr("dict.txt", dictionary)
    blob = buffer.getvalue()
    manifest = {"adapter": "dbnet_ctc", "license": "Apache-2.0 (fixture)", "pack_sha256": digest(blob), "files": files}
    return blob, manifest


def write_pack(blob, manifest):
    Path(ocr.pack_path()).write_bytes(blob)
    Path(ocr.manifest_path()).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


def clear_install():
    target = Path(ocr.model_dir())
    if target.exists():
        import shutil
        shutil.rmtree(target)


STUB_RESULT = {
    "provider": ocr.PROVIDER_VERSION, "adapter": "dbnet_ctc", "pack_sha256": "f" * 64,
    "lines": [
        {"text": "免费维修期为 24 个月", "score": 0.93, "box": [0.10, 0.20, 0.60, 0.25]},
        {"text": "电池上限 320 元", "score": 0.55, "box": [0.10, 0.30, 0.45, 0.35]},
    ],
    "line_count": 2, "low_confidence": 1, "mean_score": 0.74, "verified": False,
}


class OcrSkeletonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s.init()
    @classmethod
    def tearDownClass(cls):
        TEMP.cleanup()

    def setUp(self):
        clear_install()
        for path in (ocr.pack_path(), ocr.manifest_path()):
            if Path(path).exists():
                Path(path).unlink()

    # --- capability states -------------------------------------------------
    def test_without_a_manifest_ocr_is_not_ready_and_says_why(self):
        state = ocr.capabilities()
        self.assertFalse(state["ready"])
        self.assertIn("清单", state["reason"])
        self.assertIsNone(ocr.recognize_page(b"\x89PNG\r\n\x1a\n"))

    def test_pack_without_manifest_is_refused(self):
        blob, _ = build_pack()
        Path(ocr.pack_path()).write_bytes(blob)
        self.assertFalse(ocr.capabilities()["ready"])
        with self.assertRaises(ValueError) as raised:
            ocr.install()
        self.assertIn("清单", str(raised.exception))

    def test_manifest_validation_rejects_unsafe_entries(self):
        blob, manifest = build_pack()
        cases = {
            "adapter": {**manifest, "adapter": "unknown_engine"},
            "license": {**manifest, "license": ""},
            "roles": {**manifest, "files": [f for f in manifest["files"] if f["role"] != "recognition"]},
            "filename": {**manifest, "files": manifest["files"] + [{"path": "../escape.onnx", "sha256": "a" * 64, "role": "detection"}]},
            "sha": {**manifest, "files": manifest["files"] + [{"path": "extra.onnx", "sha256": "zz", "role": "detection"}]},
        }
        Path(ocr.pack_path()).write_bytes(blob)
        for label, broken in cases.items():
            with self.subTest(case=label):
                write_pack(blob, broken)
                with self.assertRaises(ValueError):
                    ocr.install()
                self.assertFalse(ocr.capabilities()["ready"])

    def test_tampered_pack_is_refused_before_extraction(self):
        blob, manifest = build_pack()
        write_pack(blob + b"tampered", manifest)
        with self.assertRaises(ValueError) as raised:
            ocr.install()
        self.assertIn("摘要", str(raised.exception))
        self.assertFalse(Path(ocr.model_dir()).exists())

    def test_tampered_member_inside_a_valid_pack_is_refused(self):
        blob, manifest = build_pack()
        # Rewrite one member but keep the manifest hash of the original archive.
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("det.onnx", DETECTION_BYTES)
            archive.writestr("rec.onnx", b"swapped-weights")
            archive.writestr("dict.txt", DICTIONARY_TEXT)
        swapped = buffer.getvalue()
        manifest = {**manifest, "pack_sha256": digest(swapped)}
        write_pack(swapped, manifest)
        with self.assertRaises(ValueError) as raised:
            ocr.install()
        self.assertIn("摘要不一致", str(raised.exception))

    def test_verified_pack_installs_and_reports_its_licence(self):
        blob, manifest = build_pack()
        write_pack(blob, manifest)
        state = ocr.install()
        self.assertTrue(state["ready"])
        self.assertEqual(state["adapter"], "dbnet_ctc")
        self.assertEqual(state["pack_sha256"], digest(blob))
        self.assertIn("fixture", state["license"])
        self.assertEqual(sum(1 for m in state["models"] if m["role"] == "detection"), 1)
        self.assertTrue((Path(ocr.model_dir()) / "rec.onnx").is_file())
        removed = ocr.uninstall()
        self.assertFalse(removed["ready"])

    # --- recognition post-processing ---------------------------------------
    def test_connected_components_separate_distinct_text_blocks(self):
        mask = np.zeros((40, 80), dtype=bool)
        mask[5:15, 4:20] = True
        mask[25:35, 40:70] = True
        boxes, _ = ocr._boxes(mask)
        self.assertEqual(len(boxes), 2)
        self.assertEqual(boxes[0][:2], (4, 5))
        self.assertEqual(boxes[1][:2], (40, 25))

    def test_small_specks_are_dropped_as_noise(self):
        mask = np.zeros((40, 80), dtype=bool)
        mask[5:15, 4:20] = True
        mask[30, 60] = True
        boxes, _ = ocr._boxes(mask)
        self.assertEqual(len(boxes), 1)

    def test_ctc_decode_collapses_repeats_and_drops_blanks(self):
        dictionary = ["a", "b", "c"]
        tokens = [1, 1, 0, 2, 3, 3]          # blank is index 0
        logits = np.zeros((len(tokens), len(dictionary) + 1), dtype=np.float32)
        for row, token in enumerate(tokens):
            logits[row, token] = 0.9
        text, score = ocr._decode(logits, dictionary)
        self.assertEqual(text, "abc")
        self.assertGreater(score, 0.8)

    def test_ctc_decode_skips_tokens_outside_the_dictionary(self):
        dictionary = ["a"]
        logits = np.zeros((3, 2), dtype=np.float32)
        logits[:, 1] = 0.9
        text, _ = ocr._decode(logits, dictionary)
        self.assertEqual(text, "a")

    def test_recognize_page_without_installation_returns_nothing(self):
        self.assertIsNone(ocr.recognize_page(b"\x89PNG\r\n\x1a\n"))

    # --- wiring into the document pipeline ---------------------------------
    def stub(self):
        return patch.object(ocr, "capabilities", return_value={"ready": True, "adapter": "dbnet_ctc"}), \
               patch.object(ocr, "recognize", return_value=dict(STUB_RESULT))

    def test_recognized_page_becomes_offsets_and_line_boxes(self):
        caps, recognize = self.stub()
        with caps, recognize:
            page = ocr.recognize_page(b"\x89PNG\r\n\x1a\n")
        self.assertIsNotNone(page)
        self.assertIn("免费维修期为 24 个月", page["text"])
        for block in page["blocks"]:
            self.assertEqual(page["text"][block["start"]:block["end"]], block["text"])
            self.assertEqual(block["chars"], [])
            self.assertEqual(block['location_precision'],'line')
        self.assertEqual(page["info"]["box_precision"], "line")
        self.assertFalse(page["info"]["verified"])
        self.assertEqual(page["low_confidence"], 1)

    def write_png(self, name="scan.png"):
        path = s.UPLOADS / name
        buffer = io.BytesIO()
        Image.new("RGB", (600, 200), "#eef2f8").save(buffer, format="PNG")
        path.write_bytes(buffer.getvalue())
        return path

    def formal_recognize(self, document):
        identity={'ready':True,'local':True,'fingerprint':'upstream-ocr-protocol-fixture','model':'protocol-fixture'}
        def recognize(raw,**kwargs):
            return {'lines':[{'text':line['text'],'box':line['box'],'confidence':line['score'],
                             'status':'confirmed' if line['score']>=.8 else 'pending'} for line in STUB_RESULT['lines']],
                    'tables':[],'issues':[],'model':identity['model'],'fingerprint':identity['fingerprint'],
                    'input_image_sha256':digest(raw),'duration_ms':1,'provenance':{'method':'ocr','location_precision':'line'}}
        manager=tasks.Manager(workers=1)
        try:
            with patch.object(providers,'describe',return_value=identity),patch.object(providers,'ocr',side_effect=recognize),patch('backend.rag_index.submit_index',return_value={'id':'protocol-index'}):
                result=manager.submit(tasks.ParseRequest(document_id=document['id'],version=document['sha256'],pages=[1],mode='ocr'),synchronous=True)
                self.assertEqual(result['status'],'completed',result)
                return result
        finally:manager.pool.shutdown(wait=True)

    def test_scanned_image_is_indexed_with_text_and_flagged_for_review(self):
        path = self.write_png("review.png")
        caps, recognize = self.stub()
        with caps, recognize as called:
            document = s.index_document(path)
            called.assert_not_called()  # import preserves raw source; OCR is an explicit task
        self.formal_recognize(document)
        document=s.docrow(s.doc_get(document['id']))
        self.assertEqual(document["status"], "needs_review")
        self.assertIn("结构或识别待复核", document["quality"]["quality_flags"])
        self.assertNotIn("需要文字识别", document["quality"]["quality_flags"])
        self.assertEqual(document['quality']['pending_text_lines'],1)
        blocks = s.blocks(document["id"])
        self.assertTrue(blocks)
        self.assertTrue(all(block["quality"].get("location_precision")=="line" for block in blocks))
        self.assertTrue(any("免费维修期" in block["text"] for block in blocks))
        self.assertFalse(any('320' in block['text'] for block in blocks))

    def test_image_without_ocr_stays_pending_recognition(self):
        path = self.write_png("pending.png")
        document = s.index_document(path)
        self.assertIn("需要文字识别", document["quality"]["quality_flags"])
        self.assertEqual(document["status"], "needs_review")
        self.assertIsNone(document["quality"].get("ocr"))
        self.assertEqual(s.blocks(document["id"])[0]["content_type"], "image")

    def test_recognized_page_is_locatable_by_text_range(self):
        path = self.write_png("locate.png")
        quote = "免费维修期为 24 个月"
        caps, recognize = self.stub()
        with caps, recognize:
            document = s.index_document(path)
        self.formal_recognize(document)
        bound = documents.resolve({"document_id": document["id"], "sha256": document["sha256"], "page": 1}, quote)
        self.assertEqual(bound["status"], "located")
        self.assertEqual(bound["quote"], quote)
        self.assertEqual(bound["locator"]["location_precision"], "line")
        self.assertTrue(bound["locator"]["rects"])
        self.assertEqual(documents.resolve({'document_id':document['id'],'page':1},'电池上限 320 元')['status'],'unlocated')

    def test_reparse_reports_the_recognition_change(self):
        path = self.write_png("reparse.png")
        document = s.index_document(path)
        before = documents.layout(document["id"], document["sha256"])
        record=self.formal_recognize(document)
        self.assertNotEqual(record['result']['parse_id'],before['parse_id'])
        self.assertEqual(record['events'][0]['method'],'ocr')
        self.assertIsInstance(record['events'][0]['duration_ms'],(int,float))
        self.assertEqual(documents.layout(document['id'],parse_id=before['parse_id'])['pages'][0]['text'],'')


if __name__ == "__main__":
    unittest.main()
