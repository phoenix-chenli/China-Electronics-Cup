from __future__ import annotations
import json
import hashlib
import os
import platform
import mimetypes
import re
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, UploadFile, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from . import store as s
from . import engine, conversations, scenarios, settings, connections, extensions, sharing, agent, database_query
from . import documents as document_engine
from . import rag_index, rag_providers
from . import document_tasks, document_providers, document_review, query_routing, public_statistics, chinook_benchmark
from .seed import seed_business, write_seed_docs

# Capture once for runtime/source acceptance. Re-reading edited files in the
# health handler would incorrectly identify old running code as the new build.
BACKEND_SOURCE_AT_STARTUP={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in sorted(Path(__file__).parent.glob('*.py'))}

app=FastAPI(title="证流",version="0.7.0")
app.include_router(conversations.router)
app.include_router(scenarios.router)
app.include_router(settings.router)
app.include_router(connections.router)
app.include_router(extensions.router)
app.include_router(sharing.router)
app.include_router(agent.router)
app.include_router(document_review.router)
app.include_router(rag_providers.router)
app.include_router(document_tasks.router)
def init_app():
    s.init(); seed_business(); write_seed_docs()
    seed_docs=s.PROJECT_ROOT/'02_数据资料'/'示例数据'/'docs'
    for source in sorted(seed_docs.glob('*')):
        if source.is_file() and not source.is_symlink() and source.suffix in {'.md','.txt','.pdf','.png','.jpg','.jpeg','.webp','.docx'}:
            target=s.DOCS/source.name
            if not target.exists(): shutil.copy2(source,target)
    scenarios.initialize()
    settings.initialize()
    connections.initialize()
    agent.initialize()
    rag_providers.initialize()
    document_providers.initialize()
    for p in sorted(s.DOCS.iterdir()):
        if p.is_file(): s.index_document(p)
@app.on_event("startup")
def startup():
    init_app()
    conversations.initialize()
    if os.environ.get('EVIDENCEFLOW_SKIP_PUBLIC_DEMO') != '1':
        public_statistics.install()
        # A benchmark that cannot be bound reports a reason; it never stops the service.
        chinook_benchmark.install()
    document_tasks.initialize()

@app.middleware("http")
async def local_origin(request: Request, call_next):
    if request.url.hostname not in {"localhost","127.0.0.1","::1"}:
        return JSONResponse({"detail":"只接受本机地址"},status_code=403)
    if request.method not in {"GET","HEAD","OPTIONS"}:
        origin=request.headers.get("origin")
        if origin and origin not in {f"http://127.0.0.1:{p}" for p in [8765,8876,4173]} | {f"http://localhost:{p}" for p in [8765,8876,4173]}:
            return JSONResponse({"detail":"只接受本机页面的操作"},status_code=403)
    return await call_next(request)
@app.exception_handler(RequestValidationError)
async def invalid_fields(request,exc):
    fields=', '.join('.'.join(str(x) for x in e['loc'][1:]) for e in exc.errors())
    return JSONResponse({'detail':'字段格式不正确：'+fields},status_code=422)

@app.exception_handler(ValueError)
async def invalid(request, exc):
    detail={"detail":str(exc), 'error_detail':agent.failure_detail(str(exc))}
    if isinstance(exc,agent.AgentFailure):
        detail={**exc.result,**detail,'error_detail':exc.result.get('error_detail',detail['error_detail'])}
    return JSONResponse(detail,status_code=400)

class DocumentContext(BaseModel):
    document_id: str=Field(min_length=1,max_length=100)
    sha256: str=Field(pattern=r'^[a-f0-9]{64}$')
    parse_id: str | None=Field(default=None,max_length=100)
    page: int=Field(default=1,ge=1)
    text_range: dict[str,Any] | None=None
    quote: str=Field(default='',max_length=100000)
class RagScope(connections.Strict):
    scope_type: Literal['document','corpus']
    document_ids: list[str] | None=Field(default=None,max_length=500)
class QueryRequest(BaseModel):
    question: str=Field(min_length=1,max_length=2000)
    previous_context: dict[str,Any]=Field(default_factory=dict)
    confirmed: dict[str,Any]=Field(default_factory=dict)
    workspace_id: str="sales-demo"
    project_id: str | None=None
    scope_override: Literal['sales','database','knowledge','hybrid'] | None=None
    session_id: str | None=None
    request_id: str | None=Field(default=None,max_length=100)
    parent_message_id: str | None=None
    inherit_context: bool=True
    mode: str="query"
    display_question: str | None=Field(default=None,max_length=2000)
    document_context: DocumentContext | None=None
    rag_scope: RagScope | None=None
class RecomputeRequest(BaseModel):
    run_id: str | None=None
    question: str=""
    overrides: dict[str,Any]=Field(default_factory=dict)
class SaveRequest(BaseModel):
    name: str=Field(min_length=1,max_length=120)
    run_id: str
class DefinitionRequest(BaseModel):
    kind: str
    name: str
    description: str=""
    aliases: list[str]=Field(default_factory=list)
    source: str="用户确认"
    field: str | None=None
    target: str | None=None
    threshold: str | None=None
    rate: str | None=None
    effective_from: str | None=None
    effective_to: str | None=None
    id: str | None=None
class PageRequest(BaseModel):
    page: int=Field(default=1,ge=1)
    version: str | None=None
    parse_id: str | None=None
class SourceRequest(BaseModel):
    locator: dict[str,Any]
    quote: str=Field(default="",max_length=100000)
class VerifyRequest(BaseModel):
    run_id: str
    claim_id: str
class RenameRequest(BaseModel):
    name: str=Field(min_length=1,max_length=120)

def run(req, cancelled=None, progress=None):
    if req.workspace_id!="sales-demo": raise ValueError("工作区不存在，未继承其他工作区条件")
    document_context=req.document_context.model_dump(exclude_none=True) if req.document_context else None
    previous={k:v for k,v in req.previous_context.items() if k!='rag_scope'}
    context=agent.query_context(previous,document_context,req.rag_scope.model_dump(exclude_none=True) if req.rag_scope else None,
                                question=req.question,project_id=req.project_id,scope_override=req.scope_override)
    return agent.execute(req.question,previous,req.confirmed,cancelled=cancelled,progress=progress,document_context=document_context,database_context=context)

@app.get("/api/health")
def health():
    cfg=agent.saved_config()
    return {"ok":True,"service":"evidenceflow","version":"0.7.0","mode":"configurable-agent","agent_mode":cfg['mode'],"query_scope":cfg.get('query_scope','sales'),"platform":platform.system()+" "+platform.machine(),"instance":{"workspace":os.environ.get("EVIDENCEFLOW_WORKSPACE_ID","manual"),"id":os.environ.get("EVIDENCEFLOW_INSTANCE_ID","manual"),"pid":os.getpid()},"backend_source_at_startup":BACKEND_SOURCE_AT_STARTUP,"model_capabilities":{**connections.capabilities(),"ocr":bool(document_providers.describe().get('ready'))},"local_only":connections.local_only(),"features":engine.FEATURES,"time":s.now()}

@app.get("/api/summary")
def summary(project_id: str | None=None):
    tables=query_routing.project_tables(project_id); docs=query_routing.project_docs(project_id)
    if project_id and query_routing.profile(project_id).get('managed_public'):
        bound=query_routing.profile(project_id)
        return {"workspace_id":project_id,"workspace_name":bound['project_name'],"counts":{t['name']:t['row_count'] for t in tables},"tables":[t['name'] for t in tables],"documents":len(docs),"pages":sum(d['pages'] for d in docs),"data_version":s.data_version(),"data_source_note":bound['source_label']}
    return {"workspace_id":"sales-demo","workspace_name":"销售与项目经营","counts":{t["name"]:t["row_count"] for t in tables},"tables":[t["name"] for t in tables],"documents":len(docs),"pages":sum(d["pages"] for d in docs),"data_version":s.data_version(),"data_source_note":"合成示例数据"}
@app.get("/api/tables")
def tables(project_id: str | None=None): return query_routing.project_tables(project_id)
@app.get("/api/dashboard")
def dashboard(project_id: str = "default"):
    with s.conn() as c:
        if not c.execute("SELECT 1 FROM ef_projects WHERE id=?", (project_id,)).fetchone():
            raise HTTPException(404, "项目不存在")
        # Only durable user submissions count. Evaluations and old /query runs
        # have no task/session and must not inflate the product's usage numbers.
        counts=c.execute("""SELECT COUNT(*) AS total,
          SUM(date(t.created_at,'localtime')=date('now','localtime')) AS today
          FROM ef_tasks t JOIN ef_sessions s ON s.id=t.session_id WHERE s.project_id=?""", (project_id,)).fetchone()
        trend_counts={r['day']:r['count'] for r in c.execute("""SELECT date(t.created_at,'localtime') AS day,COUNT(*) AS count
          FROM ef_tasks t JOIN ef_sessions s ON s.id=t.session_id WHERE s.project_id=?
          AND date(t.created_at,'localtime')>=date('now','localtime','-29 days') GROUP BY day""", (project_id,))}
        rows=c.execute("""SELECT m.id AS message_id,m.session_id,m.text AS question,m.created_at,
          a.id AS result_message_id,t.status FROM ef_messages m JOIN ef_sessions s ON s.id=m.session_id
          LEFT JOIN ef_tasks t ON t.id=m.task_id
          LEFT JOIN ef_messages a ON a.task_id=m.task_id AND a.role='assistant' AND a.result_json IS NOT NULL
          WHERE s.project_id=? AND m.role='user' ORDER BY m.sequence DESC LIMIT 6""", (project_id,)).fetchall()
        session_count=c.execute("SELECT COUNT(*) FROM ef_sessions WHERE project_id=?", (project_id,)).fetchone()[0]
    today=datetime.now().date()
    trend=[{"day":str(today-timedelta(days=offset)),"count":trend_counts.get(str(today-timedelta(days=offset)),0)} for offset in range(29,-1,-1)]
    bound=query_routing.profile(project_id)
    sales=[] if bound.get('managed_public') else s.read("SELECT strftime('%Y',order_date) AS year, SUM(sales_amount-returned_amount) AS amount FROM orders GROUP BY year ORDER BY year")[0]
    return {"project_id":project_id,"recent":[dict(r) for r in rows],"session_count":session_count,
      "query_count":counts['total'],"today_count":counts['today'] or 0,"trend":trend,"sales":sales,
      "activity_scope":"当前项目的用户提交，含取消、失败与重试；不含系统检查",
      "data_note":bound['source_label'],"timezone":str(datetime.now().astimezone().tzinfo)}
@app.get("/api/tables/{name}/rows")
def table_rows(name: str, offset: int=0, limit: int=100, project_id: str | None=None):
    t=next((t for t in query_routing.project_tables(project_id) if t["name"]==name),None)
    if t is None: raise HTTPException(404,"数据表不存在")
    if not 1<=limit<=1000 or offset<0: raise ValueError("无效的分页参数")
    if t.get('connection_id'):
        config=connections.ready(t['connection_id'],connections.DB_KINDS)
        with connections.database(config) as db:
            rows=connections.rows(db,f"SELECT * FROM {connections.identifier(config,t.get('schema','main'),name)} LIMIT ? OFFSET ?",[limit,offset],limit=limit)['rows']
    else:
        rows,_=s.read(f"SELECT * FROM {s.quoted(name)} LIMIT ? OFFSET ?",[limit,offset])
    return {"rows":rows,"columns":[c["name"] for c in t["columns"]],"total":t["row_count"],"offset":offset}
@app.post("/api/tables/{name}/archive")
def archive_table(name: str):
    if name in s.BASE_TABLES: raise ValueError("示例基础表被查询流程使用，不能移除")
    if name not in {t["name"] for t in s.tables()}: raise HTTPException(404,"数据表不存在")
    with s.conn() as c: c.execute("UPDATE table_catalog SET archived=1 WHERE name=?",(name,))
    return {"ok":True}
@app.post("/api/tables/{name}/restore")
def restore_table(name: str):
    with s.conn() as c: c.execute("UPDATE table_catalog SET archived=0 WHERE name=?",(name,))
    return {"ok":True}
@app.get("/api/archived-tables")
def archived_tables():
    with s.conn() as c: return [dict(r) for r in c.execute("SELECT * FROM table_catalog WHERE archived=1")]

@app.get("/api/documents")
def documents(archived: bool=False, project_id: str | None=None): return query_routing.project_docs(project_id,archived)

_rag_previews={}
_rag_preview_lock=threading.Lock()
class RagReindex(connections.Strict):
    document_ids: list[str] | None=Field(default=None,max_length=500)
    remote_opt_in: bool=False
    preview_id: str | None=Field(default=None,max_length=100)

@app.get('/api/rag/status')
def rag_status():
    return {**rag_index.status(),'providers':rag_providers.capabilities()}

@app.post('/api/rag/reindex/preview')
def rag_preview(req: RagReindex):
    preview=rag_index.index_preview(req.document_ids)
    provider=rag_providers.describe('embedding')
    endpoint=None
    if provider.get('mode')=='connection' and provider.get('provider_id'):
        endpoint=connections.ready(provider['provider_id'],{'embedding'}).get('endpoint')
    pid=uuid.uuid4().hex
    with _rag_preview_lock:
        now=time.monotonic()
        for key in list(_rag_previews):
            if now-_rag_previews[key]['created']>600: _rag_previews.pop(key)
        if len(_rag_previews)>=100: _rag_previews.pop(next(iter(_rag_previews)))
        _rag_previews[pid]={'created':now,'preview':preview,'fingerprint':provider.get('fingerprint')}
    return {**preview,'preview_id':pid,'endpoint':endpoint,'model':provider.get('model'),
            'connection_revision':provider.get('connection_revision',provider.get('revision')),'provider_fingerprint':provider.get('fingerprint')}

@app.post('/api/rag/reindex')
def rag_reindex(req: RagReindex):
    provider=rag_providers.describe('embedding')
    remote=not provider.get('local',True)
    kwargs={}
    ids=req.document_ids
    if remote:
        if not req.remote_opt_in or not req.preview_id: raise ValueError('外部索引需要先预览将发送的文档并确认')
        with _rag_preview_lock: saved=_rag_previews.pop(req.preview_id,None)
        if not saved or time.monotonic()-saved['created']>600: raise ValueError('索引预览已过期，请重新预览')
        preview=saved['preview']
        if saved['fingerprint']!=provider.get('fingerprint'): raise ValueError('索引连接已改变，请重新预览')
        if ids is not None and set(ids)!=set(preview['document_ids']): raise ValueError('索引文档范围与预览不一致')
        ids=preview['document_ids']
        current=rag_index.index_preview(ids)
        if current['documents']!=preview['documents']: raise ValueError('文档或切片已改变，请重新预览')
        kwargs={'expected_preview':preview,'expected_provider':saved['fingerprint']}
    return rag_index.submit_index(ids,remote_index_opt_in=bool(remote and req.remote_opt_in),**kwargs)
@app.get("/api/documents/search")
def search_documents(q: str=""):
    if not q.strip(): return {"items":[]}
    items=[]
    for d in s.doc_list():
        try: hits=document_engine.search(d['id'],q)['matches'] if d['status']!='failed' else []
        except ValueError: hits=[]
        if q.casefold() in d['name'].casefold() or hits:items.append({"document_id":d['id'],"matches":hits[:3]})
    return {"items":items}
@app.get("/api/documents/{did}/view")
def document_view(did: str,version: str | None=None,parse_id: str | None=None):
    return document_engine.view(did,version,parse_id)
@app.get("/api/documents/{did}/pages/{number}")
def document_page(did: str,number: int,version: str | None=None,parse_id: str | None=None):
    return document_engine.get_page(did,number,version,parse_id)
@app.get("/api/documents/{did}/pages/{number}/image")
def document_image(did: str,number: int,version: str | None=None):
    return Response(document_engine.render_image(did,number,version),media_type="image/png",headers={"Cache-Control":"private, max-age=3600","X-Content-Type-Options":"nosniff"})

@app.get("/api/documents/{did}/media/{media_id}")
def document_media(did:str,media_id:str,version:str|None=None):
    d,path=document_engine.version(did,version)
    if d['format']!='docx': raise ValueError('此原件不是 DOCX')
    from .document_native import read_docx_media
    raw,media=read_docx_media(path,media_id)
    return Response(raw,media_type=media,headers={"Cache-Control":"private, max-age=3600","X-Content-Type-Options":"nosniff","Content-Security-Policy":"default-src 'none'; sandbox"})
@app.get("/api/documents/{did}/search")
def document_search(did: str,q: str="",version: str | None=None,parse_id: str | None=None):
    return document_engine.search(did,q,version,parse_id)
@app.post("/api/evidence/resolve")
def resolve_source(req: SourceRequest):
    if not req.locator.get('document_id'): raise ValueError('请选择文档来源')
    return document_engine.resolve(req.locator,req.quote)
@app.post("/api/evidence/verify")
def verify_source(req: VerifyRequest):
    return document_engine.verify_claim(req.run_id,req.claim_id)
@app.get("/api/documents/{did}/blocks")
def blocks(did: str): return s.blocks(did)
@app.get("/api/documents/{did}/original")
def original(did: str, version: str | None=None):
    d,path=document_engine.version(did,version)
    media=mimetypes.guess_type(d["name"])[0] or "application/octet-stream"
    if d["format"] in {"md","txt"}: media="text/plain; charset=utf-8"
    return FileResponse(path,media_type=media,headers={"X-Content-Type-Options":"nosniff","Content-Security-Policy":"default-src 'none'; sandbox"})
@app.post("/api/documents/{did}/archive")
def archive_doc(did: str):
    s.doc_get(did)
    with s.conn() as c: c.execute("UPDATE documents SET status='archived',updated_at=? WHERE id=?",(s.now(),did))
    return {"ok":True}
@app.post("/api/documents/{did}/restore")
def restore_doc(did: str):
    d=s.doc_get(did)
    quality=json.loads(d['quality_json'] or '{}')
    restored_status='failed' if quality.get('errors') else 'needs_review' if quality.get('quality_flags') else 'ready'
    with s.conn() as c: c.execute("UPDATE documents SET status=? WHERE id=?",(restored_status,did))
    value=s.index_document(Path(d["path"]),d["name"])
    rag_index.submit_index([did])
    return value
@app.post("/api/documents/{did}/reparse")
def reparse_doc(did: str, req: PageRequest):
    value=document_engine.reparse(did,req.page,req.version,req.parse_id)
    rag_index.submit_index([did])
    return value
@app.get("/api/documents/{did}/reparses")
def reparses(did: str):
    with s.conn() as c: return [json.loads(r[0]) for r in c.execute("SELECT body FROM reparses WHERE document_id=? ORDER BY created_at DESC",(did,))]

@app.get("/api/definitions")
def definitions():
    return {"items":s.definition_list(),"builtins":[
        {"name":"净销售额","description":"订单含税销售额 − 已确认退货金额","source":"指标口径字典.md","unit":"元"},
        {"name":"销售冠军","description":"当前时间与区域内，净销售额最高的产品；并列时全部列出","source":"当前工作区口径","unit":"产品"},
        {"name":"增长率","description":"（本期 − 对比期）÷ 对比期；基期应大于零","source":"指标口径字典.md","unit":"%"},
    ],"rules":s.read("SELECT * FROM business_rules ORDER BY effective_from")[0],"products":engine.vocab()[0],"metrics":engine.METRICS}
@app.post("/api/definitions")
def define(req: DefinitionRequest): return s.save_definition(req.model_dump(exclude_none=True))
@app.delete("/api/definitions/{did}")
def delete_definition(did: str):
    with s.conn() as c: c.execute("UPDATE definitions SET deleted=1 WHERE id=?",(did,))
    return {"ok":True}

@app.post("/api/query")
def query(req: QueryRequest): return run(req)
@app.post("/api/query/recompute")
def recompute(req: RecomputeRequest):
    old=s.get_run(req.run_id) if req.run_id else None
    spec={**(old["query_spec"] if old else {}),**req.overrides}
    if conversations.is_database_analysis(spec) or spec.get('rag_scope') or spec.get('metric')=='document' or (old and conversations.is_database_analysis(old)):
        raise ValueError("数据库问数请使用自然语言追问修改范围或刷新结果，不能套用销售条件重算")
    # Recomputing the same question must retain ALL confirmed conditions.
    result=agent.execute(old["question"] if old else req.question,{},spec,database_context={'query_scope':'sales','database_connection_id':None})
    result["parent_run_id"]=req.run_id
    result["changes"]=[{"key":k,"before":old["query_spec"].get(k) if old else None,"after":v} for k,v in req.overrides.items()]
    s.snapshot_run(result)
    return result

@app.post("/api/tasks")
def create_task(req: QueryRequest): return conversations.manager.submit(req.model_dump())
@app.get("/api/tasks/{tid}")
def task(tid: str): return conversations.manager.get(tid)
@app.post("/api/tasks/{tid}/cancel")
def cancel_task(tid: str): return conversations.manager.cancel(tid)

@app.get("/api/saved")
def saved(deleted: bool=False):
    with s.conn() as c:
        rows=c.execute("SELECT * FROM saved_analyses WHERE deleted_at IS "+("NOT NULL" if deleted else "NULL")+" ORDER BY updated_at DESC").fetchall()
    items=[];cache={}
    for row in rows:
        result=json.loads(row['result_json'])
        state=conversations.analysis_snapshot_state(result,cache)
        items.append({k:v for k,v in dict(row).items() if k!='result_json'}|{'result':result,**state})
    return items
@app.post("/api/saved")
def save(req: SaveRequest):
    name=req.name.strip()
    if not name: raise ValueError("名称不能为空")
    result=s.get_run(req.run_id); sid=str(uuid.uuid4()); stamp=s.now()
    with s.conn() as c:
        c.execute("INSERT INTO saved_analyses (id,name,question,result_json,created_at,updated_at,deleted_at,snapshot_version) VALUES (?,?,?,?,?,?,NULL,?)",(sid,name,result["question"],json.dumps(result,ensure_ascii=False),stamp,stamp,result["data_version"]))
    return {"id":sid,"ok":True}
@app.patch("/api/saved/{sid}")
def rename(sid: str, req: RenameRequest):
    if not req.name.strip(): raise ValueError("名称不能为空")
    with s.conn() as c: c.execute("UPDATE saved_analyses SET name=?,updated_at=? WHERE id=?",(req.name.strip(),s.now(),sid))
    return {"ok":True}
@app.delete("/api/saved/{sid}")
def trash(sid: str):
    with s.conn() as c: c.execute("UPDATE saved_analyses SET deleted_at=? WHERE id=?",(s.now(),sid))
    return {"ok":True,"recoverable":True}
@app.post("/api/saved/{sid}/restore")
def restore(sid: str):
    with s.conn() as c: c.execute("UPDATE saved_analyses SET deleted_at=NULL WHERE id=?",(sid,))
    return {"ok":True}

@app.post("/api/import")
async def import_file(file: UploadFile=File(...)):
    name=Path(file.filename or "upload").name
    suffix=Path(name).suffix.lower()
    if suffix not in {".csv",".xlsx",".sqlite",".db",".md",".txt",".pdf",".docx",".png",".jpg",".jpeg",".webp"}: raise ValueError("支持 CSV、XLSX、SQLite、DOCX、PDF、TXT、Markdown 和图片")
    content=await file.read(50*1024*1024+1)
    if len(content)>50*1024*1024: raise HTTPException(413,"文件超过 50 MB")
    if not content: raise ValueError("文件为空，请选择包含数据的文件")
    dest=s.UPLOADS/(uuid.uuid4().hex[:10]+"_"+name); dest.write_bytes(content)
    if suffix in {".md",".txt",".pdf",".docx",".png",".jpg",".jpeg",".webp"}:
        d=s.index_document(dest,name)
        rag_index.submit_index([d['id']])
        return {"ok":d["status"]!="failed","kind":"document","document":d,"message":"解析失败，原文件已保留，可查看原因后重试" if d["status"]=="failed" else "文档已导入"}
    import pandas as pd
    frames={};source_tables=[]
    try:
        if suffix==".csv":
            frames[Path(name).stem]=pd.read_csv(dest)
        elif suffix==".xlsx": frames=pd.read_excel(dest,sheet_name=None)
        else:
            c=sqlite3.connect(s.readonly_uri(dest),uri=True)
            try:
                c.execute('PRAGMA query_only=ON');c.execute('PRAGMA trusted_schema=OFF')
                source_names=c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
                for row in source_names:
                    source_tables.append(database_query._sqlite_metadata(c,{'schema':'main','name':row[0]},{}))
                    frames[row[0]]=pd.read_sql_query("SELECT * FROM "+s.quoted(row[0]),c)
            finally: c.close()
        if not frames: raise ValueError("文件中没有数据表")
        prepared=[];mapping={};column_mapping={}
        for original,df in frames.items():
            if len(df)>200000: raise ValueError("单表超过 20 万行")
            if not len(df.columns): raise ValueError("数据表没有字段")
            columns=[str(x).strip() for x in df.columns]
            if any(not x or "\x00" in x for x in columns) or len({x.casefold() for x in columns})!=len(columns):
                raise ValueError("字段名称为空或重复")
            base=re.sub(r"\W+","_",original).strip("_")[:50] or "imported"
            table="import_"+base+"_"+uuid.uuid4().hex[:6]
            types=["INTEGER" if pd.api.types.is_integer_dtype(t) or pd.api.types.is_bool_dtype(t) else "REAL" if pd.api.types.is_numeric_dtype(t) else "TEXT" for t in df.dtypes]
            def scalar(value):
                if pd.isna(value): return None
                if hasattr(value,"item"): value=value.item()
                return value if isinstance(value,(str,int,float,bytes)) else str(value)
            values=[tuple(scalar(value) for value in row) for row in df.itertuples(index=False,name=None)]
            prepared.append((table,columns,types,values))
            original_key='main.'+str(original)
            mapping[original_key]=table
            column_mapping[original_key]={str(old):new for old,new in zip(df.columns,columns)}
            if suffix not in {'.sqlite','.db'}:
                source_tables.append({'schema':'main','name':str(original),'qualified_name':original_key,
                                      'columns':[{'name':str(old),'type':kind} for old,kind in zip(df.columns,types)],
                                      'primary_key':[],'unique_keys':[],'foreign_keys':[]})
        snapshot={'tables':source_tables,'source':{'connection_id':None,'revision':None,
                  'fingerprint':hashlib.sha256(json.dumps(source_tables,ensure_ascii=False,sort_keys=True).encode()).hexdigest()}}
        metadata=connections.import_metadata(snapshot,mapping,column_mapping)
        # All sheets and the catalog participate in one transaction; no partial
        # import is exposed if a later sheet is invalid or a write fails.
        c=sqlite3.connect(s.BUSINESS_DB,timeout=8)
        try:
            c.execute("ATTACH DATABASE ? AS catalog",(str(s.APP_DB),))
            c.execute("BEGIN IMMEDIATE")
            c.execute('CREATE TABLE IF NOT EXISTS catalog.ef_database_import_metadata(name TEXT PRIMARY KEY,body TEXT NOT NULL)')
            for table,columns,types,values in prepared:
                fields=", ".join(s.quoted(col)+" "+kind for col,kind in zip(columns,types))
                c.execute("CREATE TABLE "+s.quoted(table)+" ("+fields+")")
                c.executemany("INSERT INTO "+s.quoted(table)+" VALUES ("+",".join("?" for _ in columns)+")",values)
                c.execute("INSERT INTO catalog.table_catalog VALUES (?,?,?,0)",(table,name,s.now()))
                c.execute('INSERT INTO catalog.ef_database_import_metadata VALUES (?,?)',(table,json.dumps(metadata[table],ensure_ascii=False)))
            c.commit()
        except Exception:
            c.rollback(); raise
        finally: c.close()
        names=[item[0] for item in prepared]
    except pd.errors.EmptyDataError: raise ValueError("文件没有表头或数据，请检查后重新导入")
    except UnicodeDecodeError: raise ValueError("无法读取文字编码，请将文件另存为 UTF-8 CSV 后重试")
    except Exception as exc: raise ValueError("表格解析失败："+str(exc)[:200])
    return {"ok":True,"kind":"table","tables":names,"message":"已导入 "+str(len(names))+" 张表，原表未覆盖"}

@app.get("/api/evaluation")
def evaluation():
    from .evaluation import smoke
    return smoke()
@app.get("/api/features")
def features(): return {"flags":engine.FEATURES,"scope":"开关仅供独立测试使用；日常查询启用全部检查"}
DIST=s.ROOT/"frontend"/"dist"
if DIST.exists():
    app.mount("/assets",StaticFiles(directory=DIST/"assets"),name="assets")
@app.get("/{path:path}")
def frontend(path: str):
    if path.startswith("api/"): raise HTTPException(404,"接口不存在")
    if not (DIST/"index.html").exists(): raise HTTPException(503,"前端尚未构建")
    asset=(DIST/path).resolve()
    if asset.is_relative_to(DIST.resolve()) and asset.is_file() and asset.name!="index.html":
        return FileResponse(asset)
    return FileResponse(DIST/"index.html",headers={"Cache-Control":"no-cache"})
