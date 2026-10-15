"""Version-pinned document reading and deterministic, inspectable source locations.

PDF coordinates use the displayed (rotated) page, normalized to [0, 1].
Text ranges use Unicode code points in the stored page text, never DOM offsets.
Derived recognition is immutable and explicitly identified separately from native text.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
import uuid
from pathlib import Path
from decimal import Decimal
from . import store as s

LEGACY_PARSER = "source-layout-v1"
PARSER = "source-layout-v2"
LOCK = threading.RLock()


def register(c, doc):
    if doc and re.fullmatch(r"[a-f0-9]{64}", doc["sha256"]):
        c.execute("INSERT OR IGNORE INTO ef_document_versions VALUES (?,?,?,?,?,?)",
                  (doc["id"], doc["sha256"], doc["name"], doc["format"], doc["pages"], s.now()))


def initialize(c):
    c.executescript("""
      CREATE TABLE IF NOT EXISTS ef_document_versions(
        document_id TEXT, sha256 TEXT, name TEXT, format TEXT, pages INTEGER,
        created_at TEXT, PRIMARY KEY(document_id,sha256));
      CREATE TABLE IF NOT EXISTS ef_document_parses(
        id TEXT PRIMARY KEY, document_id TEXT, sha256 TEXT, parser TEXT,
        body TEXT, created_at TEXT, reason TEXT, page INTEGER);
      CREATE INDEX IF NOT EXISTS ef_document_parse_lookup
        ON ef_document_parses(document_id,sha256,created_at);
      CREATE TABLE IF NOT EXISTS ef_document_active_parses(
        document_id TEXT, sha256 TEXT, parse_id TEXT NOT NULL,
        PRIMARY KEY(document_id,sha256));
    """)
    for doc in c.execute("SELECT * FROM documents").fetchall(): register(c, doc)
    # Recover provenance of legacy saved sources without accepting arbitrary hashes.
    for row in c.execute("SELECT result_json FROM runs UNION SELECT result_json FROM saved_analyses"):
        for evidence in json.loads(row[0]).get("evidence", []):
            loc = evidence.get("locator", {})
            did, sha = loc.get("document_id"), loc.get("sha256", "")
            doc = c.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
            if doc and re.fullmatch(r"[a-f0-9]{64}", sha) and (s.VERSIONS/(sha+"."+doc["format"])).is_file():
                register(c, {**dict(doc), "sha256":sha, "name":evidence.get("source", doc["name"])})


def active_parse(c, did, sha):
    """One authoritative current parse; historical IDs remain independently readable."""
    pointer=c.execute("SELECT parse_id FROM ef_document_active_parses WHERE document_id=? AND sha256=?",(did,sha)).fetchone()
    if pointer:
        row=c.execute("SELECT * FROM ef_document_parses WHERE id=? AND document_id=? AND sha256=?",(pointer[0],did,sha)).fetchone()
        if not row: raise ValueError("当前解析指针已损坏，未切换其他解析")
        return row
    return c.execute("SELECT * FROM ef_document_parses WHERE document_id=? AND sha256=? AND parser IN (?,?) ORDER BY created_at DESC,id DESC LIMIT 1",(did,sha,PARSER,LEGACY_PARSER)).fetchone()


def current_parse_id(did, sha):
    with s.conn() as c: row=active_parse(c,did,sha)
    return row["id"] if row else None


def version(did, sha=None):
    current = s.doc_get(did)
    sha = sha or current["sha256"]
    if not re.fullmatch(r"[a-f0-9]{64}", sha): raise ValueError("无效的文档版本")
    with s.conn() as c:
        row = c.execute("SELECT * FROM ef_document_versions WHERE document_id=? AND sha256=?", (did,sha)).fetchone()
    if not row: raise ValueError("这个版本不属于当前文档")
    d = dict(row)
    path = s.VERSIONS/(sha+"."+d["format"])
    if not path.is_file(): raise ValueError("原文件版本已缺失")
    if s.digest(path) != sha: raise ValueError("原件摘要不一致，已停止定位")
    d.update(current_sha256=current["sha256"], is_current=sha==current["sha256"],
             archived=current["status"]=="archived", size_bytes=path.stat().st_size,
             source_url=f"/api/documents/{did}/original?version={sha}")
    quality=json.loads(current.get('quality_json') or '{}')
    provenance=quality.get('source_provenance')
    if provenance and provenance.get('file_sha256') == sha:
        d.update(provenance=provenance,official_url=provenance.get('official_url'))
    return d, path


def _box(rect, page):
    import pymupdf
    r = pymupdf.Rect(rect)*page.rotation_matrix
    w,h = page.rect.width,page.rect.height
    return [round(max(0,min(1,x)),7) for x in (r.x0/w,r.y0/h,r.x1/w,r.y1/h)]


def parse_page(path, page_number):
    """Read only the requested page. PDF render and extraction share rotation."""
    fmt = path.suffix.lower().lstrip(".")
    if fmt=='docx':
        from .document_native import parse_docx
        pages=parse_docx(path)['pages']
        if not 1<=page_number<=len(pages):raise ValueError('逻辑节超出范围')
        return pages[page_number-1]
    text=""; blocks=[]; images=[]; headings=[]; width,height=595,842; raster=None
    if fmt == "pdf":
        import pymupdf
        with pymupdf.open(path) as doc:
            if not 1<=page_number<=len(doc): raise ValueError("页码超出范围")
            p=doc[page_number-1]; width,height=p.rect.width,p.rect.height
            raw=p.get_text("rawdict",sort=True,flags=pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
            for bi,b in enumerate(raw["blocks"]):
                if b["type"] != 0: continue
                start=len(text); chars=[]; sizes=[]; fonts={}; bold=False; baselines=[]
                for line in b["lines"]:
                    direction=line.get("dir")
                    if direction and len(direction)==2:
                        baselines.append(round(__import__("math").degrees(__import__("math").atan2(direction[1],direction[0])),4))
                    for span in line["spans"]:
                        # Size, weight and family are recorded so a heading hierarchy
                        # can be inferred later for PDFs without bookmarks. Chinese
                        # bulletins set section headings in 黑体 at the same size as
                        # the 仿宋 body, so the family matters as much as the size.
                        sizes.append(float(span.get("size") or 0))
                        if int(span.get("flags") or 0) & 16 or "bold" in str(span.get("font", "")).lower():
                            bold=True
                        name=str(span.get("font") or "")
                        fonts[name]=fonts.get(name,0)+len(span["chars"])
                        for char in span["chars"]:
                            chars.append({"start":len(text),"end":len(text)+len(char["c"]),"text":char["c"],"box":_box(char["bbox"],p)})
                            text+=char["c"]
                    text+="\n"
                end=len(text)
                if text[start:end].strip():
                    blocks.append({"id":f"p{page_number}-b{bi}","start":start,"end":end,"text":text[start:end],"kind":"text","box":_box(b["bbox"],p),"chars":chars,"font_size":max(sizes) if sizes else None,"font":max(fonts,key=fonts.get) if fonts else None,"bold":bold,"baseline_angles":baselines})
                text+="\n"
            images=[{"box":_box(i["bbox"],p)} for i in p.get_image_info()]
            rotation=p.rotation
            # A page with no text layer is exactly where orientation and
            # sharpness have to be judged from pixels rather than metadata.
            if not text.strip():
                scale=min(2,1600/max(width,height))
                raster=p.get_pixmap(matrix=pymupdf.Matrix(scale,scale),alpha=False).tobytes("png")
    elif fmt in {"png","jpg","jpeg","webp"}:
        from PIL import Image
        if page_number!=1: raise ValueError("页码超出范围")
        with Image.open(path) as im: width,height=im.size
        rotation=0; images=[{"box":[0,0,1,1]}]
    elif fmt in {"md","txt"}:
        if page_number!=1: raise ValueError("页码超出范围")
        text=path.read_text(encoding="utf-8-sig"); offset=0; titles=[]; rotation=0
        for i,line in enumerate(text.splitlines(keepends=True)):
            content=line.rstrip("\r\n")
            heading=re.match(r"^(#{1,6})\s+(.+)",content) if fmt=="md" else None
            if heading:
                level=len(heading[1]); titles=titles[:level-1]+[heading[2]]
                headings.append({"title":heading[2],"level":level,"page":1,"start":offset,"end":offset+len(content),"block_id":f"p1-b{i}"})
            if content.strip():
                blocks.append({"id":f"p1-b{i}","start":offset,"end":offset+len(content),"text":content,
                               "kind":"heading" if heading else "table_row" if fmt=="md" and content.strip().startswith("|") else "text",
                               "level":len(heading[1]) if heading else 0,"title_path":" / ".join(titles),"box":None,"chars":[]})
            offset+=len(line)
    else: raise ValueError("此格式暂不支持原文定位")
    result={"number":page_number,"width":width,"height":height,"rotation":rotation,"text":text,"blocks":blocks,"images":images,"headings":headings,"text_available":bool(text.strip()),"ocr_required":not bool(text.strip()) and fmt not in {"md","txt"},"vision_verified":False,"method":"native_text","location_precision":"character" if fmt=="pdf" and text.strip() else "text_range","tables":[],"charts":[],"issues":[]}
    if fmt=="pdf":
        from .document_native import enhance_pdf_page
        result=enhance_pdf_page(path,page_number,result)
    from . import document_quality
    if fmt in {"png","jpg","jpeg","webp"}:
        # Advisory raster measurements for the quality alarms. They never block
        # a parse, and a page that cannot be measured reports nothing.
        result["quality_signals"]=document_quality.page_signals(path.read_bytes())
    elif raster is not None:
        result["quality_signals"]=document_quality.page_signals(raster)
    return document_quality.inspect(result)


def _parse_file(path):
    if path.suffix.lower()==".docx":
        from .document_native import parse_docx
        body=parse_docx(path)
        from . import document_quality
        body['pages']=[document_quality.inspect(p) for p in body['pages']]
        return body
    if path.suffix.lower()==".pdf":
        import pymupdf
        with pymupdf.open(path) as doc:
            count=len(doc)
            toc=[{"level":x[0],"title":x[1],"page":x[2]} for x in doc.get_toc() if 1<=x[2]<=count]
    else: count=1; toc=[]
    pages=[parse_page(path,n) for n in range(1,count+1)]
    from .document_native import infer_pdf_outline, link_continuations
    # A PDF without a bookmark outline still has font sizes; they are the only
    # signal left when the file's own directory is missing or unusable. The
    # result is reported as inferred, never as declared structure.
    inferred=[] if toc else infer_pdf_outline(pages)
    link_continuations(pages)
    if toc: structure="原文件书签"
    elif path.suffix.lower()==".md": structure="Markdown 标题"
    elif path.suffix.lower()==".docx": structure="Word 标题与表格"
    elif inferred: structure="PDF 字体层级（推定）"
    else: structure="按页"
    return {"pages":pages,"outline":toc or [h for p in pages for h in p["headings"]],"structure":structure,"parser":PARSER}


def save_parse(c, did, sha, body, reason, page=None, *, activate=True):
    pid=uuid.uuid4().hex
    c.execute("INSERT INTO ef_document_parses VALUES (?,?,?,?,?,?,?,?)", (pid,did,sha,body.get("parser",PARSER),json.dumps(body,ensure_ascii=False),s.now(),reason,page))
    if activate:
        c.execute("INSERT OR REPLACE INTO ef_document_active_parses VALUES (?,?,?)",(did,sha,pid))
    return {**body,"parse_id":pid}


def _save_parse(did, sha, body, reason, page=None):
    with s.conn() as c:
        return save_parse(c,did,sha,body,reason,page)


def layout(did, sha=None, parse_id=None):
    d,path=version(did,sha)
    with LOCK:
        with s.conn() as c:
            row=c.execute("SELECT * FROM ef_document_parses WHERE document_id=? AND sha256=? AND id=?",(did,d["sha256"],parse_id)).fetchone() if parse_id else active_parse(c,did,d["sha256"])
        if parse_id and not row: raise ValueError("解析版本不属于当前文档")
        try: body={**json.loads(row["body"]),"parse_id":row["id"]} if row else _save_parse(did,d["sha256"],_parse_file(path),"读取原件")
        except Exception as exc: raise ValueError("原文件无法解析，可能已损坏、加密或格式不受支持。原文件已保留。") from exc
    return {**body,"document":d}


def view(did, sha=None, parse_id=None):
    data=layout(did,sha,parse_id)
    with s.conn() as c:
        versions=[dict(r) for r in c.execute("SELECT sha256,created_at FROM ef_document_versions WHERE document_id=? ORDER BY created_at DESC",(did,))]
        active=active_parse(c,did,data["document"]["sha256"])
        parses=[dict(r) for r in c.execute("SELECT id AS parse_id,created_at,reason,parser FROM ef_document_parses WHERE document_id=? AND sha256=? ORDER BY created_at DESC,id DESC",(did,data["document"]["sha256"]))]
    active_id=active["id"] if active else None
    for item in parses: item["is_active"]=item["parse_id"]==active_id
    sections=[{"page":p["number"],"image_count":len(p.get("images",[]))} for p in data["pages"] if p.get("images")]
    return {"document":data["document"],"parse_id":data["parse_id"],"parser":data.get("parser",PARSER),"page_count":len(data["pages"]),"outline":data["outline"],"structure":data["structure"],"versions":versions,"text_pages":sum(p["text_available"] for p in data["pages"]),"page_unit":"logical_section" if data["document"]["format"]=="docx" else "page","parses":parses,"active_parse_id":active_id,"is_active_parse":data["parse_id"]==active_id,"image_count":sum(x["image_count"] for x in sections),"embedded_image_sections":sections if data["document"]["format"]=="docx" else []}


def get_page(did, number, sha=None, parse_id=None):
    data=layout(did,sha,parse_id)
    if not 1<=number<=len(data["pages"]): raise ValueError("页码超出范围")
    page=copy.deepcopy(data["pages"][number-1])
    if data["document"]["format"]=="docx":
        for media in page.get("images",[]):
            if media.get("id"): media["preview_url"]=f"/api/documents/{did}/media/{media['id']}?version={data['document']['sha256']}"
    return {**page,"parse_id":data["parse_id"],"sha256":data["document"]["sha256"]}


def rects_for(page, start, end):
    # Merge adjacent glyphs on a line, keeping distinct rows and vertical text.
    boxes=[]
    for b in page["blocks"]:
        if b["end"]<=start or b["start"]>=end: continue
        chars=b.get("chars",[])
        if chars: boxes.extend(ch["box"] for ch in chars if ch["end"]>start and ch["start"]<end)
        elif b.get("box"): boxes.append(b["box"])
    merged=[]
    for box in boxes:
        if merged and abs(merged[-1][1]-box[1])<.002 and abs(merged[-1][3]-box[3])<.002 and -.002<=box[0]-merged[-1][2]<.015:
            merged[-1][2]=max(merged[-1][2],box[2])
        else: merged.append(list(box))
    return merged


def _matches(text, quote):
    if not quote.strip(): return []
    mapping=[i for i,c in enumerate(text) if not c.isspace()]
    compact="".join(text[i] for i in mapping); needle="".join(quote.split()); start=0; found=[]
    if not needle: return []
    while len(found)<100:
        i=compact.find(needle,start)
        if i<0: break
        found.append((mapping[i],mapping[i+len(needle)-1]+1));start=i+1
    return found


def resolve(locator, quote=""):
    data=layout(locator["document_id"],locator.get("sha256"),locator.get("parse_id"))
    n=int(locator.get("page",1))
    if not 1<=n<=len(data["pages"]): raise ValueError("引用页码超出范围")
    p=data["pages"][n-1]; sha=data["document"]["sha256"]; span=locator.get("text_range")
    found=[]
    if span:
        start,end=span.get("start"),span.get("end")
        if isinstance(start,int) and isinstance(end,int) and 0<=start<end<=len(p["text"]):
            actual=p["text"][start:end]
            expected=span.get("text_sha256")
            if (not expected or hashlib.sha256(actual.encode()).hexdigest()==expected) and (not quote or "".join(actual.split())=="".join(quote.split())): found=[(start,end)]
        # A broken pinned range must never silently select a different occurrence.
    else: found=_matches(p["text"],quote)
    found=[(start,end) for start,end in found if not any(b.get("status","confirmed")!="confirmed" and b["end"]>start and b["start"]<end for b in p["blocks"])]
    loc={**locator,"sha256":sha,"parse_id":data["parse_id"],"page":n,"source_url":data["document"]["source_url"]}
    status="located" if len(found)==1 else "ambiguous" if found else "unlocated"
    relevant=[];methods=set()
    if status=="located":
        start,end=found[0]; actual=p["text"][start:end]
        relevant=[b for b in p["blocks"] if b["end"]>start and b["start"]<end]
        refs=[{k:b[k] for k in ("id","source_ref","source","method","source_method","source_part","kind","object_id","table_id","cell_ids","chart_id","point_id","media_id","observation_id") if k in b} for b in relevant]
        precisions={b.get("location_precision",p.get("location_precision",p.get("provenance",{}).get("location_precision","text_range"))) for b in relevant}
        methods={b.get("source_method",b.get("method",p.get("provenance",{}).get("method",p.get("method","native_text")))) for b in relevant}
        loc.update(text_range={"start":start,"end":end,"text_sha256":hashlib.sha256(actual.encode()).hexdigest()},rects=rects_for(p,start,end),location_precision=next(iter(precisions)) if len(precisions)==1 else "mixed",extraction_method=next(iter(methods)) if len(methods)==1 else "mixed",source_refs=refs)
        loc["derived_view"]=any(b.get("table_id") or b.get("chart_id") or b.get("source_method") in {"ocr","visual_observed","human_review"} for b in relevant)
        media_ids={b.get("media_id") for b in relevant}
        if len(media_ids)==1 and next(iter(media_ids)):
            loc.update(media_id=next(iter(media_ids)),coordinate_space="media")
        elif any(media_ids):
            loc["media_regions"]=[{"media_id":mid,"rects":[b["box"] for b in relevant if b.get("media_id")==mid and b.get("box")]} for mid in media_ids if mid]
            loc["rects"]=[];loc["coordinate_space"]="mixed"
    else: loc.update(rects=[],location_precision="page");loc.pop("text_range",None)
    method=p.get("provenance",{}).get("method",p.get("method","native_text"))
    scores=[b["confidence"] for b in relevant if isinstance(b.get("confidence"),(int,float)) and not isinstance(b.get("confidence"),bool) and 0<=b["confidence"]<=1]
    quality={"confirmed":status=="located","human_reviewed":bool(relevant) and all(b.get('human_reviewed') is True for b in relevant),"derived_view":bool(loc.get("derived_view")),"source_methods":sorted(methods),"location_precision":loc["location_precision"],"min_ocr_confidence":min(scores) if scores else None,"semantic_accuracy":"not_proven"}
    table_rows=[b for b in relevant if b.get('kind')=='table_row' and b.get('cell_bindings') and b.get('table_relation_version')]
    if table_rows:
        complete=[b for b in table_rows if start<=b['start'] and end>=b['end']]
        quality['table_complete']=len(complete)==len(table_rows)
        incomplete_ids={b['id'] for b in table_rows if b not in complete}
        for ref in loc.get('source_refs',[]):
            if ref['id'] in incomplete_ids:ref.pop('cell_ids',None)
        bindings=[{**binding,'table_id':b['table_id']} for b in complete for binding in b['cell_bindings']]
        if bindings:
            quality.update(table_cell_bindings=bindings,table_relation_version=sorted({b['table_relation_version'] for b in complete}),
                           cell_ids=list(dict.fromkeys(cid for b in complete for cid in b.get('cell_ids',[]))))
    provenance=data['document'].get('provenance',{})
    publication={k:v for k,v in provenance.items() if k in
        {'title','institution','document_year','published_at','origin','publication_status','publication_group'}}
    return {"status":status,"matches":len(found),"locator":loc,"is_current":data["document"]["is_current"],"text_available":p["text_available"],"quote":p["text"][found[0][0]:found[0][1]] if status=="located" else quote,"method":"固定解析文字定位："+method,"recognition_derived":method not in {"native_text","docx_native","pdf_native","native"},"visual_verified":False,"quality":quality,"publication":publication}


def search(did, query, sha=None, parse_id=None):
    data=layout(did,sha,parse_id);matches=[]
    if len(query)>200: raise ValueError("搜索词最多 200 个字")
    for p in data["pages"]:
        for start,end in _matches(p["text"],query):
            matches.append({"page":p["number"],"text":p["text"][max(0,start-25):end+45],"start":start,"end":end,"rects":rects_for(p,start,end)})
            if len(matches)==100: break
        if len(matches)==100:break
    return {"matches":matches,"truncated":len(matches)==100,"parse_id":data["parse_id"],"sha256":data["document"]["sha256"]}


def render(did, number, sha=None):
    d,path=version(did,sha)
    if d["format"]!="pdf": raise ValueError("当前原件不是 PDF")
    import pymupdf
    try: pdf=pymupdf.open(path)
    except Exception as exc: raise ValueError("原页无法读取，请检查原文件是否损坏或已加密。") from exc
    with pdf as doc:
        if not 1<=number<=len(doc): raise ValueError("页码超出范围")
        p=doc[number-1]; scale=min(2,2000/max(p.rect.width,p.rect.height))
        return p.get_pixmap(matrix=pymupdf.Matrix(scale,scale),alpha=False).tobytes("png")


def render_image(did, number, sha=None):
    d,path=version(did,sha)
    if d["format"]=="pdf": return render(did,number,d["sha256"])
    if d["format"] not in {"png","jpg","jpeg","webp"} or number!=1:
        raise ValueError("正式图像识别支持 PDF 页或图片原件")
    from PIL import Image
    import io
    with Image.open(path) as image:
        if image.width*image.height>40_000_000: raise ValueError("图片超过 4000 万像素，请缩小后导入")
        image=image.convert("RGB");image.thumbnail((2400,2400));buf=io.BytesIO();image.save(buf,format="PNG")
        return buf.getvalue()


def reparse(did, number, sha=None, parse_id=None):
    before=layout(did,sha,parse_id);d,path=version(did,before["document"]["sha256"])
    if d["archived"]: raise ValueError("请先恢复文档")
    if not 1<=number<=len(before["pages"]): raise ValueError("页码超出范围")
    if d["format"] in {"png","jpg","jpeg","webp"}: raise ValueError("未连接文字识别，未执行重读")
    t=time.perf_counter(); fresh=_parse_file(path)["pages"][number-1] if d["format"]=="docx" else parse_page(path,number)
    if not fresh["text_available"]: raise ValueError("当前页没有可提取文字，未连接 OCR；未更改引用")
    body={k:copy.deepcopy(before[k]) for k in ("pages","outline","structure","parser")};old=body["pages"][number-1];body["pages"][number-1]=fresh
    body["parser"]=PARSER
    if d["format"]=="md": body["outline"]=fresh["headings"]
    with s.conn() as c:
        current=c.execute("SELECT status,sha256,quality_json FROM documents WHERE id=?",(did,)).fetchone()
        active=active_parse(c,did,d["sha256"])
        if not current or current["status"]=="archived" or not active or active["id"]!=before["parse_id"]: raise ValueError("文档已归档或基础解析已改变，请重新读取")
        after=save_parse(c,did,d["sha256"],body,"用户重读指定页面",number)
        if d["is_current"]:
            quality=s.retain_provenance(s.quality_for_parse(body),current)
            c.execute("UPDATE documents SET quality_json=?,status=?,updated_at=? WHERE id=? AND sha256=? AND status!='archived'",(json.dumps(quality,ensure_ascii=False),"needs_review" if quality["quality_flags"] else "ready",s.now(),did,d["sha256"]))
    record={"id":str(uuid.uuid4()),"page":number,"sha256":d["sha256"],"before_parse_id":before["parse_id"],"after_parse_id":after["parse_id"],"before":old["text"],"after":fresh["text"],"changed":old["text"]!=fresh["text"],"method":"本机原始文字提取；未调用 OCR 或视觉模型","created_at":s.now(),"duration_ms":round((time.perf_counter()-t)*1000,2)}
    with s.conn() as c:c.execute("INSERT INTO reparses VALUES (?,?,?,?,?)",(record["id"],did,number,record["created_at"],json.dumps(record,ensure_ascii=False)))
    return record


def scoped_blocks(scope, question):
    """A document question is constrained to this source version and current page."""
    data=layout(scope["document_id"],scope.get("sha256"),scope.get("parse_id"));n=int(scope.get("page",1))
    if not 1<=n<=len(data["pages"]): raise ValueError("文档提问页码无效")
    p=data["pages"][n-1]; span=scope.get("text_range")
    if span:
        bound=resolve({**scope,"sha256":data["document"]["sha256"],"parse_id":data["parse_id"]},scope.get("quote",""))
        if bound["status"]!="located": raise ValueError("选中文本已不匹配，请重新选择")
        ranges=[(span["start"],span["end"])]
    else:
        terms={question[i:i+2] for i in range(len(question)-1) if re.fullmatch(r"[\u4e00-\u9fff]{2}",question[i:i+2])}|set(re.findall(r"[A-Za-z0-9_]+",question))
        scores=sorted([(sum(t in b["text"] for t in terms),b) for b in p["blocks"] if b["kind"]!="heading"],key=lambda x:-x[0])
        ranges=[(b["start"],b["end"]) for score,b in scores[:3] if score>0]
    doc=data["document"]; items=[]
    for start,end in ranges:
        text=p["text"][start:end];loc={"document_id":scope["document_id"],"sha256":doc["sha256"],"parse_id":data["parse_id"],"page":n,"text_range":{"start":start,"end":end,"text_sha256":hashlib.sha256(text.encode()).hexdigest()},"rects":rects_for(p,start,end),"source_url":doc["source_url"],"location_precision":"text_range"}
        items.append({"id":f"scope-{data['parse_id']}-{n}-{start}","type":"document","source":doc["name"],"snippet":text,"status":"text_supported","locator":loc})
    return items


def verify_claim(run_id, claim_id):
    result=s.get_run(run_id);claim=next((c for c in result.get("claims",[]) if c["id"]==claim_id),None)
    if not claim: raise ValueError("结论不存在")
    evs=[e for e in result["evidence"] if e["id"] in claim.get("evidence_ids",[])]
    located=[]
    for ev in evs:
        if ev["type"]=="document":
            try: located.append({"id":ev["id"],**resolve(ev["locator"],ev.get("snippet",""))})
            except ValueError as exc:located.append({"id":ev["id"],"status":"unlocated","error":str(exc)})
    refs_ok=bool(evs) and len(evs)==len(claim.get("evidence_ids",[])) and all(x["status"]=="located" for x in located)
    checks=[{"name":"引用定位","status":"passed" if refs_ok else "needs_review","detail":"文档文字范围已匹配，表格使用查询时的返回快照。" if refs_ok else "存在未定位、重复或缺失的引用；请查看来源。"}]
    unit={"name":"单位与数值","status":"not_checked","detail":"当前结论没有可独立核对的结构化数值，未做单位换算。"}
    formula=next((f for f in result.get("formulas",[]) if claim.get("kind")=="formula" and Decimal(str(f["value"]))==Decimal(str(claim.get("value",0)))),None)
    if formula:
        from .engine import calculate,check_parameters
        try:
            ps=formula["parameters"];check_parameters(ps);value=calculate(formula["expression"],{p["name"]:p["value"] for p in ps})
            okay=abs(value-Decimal(str(claim["value"])))<=Decimal('.01') and formula["unit"]==claim.get("unit") and refs_ok
            unit.update(status="passed" if okay else "needs_review",detail=f"按保存的参数重新计算为 {value} {formula['unit']}；只校验本条公式，不代表其他文档单位已核验。")
        except (ValueError,KeyError) as exc:unit.update(status="needs_review",detail=str(exc))
    checks.append(unit)
    # Exact excerpt identity can be verified; free semantic entailment cannot.
    texts=["".join(x.get("quote","").split()) for x in located if x["status"]=="located"]
    if len(texts)>1 and len(set(texts))==1:
        consistency={"name":"多源一致性","status":"passed","detail":"本条结论所附文档片段逐字相同；未执行语义推断。"}
    else:consistency={"name":"多源一致性","status":"not_checked","detail":"没有两个可直接比较的同口径文档数值；未执行跨文档事实判定。"}
    # Only compare explicitly labelled quantities. Do not compare unrelated
    # numbers (e.g. sales versus bonus) or guess that different labels coincide.
    quantities=[]
    for x in located:
        fields={}
        if x['status']=='located':
            for label,number,unit_name in re.findall(r'([\u4e00-\u9fffA-Za-z]{2,})\s*[:：=|]\s*([0-9][0-9,.]*)\s*(亿元|万元|元|%)',x.get('quote','')):
                multiplier={'亿元':Decimal(100000000),'万元':Decimal(10000),'元':Decimal(1),'%':Decimal('.01')}[unit_name]
                fields[label]=(Decimal(number.replace(',',''))*multiplier,'ratio' if unit_name=='%' else 'CNY')
        quantities.append(fields)
    comparisons=[]
    for i,a in enumerate(quantities):
        for b in quantities[i+1:]:
            for label in a.keys() & b.keys(): comparisons.append((label,a[label],b[label]))
    if comparisons:
        equal=all(a==b for _,a,b in comparisons)
        unit.update(status='passed' if refs_ok else 'needs_review',detail='仅对同名字段执行单位换算：元／万元／亿元换为人民币元，百分数换为比例。')
        consistency={'name':'多源一致性','status':'passed' if equal and refs_ok else 'needs_review','detail':'；'.join(f'{label}：{a[0]} {a[1]} / {b[0]} {b[1]}' for label,a,b in comparisons)+('；数值一致，不代表语义已验证。' if equal else '；存在冲突，未替你选择其中一个数值。')}
    checks.append(consistency)
    counter=claim.get('counter_check')
    if counter:
        checks.append({"name":"数值反证",
                       "status":{"passed":"passed","failed":"failed"}.get(counter.get("status"),"not_checked"),
                       "detail":counter.get("detail") or "没有独立复算记录。"})
    return {"claim_id":claim_id,"checks":checks,"sources":located,"checked_at":s.now(),"scope":"仅当前结论","visual_verified":False}
