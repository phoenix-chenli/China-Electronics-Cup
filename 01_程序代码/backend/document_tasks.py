"""Durable, cancellable document recognition with atomic immutable publication."""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import Field, StrictInt
from . import connections, documents, store as s

router=APIRouter(prefix="/api/document-processing")
LOCK=threading.RLock()
ACTIVE={"queued","running"}
MAX_PAGES=40
MAX_SECONDS=900
_initialized=set()


def encode(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"))
def fingerprint(value): return hashlib.sha256(encode(value).encode()).hexdigest()


class ProcessingConfig(connections.Strict):
    default_mode:Literal["native","ocr","vision"]="ocr"
    connection_id:str|None=Field(default=None,max_length=100)


class ParseRequest(connections.Strict):
    document_id:str=Field(min_length=1,max_length=100)
    version:str|None=Field(default=None,pattern=r"^[a-f0-9]{64}$")
    parse_id:str|None=Field(default=None,max_length=100)
    pages:list[StrictInt]=Field(min_length=1,max_length=MAX_PAGES)
    mode:Literal["native","ocr","vision"]="ocr"
    rotation:Literal[0,90,180,270]=0
    connection_id:str|None=Field(default=None,max_length=100)
    media_ids:list[str]|None=Field(default=None,max_length=MAX_PAGES)
    preview_id:str|None=Field(default=None,max_length=100)
    accepted:bool=False


class RetryRequest(connections.Strict):
    preview_id:str|None=Field(default=None,max_length=100)
    accepted:bool=False


class InstallRequest(connections.Strict):
    accepted:bool=False


def initialize(*, recover=False):
    key=str(s.APP_DB.resolve())
    with LOCK:
        if key in _initialized and not recover: return
        with s.conn() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS ef_document_processing_tasks(
                id TEXT PRIMARY KEY,document_id TEXT NOT NULL,status TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS ef_document_processing_doc ON ef_document_processing_tasks(document_id,created_at);
              CREATE TABLE IF NOT EXISTS ef_document_processing_previews(
                id TEXT PRIMARY KEY,body TEXT NOT NULL,created_at TEXT NOT NULL,consumed INTEGER NOT NULL DEFAULT 0);
            """)
            for row in db.execute("SELECT * FROM ef_document_processing_tasks WHERE status IN ('queued','running')").fetchall():
                body=json.loads(row["body"])
                body.update(status="interrupted",stage="解析中断",error="程序重启，未发布未完成的解析",recovered_at=s.now())
                for event in body.get("events",[]):
                    if event.get("status")=="running": event.update(status="interrupted",summary="程序重启，结束时间与耗时未知")
                db.execute("UPDATE ef_document_processing_tasks SET status='interrupted',body=? WHERE id=?",(encode(body),row["id"]))
            db.execute("DELETE FROM ef_document_processing_previews WHERE created_at<?",((datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),))
        _initialized.add(key)


def _provider(req):
    from . import document_providers as providers
    if req.mode=="native": return {"local":True,"ready":True,"fingerprint":documents.PARSER,"model":"原生格式解析","provider_id":"native"}
    if req.mode=="ocr":
        value=providers.describe()
        if not value.get("ready"): raise ValueError(value.get("reason") or "请先主动安装并验证本机 OCR 模型")
        return value
    if not req.connection_id: raise ValueError("请选择已测试启用的视觉连接")
    value=connections.ready(req.connection_id,{"vision"})
    identity={k:value.get(k) for k in ("id","revision","model","endpoint")}
    return {"connection_id":value["id"],"connection_revision":value["revision"],"model":value["model"],
            "name":value["name"],"local":connections.is_local(value),"ready":True,"fingerprint":fingerprint(identity)}


def _snapshot(req):
    data=documents.layout(req.document_id,req.version,req.parse_id)
    doc=data["document"]
    if doc["archived"] or not doc["is_current"]: raise ValueError("正式解析只处理未归档的当前原件")
    if documents.current_parse_id(req.document_id,doc["sha256"])!=data["parse_id"]: raise ValueError("基础解析已变化，请选择当前解析后重试")
    pages=sorted(set(req.pages))
    if len(pages)!=len(req.pages) or any(type(p) is not int or not 1<=p<=len(data["pages"]) for p in pages): raise ValueError("页面清单含重复或无效页码")
    if req.mode!="native" and doc["format"] not in {"pdf","png","jpg","jpeg","webp","docx"}: raise ValueError("OCR 与正式视觉解析支持 PDF 页、图片与 DOCX 嵌图")
    provider=_provider(req)
    images=[]
    if req.mode!="native":
        for number in pages:
            if doc["format"]=="docx":
                from .document_native import read_docx_media
                _,path=documents.version(req.document_id,doc["sha256"])
                for media in data["pages"][number-1].get("images",[]):
                    if req.media_ids is not None and media["id"] not in req.media_ids: continue
                    raw,_=read_docx_media(path,media["id"])
                    images.append({"page":number,"media_id":media["id"],"image_sha256":hashlib.sha256(raw).hexdigest(),"bytes":len(raw),"preview_url":f"/api/documents/{req.document_id}/media/{media['id']}?version={doc['sha256']}"})
            else:
                raw=documents.render_image(req.document_id,number,doc["sha256"])
                images.append({"page":number,"image_sha256":hashlib.sha256(raw).hexdigest(),"bytes":len(raw),
                               "preview_url":f"/api/documents/{req.document_id}/pages/{number}/image?version={doc['sha256']}"})
        if not images: raise ValueError("所选逻辑节没有可识别的嵌图，请选择包含图片的节")
        if len(images)>MAX_PAGES: raise ValueError("所选范围超过 40 张图像，请缩小逻辑节范围")
        if any(x["bytes"]>15_000_000 for x in images): raise ValueError("单张图像超过 15 MB，请缩小原件")
        if req.media_ids is not None and (len(set(req.media_ids))!=len(req.media_ids) or set(req.media_ids)!={x.get("media_id") for x in images}): raise ValueError("嵌图清单不属于所选逻辑节")
    snapshot={"document":{"id":req.document_id,"name":doc["name"],"sha256":doc["sha256"],"base_parse_id":data["parse_id"],"format":doc["format"]},
            "pages":pages,"mode":req.mode,"rotation":req.rotation,"connection_id":req.connection_id,"provider":provider,
            "images":images,"image_count":len(images),"total_bytes":sum(x["bytes"] for x in images),
            "scope":"本机处理" if provider.get("local") else "外部视觉服务"}
    if req.mode=="vision":
        from . import document_providers as providers
        snapshot["ocr_provider"]=providers.describe()
    return snapshot


@router.get("/config")
def configuration():
    from . import document_providers as providers
    with s.conn() as db: row=db.execute("SELECT value FROM settings WHERE key='document_processing'").fetchone()
    choices=json.loads(row[0]) if row else ProcessingConfig().model_dump()
    info=providers.configuration()
    return {**info,**choices,"limits":{**info.get("limits",{}),"pages":MAX_PAGES,"seconds":MAX_SECONDS,"workers":2,"queued":12}}


@router.put("/config")
def configure(req:ProcessingConfig):
    if req.connection_id:
        connection=connections.get(req.connection_id)
        if connection["kind"]!="vision": raise ValueError("请选择视觉类型的连接")
    with s.conn() as db: db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)",("document_processing",encode(req.model_dump())))
    return configuration()


@router.get("/models")
def models_status():
    from . import document_providers as providers
    return providers.models_status()


@router.post("/models/install")
def install(req:InstallRequest):
    from . import document_providers as providers
    return providers.install(req.accepted)


@router.post("/preview")
def preview(req:ParseRequest):
    initialize()
    body=_snapshot(req)
    body.update(preview_id=uuid.uuid4().hex,created_at=s.now(),expires_at=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat())
    with s.conn() as db: db.execute("INSERT INTO ef_document_processing_previews VALUES (?,?,?,0)",(body["preview_id"],encode(body),body["created_at"]))
    return body


def _validate_preview(db, req, snapshot):
    if req.mode!="vision" and not req.preview_id: return
    if req.mode=="vision" and not req.accepted: raise ValueError("请确认预览中的具体页面和发送量")
    if not req.preview_id: raise ValueError("正式视觉解析需要新的发送预览")
    row=db.execute("SELECT * FROM ef_document_processing_previews WHERE id=?",(req.preview_id,)).fetchone()
    if not row or row["consumed"]: raise ValueError("预览不存在或已使用，请重新预览")
    body=json.loads(row["body"])
    if datetime.now(timezone.utc)>=datetime.fromisoformat(body["expires_at"]): raise HTTPException(410,"发送预览已过期，请重新生成")
    if any(body.get(k)!=snapshot.get(k) for k in snapshot): raise ValueError("文档、页面、图像或模型已变化，请重新预览")
    db.execute("UPDATE ef_document_processing_previews SET consumed=1 WHERE id=? AND consumed=0",(req.preview_id,))


def _put(db, body):
    db.execute("INSERT OR REPLACE INTO ef_document_processing_tasks VALUES (?,?,?,?,?)",(body["id"],body["document"]["id"],body["status"],encode(body),body["created_at"]))


class Manager:
    def __init__(self, workers=2):
        self.pool=ThreadPoolExecutor(max_workers=workers,thread_name_prefix="document-parse")
        self.stops={}

    def get(self, tid):
        initialize()
        with s.conn() as db: row=db.execute("SELECT body FROM ef_document_processing_tasks WHERE id=?",(tid,)).fetchone()
        if not row: raise HTTPException(404,"解析任务不存在")
        return json.loads(row[0])

    def submit(self, req, *, attempt_of=None, synchronous=False):
        initialize()
        snapshot=_snapshot(req)
        stop=threading.Event()
        body={**snapshot,"id":uuid.uuid4().hex,"status":"queued","stage":"等待解析","processed":0,"total":len(snapshot["images"]) if req.mode!="native" else len(snapshot["pages"]),
              "created_at":s.now(),"started_at":None,"finished_at":None,"duration_ms":None,"events":[],"error":None,"result":None,
              "request":req.model_dump(exclude={"preview_id","accepted"}),"attempt_of":attempt_of}
        with LOCK:
            with s.conn() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT COUNT(*) FROM ef_document_processing_tasks WHERE status IN ('queued','running')").fetchone()[0]>=12: raise HTTPException(429,"解析队列已满，请稍后重试")
                if db.execute("SELECT 1 FROM ef_document_processing_tasks WHERE document_id=? AND status IN ('queued','running')",(req.document_id,)).fetchone(): raise HTTPException(409,"这份文档仍在解析，请等待或取消")
                self._guard(body,stop,db=db)
                _validate_preview(db,req,snapshot)
                _put(db,body)
            self.stops[body["id"]]=stop
        key=str(s.APP_DB.resolve())
        if synchronous: self.work(body["id"],stop,key)
        else: self.pool.submit(self.work,body["id"],stop,key)
        return self.get(body["id"])

    def _guard(self, body, stop, deadline=None, db=None):
        if stop.is_set(): raise InterruptedError("解析已取消")
        if deadline is not None and time.monotonic()>=deadline: raise TimeoutError("解析超过 900 秒总时限，请分批处理")
        if db is None:
            with s.conn() as connection: self._guard(body,stop,deadline,connection)
            documents.version(body["document"]["id"],body["document"]["sha256"])
            return
        row=db.execute("SELECT sha256,status FROM documents WHERE id=?",(body["document"]["id"],)).fetchone()
        if not row or row["sha256"]!=body["document"]["sha256"] or row["status"]=="archived": raise ValueError("文档已替换或归档，未发布解析")
        active=documents.active_parse(db,body["document"]["id"],body["document"]["sha256"])
        if not active or active["id"]!=body["document"]["base_parse_id"]: raise ValueError("基础解析已改变，未发布迟到解析")
        if body["mode"]=="vision":
            row=db.execute("SELECT body FROM ef_connections WHERE id=?",(body["connection_id"],)).fetchone()
            if not row: raise ValueError("视觉连接已删除")
            config=json.loads(row[0]);expected=body["provider"]
            if config.get("kind")!="vision" or not config.get("enabled") or not (config.get("last_test") or {}).get("ok") or config.get("revision")!=expected["connection_revision"]: raise ValueError("视觉连接已变化，请重新预览")
            identity={k:config.get(k) for k in ("id","revision","model","endpoint")}
            if fingerprint(identity)!=expected["fingerprint"]: raise ValueError("视觉服务身份已变化")
            row=db.execute("SELECT body FROM ef_settings WHERE key='privacy'").fetchone()
            if not connections.is_local(config) and (not row or json.loads(row[0]).get("local_only",True)): raise ValueError("仅本机处理已开启，未发送或发布外部结果")
        elif body["mode"]=="ocr":
            from . import document_providers as providers
            current=providers.describe()
            if not current.get("ready") or current.get("fingerprint")!=body["provider"].get("fingerprint"): raise ValueError("OCR 模型已变化或不可用，未发布解析")
        if body["mode"]=="vision" and "ocr_provider" in body:
            from . import document_providers as providers
            current=providers.describe();expected=body["ocr_provider"]
            if bool(current.get("ready"))!=bool(expected.get("ready")) or current.get("fingerprint")!=expected.get("fingerprint"): raise ValueError("视觉解析的 OCR 观察模型已变化，请重新预览")

    def save_progress(self, body):
        with LOCK:
            with s.conn() as db:
                row=db.execute("SELECT status FROM ef_document_processing_tasks WHERE id=?",(body["id"],)).fetchone()
                if not row or row[0] not in ACTIVE: return False
                _put(db,body)
        return True

    def work(self, tid, stop, key):
        body=self.get(tid);started=time.monotonic();deadline=started+MAX_SECONDS
        if body["status"] not in ACTIVE: return
        body.update(status="running",stage="读取固定原件",started_at=s.now())
        if not self.save_progress(body): return
        try:
            if str(s.APP_DB.resolve())!=key: raise ValueError("数据工作区已变化，任务已停止")
            self._guard(body,stop,deadline)
            data=documents.layout(body["document"]["id"],body["document"]["sha256"],body["document"]["base_parse_id"])
            parsed={k:copy.deepcopy(v) for k,v in data.items() if k not in {"document","parse_id"}}
            from . import document_native as native, document_providers as providers
            _,native_path=documents.version(body["document"]["id"],body["document"]["sha256"])
            native_body=documents._parse_file(native_path) if body["mode"]=="native" and body["document"]["format"]=="docx" else None
            inputs=[{"page":number} for number in body["pages"]] if body["mode"]=="native" else body["images"]
            for expected in inputs:
                number=expected["page"];media_id=expected.get("media_id")
                self._guard(body,stop,deadline)
                event={"id":uuid.uuid4().hex,"page":number,"media_id":media_id,"method":body["mode"],"status":"running","started_at":s.now(),"duration_ms":None}
                body["events"].append(event);body["stage"]=f"解析第 {number} 页";self.save_progress(body);page_started=time.monotonic()
                if body["mode"]=="native":
                    fresh=copy.deepcopy(native_body["pages"][number-1]) if native_body is not None else documents.parse_page(native_path,number)
                else:
                    if media_id:
                        _,path=documents.version(body["document"]["id"],body["document"]["sha256"])
                        raw,_=native.read_docx_media(path,media_id)
                    else: raw=documents.render_image(body["document"]["id"],number,body["document"]["sha256"])
                    if len(raw)!=expected["bytes"] or hashlib.sha256(raw).hexdigest()!=expected["image_sha256"]: raise ValueError("页面图像与冻结预览不一致")
                    base=parsed["pages"][number-1]
                    if body["mode"]=="ocr":
                        result=providers.ocr(raw,rotation=body["rotation"],cancelled=stop,deadline=deadline)
                        if "tables" not in result: result["tables"]=native.detect_ruled_tables(raw,result.get("lines",[]))
                        fresh=native.apply_ocr_page(base,result,media_id=media_id)
                    else:
                        lines=[{"text":b["text"],"box":b.get("box"),"status":b.get("status","confirmed")} for b in base.get("blocks",[]) if b.get("text","").strip() and b.get("media_id")==media_id]
                        fresh=copy.deepcopy(base)
                        if body.get("ocr_provider",{}).get("ready"):
                            ocr_started=time.monotonic()
                            observation={"id":uuid.uuid4().hex,"page":number,"media_id":media_id,"method":"ocr_observation","status":"running","started_at":s.now(),"duration_ms":None}
                            body["events"].append(observation);body["stage"]=f"识别第 {number} 页的视觉观察文字";self.save_progress(body)
                            try:
                                observed=providers.ocr(raw,rotation=body["rotation"],cancelled=stop,deadline=deadline)
                                self._guard(body,stop,deadline)
                                if "tables" not in observed: observed["tables"]=native.detect_ruled_tables(raw,observed.get("lines",[]))
                                if not base.get("text_available") or media_id:
                                    fresh=native.apply_ocr_page(base,observed,media_id=media_id)
                                else:
                                    fresh["ocr_lines"]=copy.deepcopy(observed.get("lines",[]))
                                    fresh["ocr_provenance"]={**copy.deepcopy(observed.get("provenance",{})),"input_image_sha256":observed.get("input_image_sha256"),"fingerprint":observed.get("fingerprint"),"duration_ms":observed.get("duration_ms")}
                                lines.extend(line for line in observed.get("lines",[]) if line.get("status")=="confirmed")
                                if "ocr_provenance" not in fresh: fresh["ocr_provenance"]=copy.deepcopy(fresh.get("provenance",{}))
                                observation.update(status="completed",finished_at=s.now(),duration_ms=round((time.monotonic()-ocr_started)*1000,2),summary="已实际识别并保存 OCR 观察文字")
                            except Exception as exc:
                                observation.update(status="failed",finished_at=s.now(),duration_ms=round((time.monotonic()-ocr_started)*1000,2),summary=str(exc));raise
                            finally:self.save_progress(body)
                        else:
                            body["events"].append({"id":uuid.uuid4().hex,"page":number,"media_id":media_id,"method":"ocr_observation","status":"not_available","started_at":None,"duration_ms":None,"summary":"本机 OCR 未就绪；视觉结果仅依据已有同区域文字核对，缺观察依据的内容保留待复核"})
                            self.save_progress(body)
                        body["stage"]=f"结构视觉解析第 {number} 页";self.save_progress(body)
                        result=providers.visual(raw,connection_id=body["connection_id"],expected_revision=body["provider"]["connection_revision"],observed_lines=lines,cancelled=stop,deadline=deadline)
                        fresh=native.apply_visual_page(fresh,result,media_id=media_id)
                    if media_id:
                        pending=any(line.get("media_id")==media_id for line in fresh.get("recognition_pending",[])) or any(obj.get("media_id")==media_id and obj.get("status") in {"pending","needs_review"} for key in ("tables","charts") for obj in fresh.get(key,[])) or bool(result.get("issues"))
                        for media in fresh.get("images",[]):
                            if media.get("id")==media_id: media.update(status="needs_review" if pending else "recognized",processing_method=body["mode"],human_reviewed=False)
                self._guard(body,stop,deadline)
                parsed["pages"][number-1]=fresh
                event.update(status="completed",finished_at=s.now(),duration_ms=round((time.monotonic()-page_started)*1000,2),summary="解析结果已校验，等待整批发布")
                body["processed"]+=1;self.save_progress(body)
            native.link_continuations(parsed["pages"])
            parsed["parser"]="document-processing-v1"
            parsed["processing"]={"task_id":tid,"mode":body["mode"],"pages":body["pages"],"rotation":body["rotation"],"provider":body["provider"],"base_parse_id":body["document"]["base_parse_id"]}
            self._guard(body,stop,deadline)
            with LOCK:
                with s.conn() as db:
                    db.execute("BEGIN IMMEDIATE")
                    state=db.execute("SELECT status FROM ef_document_processing_tasks WHERE id=?",(tid,)).fetchone()
                    if not state or state[0] not in ACTIVE: raise InterruptedError("解析已取消或中断")
                    self._guard(body,stop,deadline,db)
                    after=documents.save_parse(db,body["document"]["id"],body["document"]["sha256"],parsed,"正式解析任务："+body["mode"])
                    quality=s.quality_for_parse(parsed)
                    db.execute("UPDATE documents SET pages=?,status=?,quality_json=?,updated_at=? WHERE id=?",(len(parsed["pages"]),"needs_review" if quality["quality_flags"] else "ready",encode(quality),s.now(),body["document"]["id"]))
                    db.execute("DELETE FROM blocks WHERE document_id=?",(body["document"]["id"],))
                    for i,b in enumerate(s.extract(None,parsed)):
                        db.execute("INSERT INTO blocks VALUES (?,?,?,?,?,?,?,?,?)",(body["document"]["id"]+"-"+str(i),body["document"]["id"],b["page"],b["title_path"],b["content_type"],b["text"],encode(b["bbox"]),encode(b["quality"]),s.now()))
                    body.update(status="completed",stage="解析已保存",finished_at=s.now(),duration_ms=round((time.monotonic()-started)*1000,2),result={"parse_id":after["parse_id"],"sha256":body["document"]["sha256"],"before_parse_id":body["document"]["base_parse_id"]})
                    _put(db,body)
            from . import rag_index
            try:
                indexed=rag_index.submit_index([body["document"]["id"]]);body["index_job_id"]=indexed["id"]
            except Exception:
                body["index_error"]="解析已保存，检索索引提交失败；可在资料库重建索引"
            with s.conn() as db: _put(db,body)
        except Exception as exc:
            error=str(exc.detail) if isinstance(exc,HTTPException) else str(exc)
            with LOCK:
                with s.conn() as db:
                    current=db.execute("SELECT status FROM ef_document_processing_tasks WHERE id=?",(tid,)).fetchone()
                    if current and current[0] in ACTIVE:
                        status="cancelled" if stop.is_set() or isinstance(exc,InterruptedError) else "failed"
                        for event in body["events"]:
                            if event["status"]=="running": event.update(status=status,finished_at=s.now(),duration_ms=round((time.monotonic()-page_started)*1000,2),summary=error)
                        body.update(status=status,stage="已取消" if status=="cancelled" else "解析失败",error=error,result=None,finished_at=s.now(),duration_ms=round((time.monotonic()-started)*1000,2))
                        _put(db,body)
        finally:
            with LOCK: self.stops.pop(tid,None)

    def cancel(self, tid):
        with LOCK:
            body=self.get(tid)
            if body["status"] in ACTIVE:
                if tid in self.stops: self.stops[tid].set()
                body.update(status="cancelled",stage="已取消",error=None,finished_at=s.now(),result=None)
                if body.get("started_at"): body["duration_ms"]=round((datetime.now(timezone.utc)-datetime.fromisoformat(body["started_at"])).total_seconds()*1000,2)
                for event in body["events"]:
                    if event["status"]=="running": event.update(status="cancelled",finished_at=s.now(),duration_ms=round((datetime.now(timezone.utc)-datetime.fromisoformat(event["started_at"])).total_seconds()*1000,2),summary="用户取消；已发出的远程请求可能仍在服务时限内结束")
                with s.conn() as db: _put(db,body)
            return body

    def retry(self, tid, req):
        previous=self.get(tid)
        if previous["status"] not in {"failed","cancelled","interrupted"}: raise HTTPException(409,"只能重试失败、取消或中断的解析")
        payload={**previous["request"],"preview_id":req.preview_id,"accepted":req.accepted}
        return self.submit(ParseRequest(**payload),attempt_of=tid)


manager=Manager()


@router.get("/tasks")
def listing(document_id:str|None=None):
    initialize()
    with s.conn() as db:
        rows=db.execute("SELECT body FROM ef_document_processing_tasks"+(" WHERE document_id=?" if document_id else "")+" ORDER BY created_at DESC LIMIT 30",(document_id,) if document_id else ()).fetchall()
    return [json.loads(row[0]) for row in rows]


@router.post("/tasks")
def submit(req:ParseRequest): return manager.submit(req)


@router.get("/tasks/{tid}")
def get(tid:str): return manager.get(tid)


@router.post("/tasks/{tid}/cancel")
def cancel(tid:str): return manager.cancel(tid)


@router.post("/tasks/{tid}/retry")
def retry(tid:str,req:RetryRequest): return manager.retry(tid,req)
