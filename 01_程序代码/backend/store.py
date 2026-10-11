"""Local catalog, immutable source revisions, and user-owned analysis snapshots."""
from __future__ import annotations
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
DATA = Path(os.environ.get("EVIDENCEFLOW_DATA_DIR", str(PROJECT_ROOT / "02_数据资料" / "运行数据"))).resolve()
DOCS, UPLOADS, VERSIONS = (DATA / n for n in ("docs", "uploads", "versions"))
APP_DB, BUSINESS_DB = DATA / "app.db", DATA / "business.db"
BASE_TABLES = {"orders", "products", "customers", "projects", "targets", "business_rules"}
_query_state = threading.local()

@contextmanager
def query_cancellation(event):
    previous = getattr(_query_state, "cancelled", None)
    _query_state.cancelled = event
    try: yield
    finally: _query_state.cancelled = previous

def now(): return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try: return super().__exit__(*args)
        finally: self.close()

def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""): h.update(chunk)
    return h.hexdigest()
def conn():
    c = sqlite3.connect(APP_DB, timeout=8, factory=ClosingConnection)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c
def quoted(name): return '"' + name.replace('"', '""') + '"'
def readonly_uri(path): return Path(path).resolve().as_uri() + "?mode=ro"
def read(sql, params=(), limit=10000):
    c = sqlite3.connect(readonly_uri(BUSINESS_DB), uri=True, timeout=3)
    c.row_factory = sqlite3.Row
    deadline = time.monotonic() + 5
    stop = getattr(_query_state, "cancelled", None)
    c.set_progress_handler(lambda: int(time.monotonic() > deadline or bool(stop and stop.is_set())), 1000)
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
    c.set_authorizer(lambda action, a, b, db, source: sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY)
    try:
        cur = c.execute(sql, params)
        rows = cur.fetchmany(limit + 1)
        return [dict(r) for r in rows[:limit]], len(rows) > limit
    except sqlite3.OperationalError:
        if stop and stop.is_set(): raise InterruptedError("查询已取消") from None
        raise
    finally: c.close()
def init():
    for p in [DATA, DOCS, UPLOADS, VERSIONS]: p.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,name TEXT NOT NULL,path TEXT NOT NULL,sha256 TEXT NOT NULL,format TEXT,pages INTEGER,status TEXT,quality_json TEXT,created_at TEXT,updated_at TEXT);
        CREATE TABLE IF NOT EXISTS blocks(id TEXT PRIMARY KEY,document_id TEXT,page INTEGER,title_path TEXT,content_type TEXT,text TEXT,bbox_json TEXT,quality_json TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,question TEXT,status TEXT,created_at TEXT,result_json TEXT);
        CREATE TABLE IF NOT EXISTS confirmations(id TEXT PRIMARY KEY,run_id TEXT,field TEXT,value TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS saved_analyses(id TEXT PRIMARY KEY,name TEXT NOT NULL,question TEXT NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS definitions(id TEXT PRIMARY KEY,body TEXT NOT NULL,updated_at TEXT NOT NULL,deleted INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS table_catalog(name TEXT PRIMARY KEY,source TEXT,updated_at TEXT,archived INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS reparses(id TEXT PRIMARY KEY,document_id TEXT,page INTEGER,created_at TEXT,body TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        """)
        cols = {r[1] for r in c.execute("PRAGMA table_info(saved_analyses)")}
        if "deleted_at" not in cols: c.execute("ALTER TABLE saved_analyses ADD COLUMN deleted_at TEXT")
        if "snapshot_version" not in cols: c.execute("ALTER TABLE saved_analyses ADD COLUMN snapshot_version TEXT")
        from . import documents
        documents.initialize(c)

def docrow(r):
    d = dict(r)
    d["quality"] = json.loads(d.pop("quality_json") or "{}")
    d.pop("path", None)
    d["source_url"] = f"/api/documents/{d['id']}/original?version={d['sha256']}"
    provenance=d['quality'].get('source_provenance')
    if provenance and provenance.get('file_sha256') == d['sha256']:
        d['provenance']=provenance
        d['official_url']=provenance.get('official_url')
    d["version"] = d["sha256"][:10]
    return d


def retain_provenance(quality, row):
    """Parsing can change recognition quality, never the matching file origin."""
    old=json.loads(row['quality_json'] or '{}')
    provenance=old.get('source_provenance')
    if provenance and provenance.get('file_sha256') == row['sha256']:
        quality={**quality,'source_provenance':provenance}
    return quality

def doc_list(archived=False):
    with conn() as c:
        return [docrow(r) for r in c.execute("SELECT * FROM documents WHERE status "+("= 'archived'" if archived else "!= 'archived'")+" ORDER BY name")]
def doc_get(did):
    with conn() as c:
        r = c.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
    if r is None: raise ValueError("文档不存在")
    result = dict(r)
    path = Path(result["path"])
    result["path"] = str(path if path.is_absolute() else DATA / path)
    return result
def blocks(did, page=None):
    with conn() as c:
        sql="SELECT * FROM blocks WHERE document_id=?"
        p=[did]
        if page is not None: sql+=" AND page=?"; p.append(page)
        rows=c.execute(sql+" ORDER BY page,id", p).fetchall()
    return [{**dict(r), "bbox":json.loads(r["bbox_json"]), "quality":json.loads(r["quality_json"])} for r in rows]

def quality_for_parse(body, errors=()):
    pages=body.get("pages",[])
    missing=[p["number"] for p in pages if p.get("ocr_required")]
    issues=[issue for p in pages for issue in p.get("issues",[])]
    pending=sum(1 for p in pages for key in ("tables","charts") for obj in p.get(key,[]) if obj.get("status") in {"pending","needs_review"})
    pending_media=sum(1 for p in pages for img in p.get("images",[]) if img.get("status") in {"pending","needs_review"})
    pending_lines=sum(len(p.get('recognition_pending',[])) for p in pages)
    flags=[]
    if errors: flags.append("解析失败")
    if missing: flags.append("需要文字识别")
    if issues or pending or pending_media or pending_lines: flags.append("结构或识别待复核")
    return {"parser_version":3,"parser":body.get("parser"),"block_count":sum(len(p.get("blocks",[])) for p in pages),
            "visual_blocks":sum(len(p.get("images",[])) for p in pages),"quality_flags":flags,"errors":list(errors),
            "structure":body.get("structure","按页"),"missing_pages":missing,"issues":issues,"pending_structures":pending,"pending_media":pending_media,
            "confirmed":not bool(missing or issues or pending or pending_media or pending_lines or errors),"pending_text_lines":pending_lines,"human_reviewed":False,
            "confirmed_blocks":sum(1 for p in pages for b in p.get("blocks",[]) if b.get("status","confirmed")=="confirmed"),
            "methods":sorted({p.get("provenance",{}).get("method",p.get("method","native_text")) for p in pages}|{b["source_method"] for p in pages for b in p.get("blocks",[]) if b.get("source_method")}),"location_precision":"mixed"}


def extract(path, body=None):
    from . import documents
    body=body or documents._parse_file(Path(path))
    out=[]
    for p in body["pages"]:
        for b in p.get("blocks",[]):
            out.append({"page":p["number"],"text":b["text"],"title_path":b.get("title_path",""),
                        "content_type":b.get("kind","text"),"bbox":b.get("box"),
                        "quality":{"text_extracted":bool(b["text"].strip()),"ocr_required":p.get("ocr_required",False),
                                   "location_precision":b.get("location_precision",p.get("location_precision","text_range")),"method":b.get("source_method",p.get("provenance",{}).get("method",p.get("method","native_text")))}})
        if not p.get("blocks"):
            out.append({"page":p["number"],"text":"","title_path":"","content_type":"image" if p.get("images") else "text", "bbox":None,
                        "quality":{"text_extracted":False,"ocr_required":p.get("ocr_required",False),"vision_verified":False,"location_precision":"page"}})
    return out


def index_document(path, name=None):
    path=Path(path).resolve()
    if not path.is_relative_to(DATA): raise ValueError("只能索引本项目的数据")
    relative=path.relative_to(DATA).as_posix();sha=digest(path)
    did=hashlib.sha256(relative.encode()).hexdigest()[:20]
    from . import documents
    with conn() as c:
        old=c.execute("SELECT * FROM documents WHERE path IN (?,?)",(str(path),relative)).fetchone()
        if old:
            documents.register(c,old);did=old["id"]
            if old["status"]=="archived": return docrow(old)
            if old["sha256"]==sha and json.loads(old["quality_json"] or "{}").get("parser_version")==3: return docrow(old)
    # Parsing is deliberately outside catalog transactions and does not run OCR/network calls.
    body=None
    try:
        body=documents._parse_file(path);pieces=extract(path,body);errors=[]
    except Exception as exc:
        pieces=[];errors=[str(exc)[:300]]
    if digest(path)!=sha: raise ValueError("原文件在解析期间已改变，请重新导入")
    quality=quality_for_parse(body or {},errors)
    version_file=VERSIONS/(sha+path.suffix.lower())
    if not version_file.exists(): version_file.write_bytes(path.read_bytes())
    if digest(version_file)!=sha: raise ValueError("原件版本摘要不一致，未发布解析")
    stamp=now();status="failed" if errors else "needs_review" if quality["quality_flags"] else "ready"
    with conn() as c:
        current=c.execute("SELECT * FROM documents WHERE path IN (?,?)",(str(path),relative)).fetchone()
        if current and current["status"]=="archived": return docrow(current)
        if current and current["sha256"]==sha and json.loads(current["quality_json"] or "{}").get("parser_version")==3: return docrow(current)
        if current and old and current["sha256"]!=old["sha256"]: raise ValueError("文档已被另一导入更新，请重试")
        c.execute("INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,?,?,?)",(did,name or (old["name"] if old else path.name),relative,sha,path.suffix.lower().lstrip("."),len(body["pages"]) if body else 1,status,json.dumps(quality,ensure_ascii=False),old["created_at"] if old else stamp,stamp))
        documents.register(c,c.execute("SELECT * FROM documents WHERE id=?",(did,)).fetchone())
        c.execute("DELETE FROM blocks WHERE document_id=?",(did,))
        for i,b in enumerate(pieces):
            c.execute("INSERT INTO blocks VALUES (?,?,?,?,?,?,?,?,?)",(did+"-"+str(i),did,b["page"],b["title_path"],b["content_type"],b["text"],json.dumps(b["bbox"]),json.dumps(b["quality"],ensure_ascii=False),stamp))
        if body: documents.save_parse(c,did,sha,body,"原生格式解析")
    return docrow(doc_get(did))


def data_version():
    h=hashlib.sha256(digest(BUSINESS_DB).encode())
    with conn() as c:
        for r in c.execute("SELECT id,sha256,status,quality_json FROM documents ORDER BY id"): h.update(json.dumps(tuple(r)).encode())
        for r in c.execute("SELECT id,body,deleted FROM definitions ORDER BY id"): h.update(json.dumps(tuple(r)).encode())
        for r in c.execute("SELECT name,archived FROM table_catalog ORDER BY name"): h.update(json.dumps(tuple(r)).encode())
    return h.hexdigest()[:20]

def tables():
    c=sqlite3.connect(readonly_uri(BUSINESS_DB),uri=True)
    c.row_factory=sqlite3.Row
    with conn() as ac:
        metadata={r["name"]:dict(r) for r in ac.execute("SELECT * FROM table_catalog")}
        provenance={r['table_name']:json.loads(r['body']) for r in ac.execute('SELECT * FROM ef_import_provenance')} if ac.execute("SELECT 1 FROM sqlite_master WHERE name='ef_import_provenance'").fetchone() else {}
    result=[]
    for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
        n=r["name"]; meta=metadata.get(n,{})
        if meta.get("archived"): continue
        cols=[{"name":r["name"],"type":r["type"],"primary_key":bool(r["pk"])} for r in c.execute(f"PRAGMA table_info({quoted(n)})")]
        relations=[dict(r) for r in c.execute(f"PRAGMA foreign_key_list({quoted(n)})")]
        if n=="orders":
            relations=[{"from":k,"table":table,"to":k,"source":"工作区配置，运行时检查唯一性"} for k,table in [("product_id","products"),("customer_id","customers"),("project_id","projects")]]
        result.append({"name":n,"columns":cols,"sample":[dict(r) for r in c.execute(f"SELECT * FROM {quoted(n)} LIMIT 3")],"row_count":c.execute(f"SELECT COUNT(*) FROM {quoted(n)}").fetchone()[0],"relations":relations,"updated_at":meta.get("updated_at") or datetime.fromtimestamp(BUSINESS_DB.stat().st_mtime,timezone.utc).isoformat(),"source":meta.get("source","示例数据"),"removable":n not in BASE_TABLES,"provenance":provenance.get(n)})
    c.close()
    return result
def definition_list():
    with conn() as c: return [json.loads(r["body"])|{"id":r["id"],"updated_at":r["updated_at"]} for r in c.execute("SELECT * FROM definitions WHERE deleted=0 ORDER BY updated_at")]
def save_definition(body):
    body=dict(body)
    body['name']=str(body.get('name','')).strip()
    aliases=body.get('aliases',[])
    if not isinstance(aliases,list) or any(not isinstance(x,str) for x in aliases): raise ValueError('别名必须是文字列表')
    body['aliases']=list(dict.fromkeys(x.strip() for x in aliases if x.strip() and x.strip()!=body['name']))
    if len(body['name'])>60 or len(body['aliases'])>10 or any(len(x)>60 for x in body['aliases']): raise ValueError('名称最多60字，别名最多10个且每项最多60字')
    allowed={"alias","metric","rule"}
    if body.get("kind") not in allowed: raise ValueError("请选择别名、指标或规则")
    if not str(body.get("name","")).strip(): raise ValueError("请填写名称")
    if body["kind"]=="alias":
        if body.get("field") not in {"region","product","metric"} or not body.get("target"): raise ValueError("请填写有效的匹配字段和目标")
        options={"region":["华东","华北","华南","西南"],"product":[x["product_name"] for x in read("SELECT product_name FROM products")[0]],"metric":["net_sales","gross_sales","count","growth_rate"]}
        if body["target"] not in options[body["field"]]: raise ValueError("目标不在当前工作区中")
    if body["kind"]=="metric" and body.get("target") not in {"net_sales","gross_sales","count","growth_rate"}: raise ValueError("请选择已支持的计算口径")
    if body["kind"]=="rule":
        from decimal import Decimal, InvalidOperation
        try:
            if Decimal(str(body["threshold"]))<0 or not (0<=Decimal(str(body["rate"]))<=1): raise ValueError("门槛不能小于零，比例应在 0 和 1 之间")
            datetime.strptime(body["effective_from"],"%Y-%m-%d")
            if body.get("effective_to"):
                datetime.strptime(body["effective_to"],"%Y-%m-%d")
                if body["effective_to"]<body["effective_from"]: raise ValueError("失效日期早于生效日期")
        except (KeyError,InvalidOperation): raise ValueError("请填写有效门槛、比例和生效日期")
    did=body.get("id") or str(uuid.uuid4()); body={**body,"id":did}
    with conn() as c:
        for r in c.execute("SELECT id,body FROM definitions WHERE deleted=0 AND id!=?",(did,)):
            d=json.loads(r["body"])
            names={str(x).casefold() for x in [d.get('name','')]+d.get('aliases',[])}
            incoming={str(x).casefold() for x in [body['name']]+body['aliases']}
            if names & incoming: raise ValueError('名称或别名已被“'+d.get('name','')+'”使用，请编辑原定义')
        if body['kind'] in {'alias','metric'}:
            builtin={'华东':('region','华东'),'华北':('region','华北'),'华南':('region','华南'),'西南':('region','西南'),'净销售额':('metric','net_sales'),'毛销售额':('metric','gross_sales'),'增长率':('metric','growth_rate')}
            builtin.update({r['product_name']:('product',r['product_name']) for r in read('SELECT product_name FROM products')[0]})
            for label in [body['name']]+body['aliases']:
                if label in builtin and builtin[label]!=(body.get('field','metric'),body['target']):raise ValueError('别名与内置口径“'+label+'”冲突')
        c.execute('CREATE TABLE IF NOT EXISTS definition_versions(definition_id TEXT,version INTEGER,body TEXT,created_at TEXT,PRIMARY KEY(definition_id,version))')
        old=c.execute('SELECT body FROM definitions WHERE id=?',(did,)).fetchone()
        last=c.execute('SELECT MAX(version) FROM definition_versions WHERE definition_id=?',(did,)).fetchone()[0] or 0
        if old and not last:
            c.execute('INSERT INTO definition_versions VALUES (?,?,?,?)',(did,1,old[0],now()));last=1
        c.execute('INSERT INTO definition_versions VALUES (?,?,?,?)',(did,last+1,json.dumps(body,ensure_ascii=False),now()))
        c.execute("INSERT OR REPLACE INTO definitions VALUES (?,?,?,0)",(did,json.dumps(body,ensure_ascii=False),now()))
    return body
def snapshot_run(result):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)",(result["run_id"],result["question"],result["status"],now(),json.dumps(result,ensure_ascii=False)))
def get_run(rid):
    with conn() as c: row=c.execute("SELECT result_json FROM runs WHERE id=?",(rid,)).fetchone()
    if not row: raise ValueError("查询记录不存在")
    return json.loads(row[0])
