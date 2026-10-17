"""Version-pinned RAG chunks and immutable SQLite FTS5 publications.

Only this module writes derived document indexes. Existing blocks and original
document parses are retained. Model calls happen after catalog transactions.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import documents, store as s

CHUNKER_VERSION = "source-spans-v6-atomic-chart-points"
CHUNK_TOKENS = 384
OVERLAP_TOKENS = 48
LOCK = threading.RLock()
POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rag-index")
_initialized = set()
_tokenizer = None
_lexical_config = None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _guard(cancelled=None, deadline=None):
    if cancelled is not None and cancelled.is_set():
        raise InterruptedError("检索索引已取消")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("检索索引超过共享总时限")


def initialize():
    key = str(s.APP_DB.resolve())
    with LOCK:
        if key in _initialized and s.APP_DB.exists():
            return
        with s.conn() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS ef_rag_generations(
                id TEXT PRIMARY KEY,document_id TEXT NOT NULL,sha256 TEXT NOT NULL,
                parse_id TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS ef_rag_generation_doc ON ef_rag_generations(document_id,active);
              CREATE TABLE IF NOT EXISTS ef_rag_chunks(
                id TEXT PRIMARY KEY,generation_id TEXT NOT NULL,document_id TEXT NOT NULL,
                sha256 TEXT NOT NULL,parse_id TEXT NOT NULL,page INTEGER NOT NULL,
                start INTEGER NOT NULL,end INTEGER NOT NULL,text TEXT NOT NULL,
                title_path TEXT NOT NULL,content_type TEXT NOT NULL,body TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS ef_rag_chunk_generation ON ef_rag_chunks(generation_id,page,start);
              CREATE TABLE IF NOT EXISTS ef_rag_embeddings(
                chunk_id TEXT NOT NULL,fingerprint TEXT NOT NULL,dimension INTEGER NOT NULL,
                input_sha256 TEXT NOT NULL,vector BLOB NOT NULL,created_at TEXT NOT NULL,
                PRIMARY KEY(chunk_id,fingerprint));
              CREATE TABLE IF NOT EXISTS ef_rag_lexical_manifests(
                id TEXT PRIMARY KEY,filename TEXT NOT NULL,sha256 TEXT NOT NULL,
                body TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS ef_rag_index_jobs(
                id TEXT PRIMARY KEY,status TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL);
            """)
            for row in db.execute("SELECT id,body FROM ef_rag_index_jobs WHERE status IN ('pending','indexing')").fetchall():
                body = json.loads(row["body"])
                body.update(status="interrupted", error="程序重启，未假定原索引任务已完成", recovered_at=s.now())
                db.execute("UPDATE ef_rag_index_jobs SET status='interrupted',body=? WHERE id=?", (_json(body), row["id"]))
        _initialized.add(key)


def reader():
    db = sqlite3.connect(s.readonly_uri(s.APP_DB), uri=True, timeout=5, factory=s.ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def lexical_config():
    global _tokenizer, _lexical_config
    with LOCK:
        if _lexical_config is None:
            try:
                import jieba
                version = importlib.metadata.version("jieba")
            except (ImportError, importlib.metadata.PackageNotFoundError):
                raise ValueError("中文关键词索引需要封装的 jieba 0.42.1；未生成假索引") from None
            if version != "0.42.1":
                raise ValueError("中文分词器版本不匹配，需要 jieba 0.42.1")
            tokenizer = jieba.Tokenizer()
            # jieba's generated cache belongs outside the portable workspace.
            import tempfile
            tokenizer.tmp_dir = tempfile.gettempdir()
            tokenizer.initialize()
            dictionary = Path(jieba.__file__).parent / "dict.txt"
            _tokenizer = tokenizer
            _lexical_config = {"tokenizer": "jieba", "version": version,
                               "dictionary_sha256": s.digest(dictionary), "hmm": False,
                               "normalization": "NFKC-casefold-v1", "unknown_bigrams": True,
                               "fts_tokenizer": "unicode61", "weights": [1.0, 1.5, 0.20]}
        return dict(_lexical_config)


def lexical_tokens(text):
    lexical_config()
    text = unicodedata.normalize("NFKC", text).casefold()
    tokens = []
    # Protect labels and identifiers; all final MATCH syntax is server-owned.
    for part in re.findall(r"[a-z0-9_]+(?:[.\-/][a-z0-9_]+)*|[\u4e00-\u9fff]+", text):
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            tokens.extend(word for word in _tokenizer.cut(part, HMM=False) if word.strip())
        else:
            tokens.append(part)
    bigrams = [text[i:i+2] for i in range(len(text)-1)
               if re.fullmatch(r"[\u4e00-\u9fff]{2}", text[i:i+2])]
    return tokens, bigrams


def match_query(text):
    main, grams = lexical_tokens(text)
    def quote(token):
        return '"' + token.replace('"', '""') + '"'
    # A bounded OR query keeps exact identifiers as one phrase and cannot inject
    # FTS column syntax or operators from user/model input.
    main = list(dict.fromkeys(main))[:80]
    grams = list(dict.fromkeys(grams))[:120]
    parts = ["tokens:" + quote(t) for t in main]
    parts += ["title_tokens:" + quote(t) for t in main]
    parts += ["bigrams:" + quote(t) for t in grams]
    return " OR ".join(parts)


def _counter():
    try:
        from . import rag_providers
        info = rag_providers.describe("embedding")
        if info.get("ready") and info.get("local"):
            # Probe once: a missing tokenizer is not silently called token-based.
            rag_providers.token_count("测试", role="embedding", strict=True)
            return lambda text: rag_providers.token_count(text, role="embedding", strict=True), "model_tokenizer", min(CHUNK_TOKENS, max(64, int(info.get("max_tokens", 512))-96))
    except (ImportError, ValueError, OSError):
        pass
    return len, "conservative_unicode_characters", CHUNK_TOKENS


def _split(text, count, budget):
    """Keep paragraph/sentence endings when possible, never modify page text."""
    boundaries = sorted(set([m.end() for m in re.finditer(r"\n\s*\n|[。！？；.!?;](?:\s|$)|\n", text)] + [len(text)]))
    start = 0
    while start < len(text):
        while start < len(text) and text[start].isspace():
            start += 1
        if start == len(text):
            break
        lo, hi = start+1, min(len(text), start+budget*4)
        while lo < hi:
            mid = (lo+hi+1)//2
            if count(text[start:mid]) <= budget:
                lo = mid
            else:
                hi = mid-1
        end = lo
        eligible = [b for b in boundaries if start < b <= end]
        if eligible and eligible[-1]-start >= min(40, budget//4):
            end = eligible[-1]
        while end > start and text[end-1].isspace():
            end -= 1
        if end <= start:
            end = min(start+1, len(text))
        hard_split = end < len(text) and end not in boundaries and not text[end:end+1].isspace()
        yield start, end, hard_split
        if end == len(text):
            break
        # Complete-sentence overlap is preferred; do not repeat indefinitely.
        overlap = [b for b in boundaries if start < b < end and count(text[b:end]) <= OVERLAP_TOKENS]
        next_start = overlap[0] if overlap else end
        start = next_start if next_start > start else end


def _page_title(data, page, start):
    path = ""
    for block in page["blocks"]:
        if block.get("start", 0) <= start and block.get("title_path"):
            path = block["title_path"]
    if path:
        return path
    titles = []
    for heading in data.get("outline", []):
        if heading.get("page", 1) <= page["number"]:
            level = int(heading.get("level", 1))
            titles = titles[:max(0, level-1)] + [heading["title"]]
    return " / ".join(titles)


def _confirmed_ranges(page):
    """Keep continuous source offsets; uncertain observations never bridge chunks."""
    text=page["text"]
    excluded=sorted((max(0,b.get("start",0)), min(len(text),b.get("end",0)))
                    for b in page.get("blocks",[]) if b.get("status","confirmed")!="confirmed")
    cursor=0
    for start,end in excluded:
        if start>cursor:yield cursor,start
        cursor=max(cursor,end)
    if cursor<len(text):yield cursor,len(text)



def _formula_blocks(page):
    out=[]
    for block in page.get('blocks',[]):
        if block.get('status','confirmed')!='confirmed':continue
        if block.get('formula_ranges'):
            for interval in block['formula_ranges']:
                start,end=interval['start'],interval['end']
                out.append({**block,'id':block['id']+'-'+interval['formula_id'],'start':start,'end':end,
                            'text':page['text'][start:end],'formula_ids':[interval['formula_id']]})
        elif block.get('formula_ids') or block.get('formula_id') or block.get('kind')=='formula':out.append(block)
    return sorted(out,key=lambda b:b['start'])


def _table_row_blocks(page):
    """Only source-bound, confirmed row views carry relational evidence."""
    return [block for block in page.get('blocks',[])
            if block.get('kind')=='table_row' and block.get('status','confirmed')=='confirmed'
            and block.get('cell_bindings') and block.get('table_relation_version')]


def _atomic_blocks(page):
    """Overlapping formula/row intervals form one indivisible source interval."""
    blocks=[{'start':b['start'],'end':b['end'],'formula_block_ids':[b['id']],
             'table_row_ids':[],'table_ids':[],'chart_point_ids':[],'chart_ids':[]} for b in _formula_blocks(page)]
    blocks += [{'start':b['start'],'end':b['end'],'formula_block_ids':[],
                'table_row_ids':[b['id']],'table_ids':[b['table_id']] if b.get('table_id') else [],
                'chart_point_ids':[],'chart_ids':[]}
               for b in _table_row_blocks(page)]
    blocks += [{'start':b['start'],'end':b['end'],'formula_block_ids':[],
                'table_row_ids':[],'table_ids':[], 'chart_point_ids':[b.get('point_id',b['id'])],
                'chart_ids':[b['chart_id']] if b.get('chart_id') else []}
               for b in page.get('blocks',[]) if (b.get('chart_id') or b.get('kind')=='chart')
               and b.get('status','confirmed')=='confirmed']
    protected=[]
    for block in sorted(blocks,key=lambda b:(b['start'],b['end'])):
        if protected and block['start']<protected[-1]['end']:
            previous=protected[-1]
            previous['end']=max(previous['end'],block['end'])
            for key in ('formula_block_ids','table_row_ids','table_ids','chart_point_ids','chart_ids'):
                previous[key]=list(dict.fromkeys(previous[key]+block[key]))
        else:protected.append(block)
    return protected


def _source_spans(page,count,budget):
    """Whole formulas/related table rows only; omitted atoms remain in the source."""
    protected=_atomic_blocks(page)
    for offset,limit in _confirmed_ranges(page):
        cursor=offset
        for block in protected:
            start,end=block['start'],block['end']
            if end<=cursor or start>=limit:continue
            if start>cursor:
                for a,b,hard in _split(page['text'][cursor:start],count,budget):yield cursor+a,cursor+b,hard
            # If a pending source interval cuts through an atom, neither side
            # may masquerade as the complete relation or formula.
            if start>=offset and end<=limit and count(page['text'][start:end])<=budget:
                yield start,end,False
            cursor=max(cursor,min(end,limit))
        for a,b,hard in _split(page['text'][cursor:limit],count,budget):yield cursor+a,cursor+b,hard


def _extraction_quality(page,start,end):
    blocks=[b for b in page.get("blocks",[]) if b.get("end",0)>start and b.get("start",0)<end]
    provenance=page.get("provenance",{})
    methods=sorted({b.get("source_method") or provenance.get("method") or page.get("method","native_text") for b in blocks})
    precisions=sorted({b.get("location_precision") or provenance.get("location_precision") or page.get("location_precision","character") for b in blocks})
    quality={"confirmed":all(b.get("status","confirmed")=="confirmed" for b in blocks),
             "human_reviewed":bool(blocks) and all(b.get('human_reviewed') is True for b in blocks),"source_methods":methods,"location_precisions":precisions,
             "derived_view":any(b.get("derived_view",False) for b in blocks)}
    row_objects={id(b) for b in _table_row_blocks(page)}
    table_rows=[b for b in blocks if id(b) in row_objects]
    complete_rows=[b for b in table_rows if start<=b['start'] and end>=b['end']]
    complete_objects={id(b) for b in complete_rows}
    if table_rows:quality['table_complete']=len(complete_rows)==len(table_rows)
    for field in ("media_id","source_part","table_id","cell_ids","chart_id","point_id","formula_id"):
        values=[]
        for block in blocks:
            if field=='cell_ids' and id(block) in row_objects and id(block) not in complete_objects:continue
            value=block.get(field)
            if value is not None:
                for item in value if isinstance(value,list) else [value]:
                    if item not in values:values.append(item)
        if values:quality[field]=values
    bindings=[{**binding,'table_id':block['table_id']} for block in complete_rows
              if block.get('table_id') for binding in block.get('cell_bindings',[])]
    if bindings:
        quality['table_cell_bindings']=bindings
        quality['table_relation_version']=sorted({block['table_relation_version'] for block in complete_rows})
    chart_blocks=[b for b in blocks if b.get('chart_id') or b.get('kind')=='chart']
    if chart_blocks:
        from . import chart_structure
        views={(v['chart_id'],v['point_id']):v for chart in page.get('charts',[]) for v in chart_structure.point_views(chart)}
        complete_charts=[b for b in chart_blocks if start<=b['start'] and end>=b['end']
                         and (view:=views.get((b.get('chart_id'),b.get('point_id')))) and view['text']==b['text']]
        quality['chart_complete']=len(complete_charts)==len(chart_blocks)
        quality['chart_point_bindings']=[views[(b['chart_id'],b['point_id'])]['source_binding'] for b in complete_charts]
        quality['chart_relation_version']=chart_structure.VERSION
    formula_blocks=[b for b in _formula_blocks(page) if b['end']>start and b['start']<end]
    if formula_blocks:
        quality['formula_complete']=all(start<=b['start'] and end>=b['end'] for b in formula_blocks)
        quality['formula_ids']=list(dict.fromkeys(fid for b in formula_blocks for fid in b.get('formula_ids',[b.get('formula_id')]) if fid))
    confidence=[b["confidence"] for b in blocks if isinstance(b.get("confidence"),(float,int))]
    if confidence:quality["min_ocr_confidence"]=min(confidence)
    for key in ("model","fingerprint","input_image_sha256","semantic_accuracy"):
        if provenance.get(key) is not None:quality[key]=provenance[key]
    return quality


def _build_document(document_id, cancelled=None):
    _guard(cancelled)
    doc = s.doc_get(document_id)
    if doc["status"] in {"failed", "archived"}:
        return None
    documents.version(document_id,doc['sha256'])
    count, counting, budget = _counter()
    parse_id=documents.current_parse_id(document_id,doc['sha256'])
    identity = {"document_id": document_id, "sha256": doc["sha256"], "parse_id": parse_id,
                "chunker": CHUNKER_VERSION, "budget": budget, "counting": counting}
    generation_id = _hash(identity)
    with s.conn() as db:
        existing = db.execute("SELECT body,status FROM ef_rag_generations WHERE id=?", (generation_id,)).fetchone()
    if existing:
        body = json.loads(existing["body"])
        with s.conn() as db:
            current = db.execute("SELECT sha256,status FROM documents WHERE id=?", (document_id,)).fetchone()
            active=documents.active_parse_id(db,document_id,doc["sha256"])
            if not current or current["sha256"] != doc["sha256"] or current["status"] not in {"ready","needs_review"} or not active or active!=parse_id:
                raise ValueError("文档在准备索引期间已变化，未激活旧索引")
            db.execute("UPDATE ef_rag_generations SET active=0 WHERE document_id=?", (document_id,))
            db.execute("UPDATE ef_rag_generations SET active=1 WHERE id=?", (generation_id,))
        return body
    data=documents.layout(document_id,doc['sha256'])
    if data['parse_id']!=parse_id:
        # Layout may initialize the first parse. Recompute its frozen identity;
        # subsequent publication still verifies the current pointer.
        identity['parse_id']=data['parse_id'];generation_id=_hash(identity)
    chunks, missing = [], []
    for page in data["pages"]:
        _guard(cancelled)
        if not page["text_available"]:
            missing.append({"page": page["number"], "reason": "待文字识别" if page.get("ocr_required") else "没有文字"})
            continue
        pending=[{'kind':kind,'id':obj.get('id')} for kind in ('formulas','tables','charts') for obj in page.get(kind,[]) if obj.get('status')=='pending']
        if pending:missing.append({'page':page['number'],'reason':'页面局部结构尚待核验','reason_code':'source_structure_pending','structures':pending})
        oversized=[b for b in _atomic_blocks(page) if count(page['text'][b['start']:b['end']])>budget]
        formula_ids=list(dict.fromkeys(fid for b in oversized for fid in b['formula_block_ids']))
        if formula_ids:missing.append({'page':page['number'],'reason':'完整公式超过检索片段窗口，请打开原文单独核验','reason_code':'source_formula_too_long','formula_block_ids':formula_ids})
        table_rows=list(dict.fromkeys(rid for b in oversized for rid in b['table_row_ids']))
        if table_rows:missing.append({'page':page['number'],'reason':'完整表格行超过检索片段窗口，请打开原表单独核验','reason_code':'source_table_row_too_long','table_row_ids':table_rows,
                                      'table_ids':list(dict.fromkeys(tid for b in oversized for tid in b['table_ids']))})
        chart_points=list(dict.fromkeys(pid for b in oversized for pid in b['chart_point_ids']))
        if chart_points:missing.append({'page':page['number'],'reason':'完整图点关系超过检索片段窗口，请打开原图单独核验','reason_code':'source_chart_point_too_long',
                                       'chart_point_ids':chart_points,'chart_ids':list(dict.fromkeys(cid for b in oversized for cid in b['chart_ids']))})
        spans = list(_source_spans(page,count,budget))
        for start, end, hard_split in spans:
            text = page["text"][start:end]
            source_span = {"page": page["number"], "start": start, "end": end,
                           "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
            cid = _hash({**identity, "spans": [source_span]})
            title = _page_title(data, page, start)
            ctype = "table" if any(b.get("kind") == "table_row" and b["end"] > start and b["start"] < end for b in page["blocks"]) else "text"
            context_spans = []
            if ctype == "table":
                rows = [b for b in page["blocks"] if b.get("kind") == "table_row"]
                for i, row in enumerate(rows):
                    if i and re.fullmatch(r"[\s|:\-]+", row["text"]) and rows[i-1]["start"] < start:
                        header = rows[i-1]
                        # Preserve the actual header and separator as their own
                        # continuous source, not a synthetic repeated row.
                        if not any(b.get("kind") != "table_row" and row["end"] < b["start"] < start for b in page["blocks"]):
                            context_spans = [{"page": page["number"], "start": header["start"], "end": row["end"],
                                              "text": page["text"][header["start"]:row["end"]]}]
            quality = {"text_extracted": True, "ocr_required": page.get("ocr_required", False),
                       "vision_verified": page.get("vision_verified", False), "hard_split": hard_split,
                       "counting": counting, "count": count(text), "structure": data.get("structure", "按页")}
            quality.update(_extraction_quality(page,start,end))
            chunks.append({"id": cid, "generation_id": generation_id, **identity,
                           "page": page["number"], "start": start, "end": end, "text": text,
                           "title_path": title, "content_type": ctype, "quality": quality,
                           "spans": [source_span], "context_spans": context_spans, "source": doc["name"]})
    for i, chunk in enumerate(chunks):
        chunk["previous_id"] = chunks[i-1]["id"] if i else None
        chunk["next_id"] = chunks[i+1]["id"] if i+1 < len(chunks) else None
    body = {**identity, "generation_id": generation_id, "processed": len(chunks), "total": len(chunks),
            "missing_pages": missing, "status": "lexical_ready" if chunks else "failed",
            "vector_status": "pending", "error": None if chunks else "没有可检索文字；原件已保留",
            "created_at": s.now()}
    with s.conn() as db:
        # Check provenance once again after parsing. Ordinary replacement cannot
        # publish the parsed old version as the new current source.
        current = db.execute("SELECT sha256,status FROM documents WHERE id=?", (document_id,)).fetchone()
        active=documents.active_parse(db,document_id,doc["sha256"])
        if not current or current["sha256"] != doc["sha256"] or current["status"] not in {"ready","needs_review"} or not active or active["id"]!=data["parse_id"]:
            raise ValueError("文档在建索引期间已变化，未发布旧索引")
        db.execute("UPDATE ef_rag_generations SET active=0 WHERE document_id=?", (document_id,))
        db.execute("INSERT INTO ef_rag_generations VALUES (?,?,?,?,?,?,?,?)", (generation_id, document_id, doc["sha256"], data["parse_id"], 1, body["status"], _json(body), s.now()))
        for chunk in chunks:
            db.execute("INSERT INTO ef_rag_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (chunk["id"], generation_id, document_id, doc["sha256"], data["parse_id"], chunk["page"], chunk["start"], chunk["end"], chunk["text"], chunk["title_path"], chunk["content_type"], _json(chunk)))
    return body


def _active_generations():
    with s.conn() as db:
        rows = db.execute("SELECT g.* FROM ef_rag_generations g JOIN documents d ON d.id=g.document_id WHERE g.active=1 AND g.sha256=d.sha256 AND d.status IN ('ready','needs_review') ORDER BY g.document_id").fetchall()
    return [json.loads(r["body"]) for r in rows if documents.current_parse_id(r["document_id"],r["sha256"])==r["parse_id"]]


def _publish_lexical(cancelled=None):
    from .rag_candidates import SCHEMA_VERSION, noise_grams
    config = lexical_config()
    generations = _active_generations()
    content = {"generations": [{k: g[k] for k in ("document_id", "sha256", "parse_id", "generation_id")} for g in generations], "lexical_config": config, 'retrieval_schema':SCHEMA_VERSION}
    lid = _hash(content)
    filename = "rag_lexical_" + lid + ".sqlite"
    target = s.DATA / filename
    with s.conn() as db:
        existing = db.execute("SELECT * FROM ef_rag_lexical_manifests WHERE id=?", (lid,)).fetchone()
    if existing and target.is_file() and s.digest(target) == existing["sha256"]:
        with s.conn() as db:
            db.execute("UPDATE ef_rag_lexical_manifests SET active=0")
            db.execute("UPDATE ef_rag_lexical_manifests SET active=1 WHERE id=?", (lid,))
        return {**json.loads(existing["body"]), "lexical_generation": lid, "sha256": existing["sha256"]}
    staging = s.DATA / ("rag_build_" + uuid.uuid4().hex + ".sqlite")
    try:
        with sqlite3.connect(staging, factory=s.ClosingConnection) as lexical:
            lexical.set_progress_handler(lambda: int(cancelled is not None and cancelled.is_set()), 1000)
            lexical.execute("CREATE VIRTUAL TABLE chunks USING fts5(chunk_id UNINDEXED, generation_id UNINDEXED, tokens, title_tokens, bigrams, tokenize='unicode61')")
            lexical.execute("CREATE VIRTUAL TABLE noise USING fts5(chunk_id UNINDEXED, grams, tokenize='unicode61')")
            lexical.execute('CREATE TABLE chunk_meta(chunk_id TEXT PRIMARY KEY,generation_id TEXT NOT NULL,usable INTEGER NOT NULL,page INTEGER NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,input_sha256 TEXT NOT NULL)')
            lexical.execute('CREATE INDEX chunk_meta_scope ON chunk_meta(generation_id,usable,page,start,chunk_id,input_sha256)')
            for generation in generations:
                _guard(cancelled)
                with reader() as db:
                    for row in db.execute("SELECT * FROM ef_rag_chunks WHERE generation_id=? ORDER BY page,start", (generation["generation_id"],)):
                        _guard(cancelled)
                        main, grams = lexical_tokens(row["text"])
                        title, _ = lexical_tokens(row["title_path"])
                        lexical.execute("INSERT INTO chunks VALUES (?,?,?,?,?)", (row["id"], row["generation_id"], " ".join(main), " ".join(title), " ".join(grams)))
                        usable=json.loads(row['body']).get('quality',{}).get('table_complete') is not False
                        title_tail=row['title_path'][-64:]
                        vector_input=(title_tail+'\n' if title_tail else '')+row['text']
                        input_sha=hashlib.sha256(vector_input.encode()).hexdigest()
                        lexical.execute('INSERT INTO chunk_meta VALUES (?,?,?,?,?,?,?)',(row['id'],row['generation_id'],int(usable),row['page'],row['start'],row['end'],input_sha))
                        lexical.execute('INSERT INTO noise VALUES (?,?)',(row['id'],' '.join(noise_grams(row['text']))))
            lexical.execute("INSERT INTO chunks(chunks) VALUES ('optimize')")
            lexical.execute("INSERT INTO noise(noise) VALUES ('optimize')")
            lexical.execute('PRAGMA user_version='+str(SCHEMA_VERSION))
        _guard(cancelled)
        sha = s.digest(staging)
        staging.replace(target)
        body = {**content, "lexical_generation": lid, "sha256": sha, "filename": filename, "created_at": s.now()}
        with s.conn() as db:
            db.execute("UPDATE ef_rag_lexical_manifests SET active=0")
            db.execute("INSERT OR REPLACE INTO ef_rag_lexical_manifests VALUES (?,?,?,?,?,?)", (lid, filename, sha, _json(body), 1, s.now()))
        return body
    except sqlite3.OperationalError:
        _guard(cancelled)
        raise
    finally:
        staging.unlink(missing_ok=True)


def refresh_lexical(document_ids=None, cancelled=None):
    """Build local source/keyword indexes; never send texts to a model service."""
    initialize()
    with LOCK:
        ids = document_ids if document_ids is not None else [d["id"] for d in s.doc_list() if d["status"] != "failed"]
        for did in ids:
            _guard(cancelled)
            _build_document(did, cancelled)
        return _publish_lexical(cancelled)


def _set_job(job):
    with s.conn() as db:
        db.execute("INSERT OR REPLACE INTO ef_rag_index_jobs VALUES (?,?,?,?)", (job["id"], job["status"], _json(job), job["created_at"]))


def _check_index_snapshot(provider, document_ids, expected_preview, expected_provider, cancelled=None):
    """Check the reviewed outbound scope immediately before each actual batch."""
    _guard(cancelled)
    from . import rag_providers
    current = rag_providers.describe("embedding")
    if not current.get("ready") or current.get("fingerprint") != provider.get("fingerprint") or current.get("revision") != provider.get("revision"):
        raise ValueError("嵌入模型、配置或连接已变化，停止发送索引资料")
    if expected_provider and current.get("fingerprint") != expected_provider:
        raise ValueError("嵌入目标与已确认预览不一致，停止发送索引资料")
    if expected_preview is None:
        return
    sources = expected_preview.get("documents", [])
    if set(document_ids or []) != {d["id"] for d in sources}:
        raise ValueError("索引文档范围与已确认预览不一致")
    if expected_preview.get("connection_revision") is not None and current.get("connection_revision") != expected_preview["connection_revision"]:
        raise ValueError("嵌入连接与已确认预览不一致，停止发送索引资料")
    with reader() as db:
        for source in sources:
            _guard(cancelled)
            row = db.execute("SELECT sha256,status FROM documents WHERE id=?", (source["id"],)).fetchone()
            if not row or row["status"] not in {"ready", "needs_review"} or row["sha256"] != source["sha256"]:
                raise ValueError("文档版本或可用状态已变化，请重新预览索引范围")
            parse = documents.active_parse_id(db,source["id"],source["sha256"])
            generation = db.execute("SELECT id FROM ef_rag_generations WHERE document_id=? AND active=1", (source["id"],)).fetchone()
            if not parse or parse != source["parse_id"] or not generation or generation["id"] != source["generation_id"]:
                raise ValueError("文档解析或切片代次已变化，请重新预览索引范围")


def _chunk_batches(generations, cancelled=None, batch_size=16):
    # Immutable generation rows can be streamed through a read-only cursor;
    # no whole-corpus body list is retained during model calls.
    with reader() as db:
        for generation in generations:
            cursor=db.execute("SELECT body FROM ef_rag_chunks WHERE generation_id=? ORDER BY page,start",(generation["generation_id"],))
            while True:
                _guard(cancelled);rows=cursor.fetchmany(batch_size)
                if not rows:break
                yield [json.loads(row["body"]) for row in rows]


def _index_vectors(document_ids, remote_index_opt_in, cancelled, job, data_path, expected_preview=None, expected_provider=None):
    if str(s.APP_DB.resolve()) != data_path:
        return
    started = time.monotonic()
    generations = []
    try:
        job.update(status="indexing", started_at=s.now()); _set_job(job)
        refresh_lexical(document_ids, cancelled)
        from . import rag_providers
        provider = rag_providers.describe("embedding")
        if not provider.get("ready"):
            raise ValueError(provider.get("reason") or provider.get("error") or "向量模型未就绪；关键词索引可用")
        if not provider.get("local") and remote_index_opt_in and not expected_preview:
            raise ValueError("远端建索引需要固定文档预览，未发送资料")
        _check_index_snapshot(provider, document_ids, expected_preview, expected_provider, cancelled)
        generations = [g for g in _active_generations() if document_ids is None or g["document_id"] in document_ids]
        fingerprint = provider["fingerprint"]
        with reader() as db:
            job["total"]=sum(db.execute("SELECT count(*) FROM ef_rag_chunks WHERE generation_id=?",(g["generation_id"],)).fetchone()[0] for g in generations)
        job.update(reused_chunks=0,embedded_chunks=0,peak_chunk_batch=0);_set_job(job)
        import numpy as np
        processed=0
        for batch in _chunk_batches(generations,cancelled):
            _check_index_snapshot(provider, document_ids, expected_preview, expected_provider, cancelled)
            job["peak_chunk_batch"]=max(job["peak_chunk_batch"],len(batch))
            from .rag_retrieval import embedding_input
            inputs=[embedding_input(c) for c in batch]
            with reader() as db:
                cached_rows={row["chunk_id"]:row for row in db.execute("SELECT chunk_id,dimension,input_sha256,vector FROM ef_rag_embeddings WHERE fingerprint=? AND chunk_id IN ("+','.join('?' for _ in batch)+")",[fingerprint]+[c["id"] for c in batch])}
            pending=[]
            for chunk,text in zip(batch,inputs):
                _guard(cancelled);row=cached_rows.get(chunk["id"])
                if row and row["dimension"]==int(provider["dimension"]) and row["input_sha256"]==hashlib.sha256(text.encode()).hexdigest():
                    try:
                        vector=np.frombuffer(row["vector"],dtype="<f4");norm=np.linalg.norm(vector.astype(np.float64))
                        if vector.size==int(provider["dimension"]) and np.isfinite(vector).all() and np.isfinite(norm) and norm>1e-12:
                            job["reused_chunks"]+=1
                            continue
                    except (TypeError,ValueError):
                        pass
                pending.append((chunk,text))
            if pending:
                response=rag_providers.embed([text for _,text in pending],cancelled=cancelled,expected_fingerprint=fingerprint,
                    purpose="index",remote_index_opt_in=remote_index_opt_in)
                _check_index_snapshot(provider, document_ids, expected_preview, expected_provider, cancelled)
                vectors=np.asarray(response["vectors"],dtype=np.float32)
                if vectors.shape!=(len(pending),int(provider["dimension"])) or not np.isfinite(vectors).all():
                    raise ValueError("嵌入返回数量、维度或数值无效")
                norms=np.linalg.norm(vectors.astype(np.float64),axis=1)
                if not np.isfinite(norms).all() or np.any(norms<=1e-12):raise ValueError("嵌入返回零向量，未标记完成")
                vectors/=norms[:,None]
                if response.get("provider",{}).get("fingerprint",fingerprint)!=fingerprint:raise ValueError("嵌入模型在建索引期间变化，未混用向量")
                with s.conn() as db:
                    for (chunk,text),vector in zip(pending,vectors):
                        db.execute("INSERT OR REPLACE INTO ef_rag_embeddings VALUES (?,?,?,?,?,?)",(chunk["id"],fingerprint,int(provider["dimension"]),hashlib.sha256(text.encode()).hexdigest(),vector.astype("<f4").tobytes(),s.now()))
                job["embedded_chunks"]+=len(pending)
            processed+=len(batch);job["processed"]=processed;_set_job(job)
        _check_index_snapshot(provider, document_ids, expected_preview, expected_provider, cancelled)
        with s.conn() as db:
            for generation in generations:
                current = db.execute("SELECT sha256,status FROM documents WHERE id=?", (generation["document_id"],)).fetchone()
                parse = documents.active_parse_id(db,generation["document_id"],generation["sha256"])
                if not current or current["sha256"] != generation["sha256"] or current["status"] not in {"ready", "needs_review"} or not parse or parse != generation["parse_id"]:
                    raise ValueError("发布索引前文档已变化，未标记完成")
                generation.update(status="ready" if not generation["missing_pages"] else "degraded", vector_status="ready", embedding=provider, error=None)
                db.execute("UPDATE ef_rag_generations SET status=?,body=? WHERE id=?", (generation["status"], _json(generation), generation["generation_id"]))
        job.update(status="completed", error=None)
    except InterruptedError as exc:
        job.update(status="cancelled", error=str(exc))
        for generation in generations:
            generation.update(vector_status="cancelled", error=str(exc), status="degraded" if generation["processed"] else "failed")
            with s.conn() as db:
                db.execute("UPDATE ef_rag_generations SET status=?,body=? WHERE id=?", (generation["status"], _json(generation), generation["generation_id"]))
    except Exception as exc:
        job.update(status="failed", error=str(exc)[:500])
        # The source/keyword publication stays valid when a provider fails.
        for generation in generations:
            if document_ids is None or generation["document_id"] in document_ids:
                generation.update(vector_status="failed", error=str(exc)[:500], status="degraded" if generation["processed"] else "failed")
                with s.conn() as db:
                    db.execute("UPDATE ef_rag_generations SET status=?,body=? WHERE id=?", (generation["status"], _json(generation), generation["generation_id"]))
    finally:
        job.update(duration_ms=round((time.monotonic()-started)*1000, 2), finished_at=s.now())
        _set_job(job)


def submit_index(document_ids=None, *, remote_index_opt_in=False, cancelled=None, synchronous=False,
                 expected_preview=None, expected_provider=None):
    initialize()
    ids = list(dict.fromkeys(document_ids)) if document_ids is not None else None
    if ids is not None and (len(ids) > 500 or any(not isinstance(d, str) for d in ids)):
        raise ValueError("索引文档清单不正确")
    for did in ids or []:
        s.doc_get(did)
    if expected_preview is not None:
        if not isinstance(expected_preview, dict) or not isinstance(expected_preview.get("documents"), list):
            raise ValueError("索引预览清单不正确")
        expected_preview = json.loads(_json(expected_preview))
        for source in expected_preview["documents"]:
            if not isinstance(source, dict) or not all(isinstance(source.get(k), str) and source[k] for k in ("id", "sha256", "parse_id", "generation_id")):
                raise ValueError("索引预览缺少固定来源身份")
        if ids is None or len(expected_preview["documents"]) != len(ids) or set(ids) != {d["id"] for d in expected_preview["documents"]}:
            raise ValueError("索引文档清单必须与预览一致")
    if expected_provider is not None and (not isinstance(expected_provider, str) or not expected_provider):
        raise ValueError("索引模型身份不正确")
    job = {"id": uuid.uuid4().hex, "status": "pending", "document_ids": ids, "processed": 0,
           "total": 0, "error": None, "created_at": s.now(), "duration_ms": None,
           "remote_index_opt_in": bool(remote_index_opt_in)}
    _set_job(job)
    args = (ids, bool(remote_index_opt_in), cancelled, job, str(s.APP_DB.resolve()), expected_preview, expected_provider)
    if synchronous:
        _index_vectors(*args)
    else:
        POOL.submit(_index_vectors, *args)
    return dict(job)


def status():
    initialize()
    with s.conn() as db:
        generations = {g["document_id"]: json.loads(g["body"]) for g in db.execute("SELECT * FROM ef_rag_generations WHERE active=1")}
        jobs = [json.loads(r["body"]) for r in db.execute("SELECT body FROM ef_rag_index_jobs ORDER BY created_at DESC LIMIT 30")]
        lexical = db.execute("SELECT id FROM ef_rag_lexical_manifests WHERE active=1").fetchone()
    indexes = []
    for doc in s.doc_list():
        g = generations.get(doc["id"], {})
        latest = next((j for j in jobs if j["status"] in {"pending", "indexing"} and (j["document_ids"] is None or doc["id"] in j["document_ids"])), None)
        stale = g.get("sha256") != doc["sha256"] or g.get("parse_id") != documents.current_parse_id(doc["id"],doc["sha256"])
        state = latest["status"] if latest else "pending" if stale else g.get("status", "pending")
        indexes.append({"document_id": doc["id"], "name": doc["name"], "sha256": doc["sha256"],
                        "parse_id": g.get("parse_id"), "generation_id": g.get("generation_id"),
                        "status": state, "processed": latest["processed"] if latest else g.get("processed", 0),
                        "total": latest["total"] if latest else g.get("total", 0),
                        "error": g.get("error") or (latest or {}).get("error"),
                        "missing_pages": g.get("missing_pages", []), "vector_status": g.get("vector_status", "pending")})
    return {"indexes": indexes, "documents": indexes, "jobs": jobs,
            "lexical_generation": lexical["id"] if lexical else None}


def index_preview(document_ids=None):
    """Local source/index preview; no embedding or reranker invocation."""
    manifest = refresh_lexical(document_ids)
    selected = set(document_ids) if document_ids is not None else None
    out = []
    with reader() as db:
        for source in manifest["generations"]:
            if selected is not None and source["document_id"] not in selected:
                continue
            counts=db.execute("SELECT count(*),coalesce(sum(length(text)),0),coalesce(sum(length(text)+min(length(title_path),64)+CASE WHEN length(title_path)>0 THEN 1 ELSE 0 END),0) FROM ef_rag_chunks WHERE generation_id=?",(source["generation_id"],)).fetchone()
            characters=counts[2]
            doc = s.doc_get(source["document_id"])
            out.append({"id": source["document_id"], "name": doc["name"], "sha256": source["sha256"],
                        "parse_id": source["parse_id"], "generation_id": source["generation_id"],
                        "chunks": counts[0], "characters": characters,
                        "source_characters": counts[1]})
    return {"document_ids": [d["id"] for d in out], "documents": out,
            "chunk_count": sum(d["chunks"] for d in out), "character_count": sum(d["characters"] for d in out),
            "lexical_generation": manifest["lexical_generation"]}


def _provider_snapshot(sources):
    from . import rag_providers
    provider = rag_providers.describe("embedding")
    snapshot = {k: provider.get(k) for k in ("fingerprint", "revision", "dimension", "model", "local", "ready")}
    rerank = rag_providers.describe("rerank")
    rerank_snapshot = {k: rerank.get(k) for k in ("fingerprint", "revision", "model", "local", "ready")}
    generations = {}
    with reader() as db:
        for source in sources:
            if not source.get("generation_id"):
                continue
            row = db.execute("SELECT body FROM ef_rag_generations WHERE id=?", (source["generation_id"],)).fetchone()
            if row:
                body = json.loads(row["body"])
                generations[source["generation_id"]] = {"status": body.get("vector_status"),
                    "fingerprint": body.get("embedding", {}).get("fingerprint"),
                    "dimension": body.get("embedding", {}).get("dimension")}
    return {"embedding_provider": snapshot, "rerank_provider": rerank_snapshot, "vector_generations": generations}


def freeze_scope(document_context=None, requested_scope=None, previous_scope=None):
    initialize()
    if previous_scope and not document_context and not requested_scope:
        # Previous manifests must be server-owned; callers supply persisted
        # scope only, never accept this structure directly from a HTTP request.
        frozen = json.loads(_json(previous_scope))
        validate_scope(frozen)
        return frozen
    requested = requested_scope or {}
    if document_context:
        kind = "selection" if document_context.get("text_range") else "page"
        did = document_context["document_id"]
        data = documents.layout(did, document_context.get("sha256"), document_context.get("parse_id"))
        if data["document"]["archived"]:
            raise ValueError("文档已归档，不能发起新问答")
        page = int(document_context.get("page", 1))
        if not 1 <= page <= len(data["pages"]):
            raise ValueError("文档提问页码无效")
        source = {"document_id": did, "sha256": data["document"]["sha256"], "parse_id": data["parse_id"]}
        if kind == "selection":
            located = documents.resolve({**document_context, **source}, document_context.get("quote", ""))
            if located["status"] != "located":
                raise ValueError("选中文字无法定位，请重新选择")
            span = located["locator"]["text_range"]
        else:
            span = None
        # Historical page/selection scopes deliberately don't depend on the
        # current global index; their exact text is its own frozen candidate.
        return {"scope_type": kind, "documents": [source], "page": page, "text_range": span,
                **_provider_snapshot([source]),
                "lexical_generation": None, "created_at": s.now(), "scope_id": _hash([kind, source, page, span])}
    kind = requested.get("scope_type", "corpus")
    if kind not in {"document", "corpus"}:
        raise ValueError("知识库范围不正确")
    ids = requested.get("document_ids")
    if ids is not None and (not isinstance(ids, list) or len(ids) > 500 or any(not isinstance(i, str) for i in ids)):
        raise ValueError("文档范围清单不正确")
    if kind == "document" and (not ids or len(ids) != 1):
        raise ValueError("全文提问需要明确选择一份文档")
    manifest = refresh_lexical(ids)
    sources = manifest["generations"]
    if ids is not None:
        sources = [g for g in sources if g["document_id"] in ids]
        if any(d not in {g["document_id"] for g in sources} for d in ids):
            raise ValueError("所选文档未就绪或已归档")
    frozen = {"scope_type": kind, "documents": sources, "lexical_generation": manifest["lexical_generation"],
              "lexical_sha256": manifest["sha256"], "created_at": s.now(), **_provider_snapshot(sources)}
    frozen["scope_id"] = _hash({k: v for k, v in frozen.items() if k != "created_at"})
    return frozen


def validate_scope(scope, *, cancelled=None, deadline=None):
    _guard(cancelled, deadline)
    if not isinstance(scope, dict) or scope.get("scope_type") not in {"selection", "page", "document", "corpus"}:
        raise ValueError("缺少服务端固定的知识库范围")
    if not isinstance(scope.get("documents"), list):
        raise ValueError("文档范围清单缺失")
    available = []
    # Reuse one catalog connection; still hash every original and check current
    # revocation on every validation. No cross-query file-verification cache.
    with reader() as db:
        for source in scope["documents"]:
            _guard(cancelled, deadline)
            current=db.execute('SELECT status FROM documents WHERE id=?',(source['document_id'],)).fetchone()
            if not current:s.doc_get(source['document_id'])
            if current['status']=='archived':continue
            sha=source['sha256']
            if not isinstance(sha,str) or not re.fullmatch(r'[a-f0-9]{64}',sha):raise ValueError('无效的文档版本')
            original=db.execute('SELECT format FROM ef_document_versions WHERE document_id=? AND sha256=?',(source['document_id'],sha)).fetchone()
            if not original:raise ValueError('这个版本不属于当前文档')
            path=s.VERSIONS/(sha+'.'+original['format'])
            if not path.is_file():raise ValueError('原文件版本已缺失')
            if s.digest(path)!=sha:raise ValueError('原件摘要不一致，已停止定位')
            _guard(cancelled, deadline)
            available.append(source)
    return available


def lexical_path(scope, *, cancelled=None, deadline=None):
    _guard(cancelled, deadline)
    lid = scope.get("lexical_generation")
    if not isinstance(lid, str) or not re.fullmatch(r"[a-f0-9]{64}", lid):
        raise ValueError("关键词索引代次无效")
    with reader() as db:
        row = db.execute("SELECT * FROM ef_rag_lexical_manifests WHERE id=?", (lid,)).fetchone()
    if not row:
        raise ValueError("固定关键词索引不存在，未切换其他代次")
    if row["filename"] != "rag_lexical_"+lid+".sqlite":
        raise ValueError("固定关键词索引文件名与代次不匹配")
    path = s.DATA / row["filename"]
    if not path.is_file() or s.digest(path) != row["sha256"] or row["sha256"] != scope.get("lexical_sha256"):
        raise ValueError("固定关键词索引缺失或摘要不匹配")
    _guard(cancelled, deadline)
    return path


def chunks_for_scope(scope, *, cancelled=None, deadline=None):
    sources = validate_scope(scope, cancelled=cancelled, deadline=deadline)
    if scope["scope_type"] in {"page", "selection"}:
        if not sources:
            return []
        source = sources[0]
        data = documents.layout(source["document_id"], source["sha256"], source["parse_id"])
        page = data["pages"][scope["page"]-1]
        selection = scope.get("text_range")
        if selection:
            start, end = selection["start"], selection["end"]
            candidates = [(start, end, False)]
        else:
            candidates = list(_source_spans(page,len,CHUNK_TOKENS))
        out = []
        for start, end, hard_split in candidates:
            _guard(cancelled, deadline)
            text = page["text"][start:end]
            span = {"page": scope["page"], "start": start, "end": end, "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
            cid = _hash([source, span])
            out.append({"id": cid, **source, "generation_id": "scope-"+scope["scope_id"], "page": scope["page"], "start": start, "end": end, "text": text,
                        "title_path": _page_title(data, page, start), "content_type": "text", "spans": [span],
                        "source": data["document"]["name"], "quality": {"counting": "conservative_unicode_characters", "hard_split": hard_split, "ocr_required": page.get("ocr_required", False),**_extraction_quality(page,start,end)}})
        return out
    out = []
    with reader() as db:
        for source in sources:
            _guard(cancelled, deadline)
            row = db.execute("SELECT * FROM ef_rag_generations WHERE id=? AND document_id=? AND sha256=? AND parse_id=?", (source["generation_id"], source["document_id"], source["sha256"], source["parse_id"])).fetchone()
            if not row:
                raise ValueError("固定切片代次缺失，未改用当前版本")
            out.extend(json.loads(r["body"]) for r in db.execute("SELECT body FROM ef_rag_chunks WHERE generation_id=? ORDER BY page,start", (source["generation_id"],)))
    return out
