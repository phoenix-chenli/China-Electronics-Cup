"""Formal recognition publication, provenance and late-result protection in isolated data."""
import copy
import hashlib
import io
import json
import threading
import time
import unittest
import uuid
import zipfile
from unittest.mock import patch

from PIL import Image
from tests.test_conversations import TEMP, s, conv
from backend import document_tasks as tasks, documents, document_providers as providers


class DocumentTasksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s.init()
        from backend import settings, connections
        settings.initialize();connections.initialize()
        tasks.initialize()

    @classmethod
    def tearDownClass(cls):
        tasks.manager.pool.shutdown(wait=True)
        conv.manager.pool.shutdown(wait=True)
        TEMP.cleanup()

    def setUp(self):
        self.manager=tasks.Manager(workers=1)
        self.provider={"ready":True,"local":True,"fingerprint":"fixture-ocr-v1","model":"protocol-fixture","provider_id":"fixture"}
        self.desc=patch.object(providers,"describe",side_effect=lambda:copy.deepcopy(self.provider));self.desc.start()
        self.index=patch("backend.rag_index.submit_index",return_value={"id":"fixture-index"});self.index.start()

    def tearDown(self):
        self.manager.pool.shutdown(wait=True)
        self.desc.stop();self.index.stop()

    def source(self,text="# 标题\n\n收入：100万元。\n",fmt="md"):
        path=s.UPLOADS/(uuid.uuid4().hex+"."+fmt)
        if fmt=="png":
            image=Image.new("RGB",(400,200),"white");buf=io.BytesIO();image.save(buf,format="PNG");path.write_bytes(buf.getvalue())
        else: path.write_text(text,encoding="utf-8")
        return s.index_document(path),path

    def request(self,doc,**extra):
        return tasks.ParseRequest(document_id=doc["id"],version=doc["sha256"],pages=[1],**extra)

    def recognition(self,raw,**kwargs):
        return {"lines":[{"text":"收入：100万元。","box":[.1,.2,.8,.4],"polygon":[[.1,.2],[.8,.2],[.8,.4],[.1,.4]],"confidence":.99,"status":"confirmed"}],"tables":[],
                "issues":[],"method":"local_ocr","fingerprint":self.provider["fingerprint"],"model":"protocol-fixture","rotation":0,
                "input_image_sha256":hashlib.sha256(raw).hexdigest(),"duration_ms":1,"provenance":{"method":"local_ocr"}}

    def visual_connection(self,local=True):
        cid=uuid.uuid4().hex
        config={"id":cid,"name":"视觉协议夹具","kind":"vision","revision":1,"model":"fixture","endpoint":"http://127.0.0.1:9/v1" if local else "https://example.com/v1","enabled":True,"last_test":{"ok":True},"timeout":3}
        with s.conn() as db: db.execute("INSERT INTO ef_connections VALUES (?,?,NULL)",(cid,json.dumps(config)))
        return cid

    def docx_image(self):
        from backend import document_native as native
        image=Image.new("RGB",(400,200),"white");raw=io.BytesIO();image.save(raw,format="PNG")
        path=s.UPLOADS/(uuid.uuid4().hex+".docx")
        with zipfile.ZipFile(path,"w") as archive:
            archive.writestr("word/document.xml",'<w:document xmlns:w="'+native.NS['w']+'" xmlns:a="'+native.NS['a']+'" xmlns:r="'+native.NS['r']+'"><w:body><w:p><w:r><w:t>原生说明应保留。</w:t><w:drawing><a:blip r:embed="img"/></w:drawing></w:r></w:p></w:body></w:document>')
            archive.writestr("word/media/image.png",raw.getvalue())
            archive.writestr("word/_rels/document.xml.rels",'<Relationships><Relationship Id="img" Target="media/image.png"/></Relationships>')
        return s.index_document(path),raw.getvalue()

    def test_native_publication_keeps_old_parse_and_updates_catalog(self):
        doc,_=self.source();before=documents.layout(doc["id"])
        result=self.manager.submit(self.request(doc,mode="native"),synchronous=True)
        self.assertEqual("completed",result["status"],result)
        after=result["result"]["parse_id"]
        self.assertNotEqual(before["parse_id"],after)
        self.assertEqual(after,documents.current_parse_id(doc["id"],doc["sha256"]))
        self.assertEqual(before["pages"],documents.layout(doc["id"],parse_id=before["parse_id"])["pages"])
        self.assertEqual("document-processing-v1",documents.view(doc["id"])["parser"])
        catalog=next(d for d in s.doc_list() if d["id"]==doc["id"])
        self.assertEqual(3,catalog["quality"]["parser_version"])
        self.assertEqual("fixture-index",result["index_job_id"])
        self.assertIsNotNone(result["events"][0]["duration_ms"])

    def test_ocr_saved_ranges_retain_line_precision_and_real_bbox(self):
        doc,_=self.source(fmt="png");before=documents.layout(doc["id"])
        with patch.object(providers,"ocr",side_effect=self.recognition): result=self.manager.submit(self.request(doc,mode="ocr"),synchronous=True)
        self.assertEqual("completed",result["status"],result)
        located=documents.resolve({"document_id":doc["id"],"sha256":doc["sha256"],"parse_id":result["result"]["parse_id"],"page":1},"收入：100万元。")
        self.assertEqual("located",located["status"])
        self.assertEqual([[.1,.2,.8,.4]],located["locator"]["rects"])
        self.assertNotEqual("character",located["locator"]["location_precision"])
        self.assertFalse(documents.get_page(doc["id"],1,parse_id=before["parse_id"])["text_available"])
        self.assertIn("收入：100万元。",s.blocks(doc["id"])[0]["text"])

    def test_docx_embedded_image_ocr_is_frozen_and_located_in_media_space(self):
        doc,raw=self.docx_image();req=self.request(doc,mode="ocr")
        before=documents.get_page(doc["id"],1);preview=tasks.preview(req)
        self.assertEqual(1,preview["image_count"]);media=preview["images"][0]
        self.assertEqual(hashlib.sha256(raw).hexdigest(),media["image_sha256"])
        self.assertEqual(before["images"][0]["id"],media["media_id"])
        with patch.object(providers,"ocr",side_effect=self.recognition): result=self.manager.submit(req,synchronous=True)
        self.assertEqual("completed",result["status"],result)
        after=documents.get_page(doc["id"],1);self.assertIn("原生说明应保留",after["text"])
        located=documents.resolve({"document_id":doc["id"],"sha256":doc["sha256"],"page":1},"收入：100万元。")
        self.assertEqual("located",located["status"])
        self.assertEqual(media["media_id"],located["locator"]["media_id"])
        self.assertEqual("media",located["locator"]["coordinate_space"])
        self.assertEqual([[.1,.2,.8,.4]],located["locator"]["rects"])
        self.assertEqual(1,documents.view(doc["id"])["image_count"])
        def replacement(raw,**kwargs):
            result=self.recognition(raw,**kwargs);result["lines"][0]["text"]="收入：120万元。";return result
        with patch.object(providers,"ocr",side_effect=replacement): repeated=self.manager.submit(req,synchronous=True)
        self.assertEqual("completed",repeated["status"],repeated)
        current=documents.get_page(doc["id"],1)
        self.assertIn("原生说明应保留",current["text"]);self.assertNotIn("100万元",current["text"]);self.assertEqual(1,current["text"].count("120万元"))
        for block in current["blocks"]:self.assertEqual(block["text"],current["text"][block["start"]:block["end"]])

    def test_visual_scan_runs_real_observation_stage_and_saves_ocr_before_structure(self):
        doc,_=self.source(fmt="png");cid=self.visual_connection();req=self.request(doc,mode="vision",connection_id=cid);preview=tasks.preview(req)
        def visual(raw,**kwargs):
            self.assertTrue(any(line["text"]=="收入：100万元。" and line.get("confidence")==.99 for line in kwargs["observed_lines"]))
            return {"blocks":[],"tables":[],"charts":[],"issues":[],"raw_response":"{}"}
        with patch.object(providers,"ocr",side_effect=self.recognition) as ocr,patch.object(providers,"visual",side_effect=visual) as vision:
            result=self.manager.submit(tasks.ParseRequest(**(req.model_dump()|{"preview_id":preview["preview_id"],"accepted":True})),synchronous=True)
        self.assertEqual("completed",result["status"],result);ocr.assert_called_once();vision.assert_called_once()
        page=documents.get_page(doc["id"],1)
        self.assertIn("收入：100万元。",page["text"]);self.assertEqual(self.provider["fingerprint"],page["ocr_provenance"]["fingerprint"])
        observation=next(event for event in result["events"] if event["method"]=="ocr_observation")
        self.assertEqual("completed",observation["status"]);self.assertIsNotNone(observation["duration_ms"])

    def test_visual_ocr_failure_stops_before_external_structure_request(self):
        doc,_=self.source(fmt="png");cid=self.visual_connection();req=self.request(doc,mode="vision",connection_id=cid);preview=tasks.preview(req)
        before=documents.current_parse_id(doc["id"],doc["sha256"])
        with patch.object(providers,"ocr",side_effect=ValueError("OCR 运行失败")),patch.object(providers,"visual") as visual:
            result=self.manager.submit(tasks.ParseRequest(**(req.model_dump()|{"preview_id":preview["preview_id"],"accepted":True})),synchronous=True)
        self.assert_no_publication(doc,before,result);visual.assert_not_called()
        observation=next(event for event in result["events"] if event["method"]=="ocr_observation")
        self.assertEqual("failed",observation["status"]);self.assertIsNotNone(observation["duration_ms"])

    def test_visual_without_installed_ocr_records_unavailable_observations(self):
        self.provider["ready"]=False
        doc,_=self.source(fmt="png");cid=self.visual_connection();req=self.request(doc,mode="vision",connection_id=cid);preview=tasks.preview(req)
        with patch.object(providers,"ocr") as ocr,patch.object(providers,"visual",return_value={"blocks":[],"tables":[],"charts":[],"issues":[],"raw_response":"{}"}):
            result=self.manager.submit(tasks.ParseRequest(**(req.model_dump()|{"preview_id":preview["preview_id"],"accepted":True})),synchronous=True)
        self.assertEqual("completed",result["status"],result);ocr.assert_not_called()
        self.assertFalse(documents.get_page(doc["id"],1)["text_available"])
        observation=next(event for event in result["events"] if event["method"]=="ocr_observation")
        self.assertEqual("not_available",observation["status"]);self.assertIsNone(observation["duration_ms"])

    def test_privacy_changed_after_local_observation_sends_no_external_image(self):
        with s.conn() as db:
            row=db.execute("SELECT body FROM ef_settings WHERE key='privacy'").fetchone();old=row[0] if row else None
            db.execute("INSERT OR REPLACE INTO ef_settings VALUES ('privacy',?)",(json.dumps({"local_only":False}),))
        try:
            doc,_=self.source(fmt="png");cid=self.visual_connection(local=False);req=self.request(doc,mode="vision",connection_id=cid);preview=tasks.preview(req)
            before=documents.current_parse_id(doc["id"],doc["sha256"])
            def changed(raw,**kwargs):
                with s.conn() as db:db.execute("UPDATE ef_settings SET body=? WHERE key='privacy'",(json.dumps({"local_only":True}),))
                return self.recognition(raw,**kwargs)
            with patch.object(providers,"ocr",side_effect=changed),patch.object(providers,"visual") as visual:
                result=self.manager.submit(tasks.ParseRequest(**(req.model_dump()|{"preview_id":preview["preview_id"],"accepted":True})),synchronous=True)
            self.assert_no_publication(doc,before,result);visual.assert_not_called()
        finally:
            with s.conn() as db:
                if old is None:db.execute("DELETE FROM ef_settings WHERE key='privacy'")
                else:db.execute("UPDATE ef_settings SET body=? WHERE key='privacy'",(old,))

    def assert_no_publication(self,doc,before,result):
        self.assertIn(result["status"],{"failed","cancelled"})
        self.assertIsNone(result["result"])
        self.assertEqual(before,documents.current_parse_id(doc["id"],doc["sha256"]))

    def test_cancelled_late_ocr_never_activates_parse(self):
        doc,_=self.source(fmt="png");before=documents.current_parse_id(doc["id"],doc["sha256"])
        def cancelled(raw,**kwargs): kwargs["cancelled"].set();return self.recognition(raw,**kwargs)
        with patch.object(providers,"ocr",side_effect=cancelled): result=self.manager.submit(self.request(doc,mode="ocr"),synchronous=True)
        self.assert_no_publication(doc,before,result);self.assertEqual("cancelled",result["status"])

    def test_durable_cancel_wins_against_worker_and_can_retry(self):
        doc,_=self.source(fmt="png");before=documents.current_parse_id(doc["id"],doc["sha256"])
        entered=threading.Event();release=threading.Event()
        def delayed(raw,**kwargs):
            entered.set();release.wait(2);return self.recognition(raw,**kwargs)
        with patch.object(providers,"ocr",side_effect=delayed):
            submitted=self.manager.submit(self.request(doc,mode="ocr"))
            self.assertTrue(entered.wait(2))
            cancelled=self.manager.cancel(submitted["id"]);release.set()
            self.manager.pool.shutdown(wait=True)
        result=self.manager.get(cancelled["id"])
        self.assert_no_publication(doc,before,result);self.assertEqual("cancelled",result["status"])
        self.assertIsNotNone(result["duration_ms"])
        replacement=tasks.Manager(workers=1)
        try:
            with patch.object(providers,"ocr",side_effect=self.recognition):
                retried=replacement.retry(result["id"],tasks.RetryRequest())
                deadline=time.monotonic()+3
                while replacement.get(retried["id"])["status"] in tasks.ACTIVE and time.monotonic()<deadline: time.sleep(.01)
            self.assertEqual("completed",replacement.get(retried["id"])["status"])
            self.assertEqual(result["id"],replacement.get(retried["id"])["attempt_of"])
        finally: replacement.pool.shutdown(wait=True)

    def test_total_deadline_prevents_late_publication(self):
        doc,_=self.source(fmt="png");before=documents.current_parse_id(doc["id"],doc["sha256"])
        def delayed(raw,**kwargs): time.sleep(.02);return self.recognition(raw,**kwargs)
        with patch.object(tasks,"MAX_SECONDS",.01),patch.object(providers,"ocr",side_effect=delayed): result=self.manager.submit(self.request(doc,mode="ocr"),synchronous=True)
        self.assert_no_publication(doc,before,result)

    def test_archived_during_ocr_never_activates_parse(self):
        doc,_=self.source(fmt="png");before=documents.current_parse_id(doc["id"],doc["sha256"])
        def archived(raw,**kwargs):
            with s.conn() as db: db.execute("UPDATE documents SET status='archived' WHERE id=?",(doc["id"],))
            return self.recognition(raw,**kwargs)
        with patch.object(providers,"ocr",side_effect=archived): result=self.manager.submit(self.request(doc,mode="ocr"),synchronous=True)
        self.assert_no_publication(doc,before,result)

    def test_model_changed_during_ocr_never_activates_parse(self):
        doc,_=self.source(fmt="png");before=documents.current_parse_id(doc["id"],doc["sha256"])
        def changed(raw,**kwargs): self.provider["fingerprint"]="fixture-ocr-v2";return self.recognition(raw,**kwargs)
        with patch.object(providers,"ocr",side_effect=changed): result=self.manager.submit(self.request(doc,mode="ocr"),synchronous=True)
        self.assert_no_publication(doc,before,result)

    def test_base_parse_changed_during_ocr_preserves_newer_parse(self):
        doc,_=self.source(fmt="png");base=documents.layout(doc["id"]);replacement=[]
        def changed(raw,**kwargs):
            replacement.append(documents._save_parse(doc["id"],doc["sha256"],{k:copy.deepcopy(base[k]) for k in ("pages","outline","structure","parser")},"并行重解析")["parse_id"])
            return self.recognition(raw,**kwargs)
        with patch.object(providers,"ocr",side_effect=changed): result=self.manager.submit(self.request(doc,mode="ocr"),synchronous=True)
        self.assertEqual("failed",result["status"])
        self.assertEqual(replacement[0],documents.current_parse_id(doc["id"],doc["sha256"]))

    def test_native_original_replaced_during_work_does_not_publish_old_sha(self):
        doc,path=self.source();before=documents.layout(doc["id"])
        original=documents.parse_page
        def changed_once(source,number):
            data=original(source,number);path.write_text("新版：200万元。",encoding="utf-8")
            with patch.object(documents,"parse_page",side_effect=original): s.index_document(path)
            return data
        with patch.object(documents,"parse_page",side_effect=changed_once): result=self.manager.submit(self.request(doc,mode="native"),synchronous=True)
        self.assertEqual("failed",result["status"])
        self.assertEqual(before["parse_id"],documents.current_parse_id(doc["id"],doc["sha256"]))
        self.assertNotEqual(doc["sha256"],s.doc_get(doc["id"])["sha256"])

    def test_preview_requires_acceptance_and_is_consumed_once(self):
        doc,_=self.source(fmt="png");cid=self.visual_connection()
        req=self.request(doc,mode="vision",connection_id=cid);preview=tasks.preview(req)
        self.assertEqual(1,preview["image_count"]);self.assertGreater(preview["total_bytes"],0)
        with self.assertRaises(ValueError): self.manager.submit(tasks.ParseRequest(**(req.model_dump()|{"preview_id":preview["preview_id"]})))
        accepted=tasks.ParseRequest(**(req.model_dump()|{"preview_id":preview["preview_id"],"accepted":True}))
        with patch.object(providers,"ocr",side_effect=self.recognition),patch.object(providers,"visual",side_effect=ValueError("坏响应")):
            result=self.manager.submit(accepted,synchronous=True)
        self.assertEqual("failed",result["status"])
        with self.assertRaises(ValueError): self.manager.submit(accepted,synchronous=True)

    def test_preview_expiration_and_configuration_change_send_nothing(self):
        doc,_=self.source(fmt="png");cid=self.visual_connection();req=self.request(doc,mode="vision",connection_id=cid)
        preview=tasks.preview(req)
        with s.conn() as db:
            body=copy.deepcopy(preview);body["expires_at"]="2001-01-01T00:00:00+00:00"
            db.execute("UPDATE ef_document_processing_previews SET body=? WHERE id=?",(tasks.encode(body),preview["preview_id"]))
        with patch.object(providers,"visual") as visual:
            with self.assertRaises(Exception): self.manager.submit(tasks.ParseRequest(**(req.model_dump()|{"preview_id":preview["preview_id"],"accepted":True})))
            visual.assert_not_called()

    def test_visual_connection_change_after_response_does_not_publish(self):
        doc,_=self.source(fmt="png");cid=self.visual_connection();req=self.request(doc,mode="vision",connection_id=cid)
        preview=tasks.preview(req);before=documents.current_parse_id(doc["id"],doc["sha256"])
        def changed(*args,**kwargs):
            with s.conn() as db:
                row=db.execute("SELECT body FROM ef_connections WHERE id=?",(cid,)).fetchone();config=json.loads(row[0]);config["revision"]+=1
                db.execute("UPDATE ef_connections SET body=? WHERE id=?",(json.dumps(config),cid))
            return {"blocks":[],"tables":[],"charts":[],"issues":[],"raw_response":"{}"}
        with patch.object(providers,"ocr",side_effect=self.recognition),patch.object(providers,"visual",side_effect=changed): result=self.manager.submit(tasks.ParseRequest(**(req.model_dump()|{"preview_id":preview["preview_id"],"accepted":True})),synchronous=True)
        self.assert_no_publication(doc,before,result)

    def test_recovery_marks_unfinished_tasks_interrupted_without_inventing_duration(self):
        doc,_=self.source();snapshot=tasks._snapshot(self.request(doc,mode="native"));body={**snapshot,"id":uuid.uuid4().hex,"status":"running","stage":"解析","created_at":s.now(),"events":[{"status":"running","duration_ms":None}],"duration_ms":None,"result":None}
        with s.conn() as db: tasks._put(db,body)
        tasks.initialize(recover=True)
        recovered=self.manager.get(body["id"])
        self.assertEqual("interrupted",recovered["status"]);self.assertIsNone(recovered["duration_ms"]);self.assertIsNone(recovered["result"])

    def test_bad_page_scope_rejected_before_provider_execution(self):
        doc,_=self.source(fmt="png")
        with patch.object(providers,"ocr") as ocr:
            for pages in ([1,1],[2],[0]):
                with self.assertRaises(ValueError): self.manager.submit(tasks.ParseRequest(document_id=doc["id"],pages=pages,mode="ocr"),synchronous=True)
            ocr.assert_not_called()


if __name__=="__main__": unittest.main()
