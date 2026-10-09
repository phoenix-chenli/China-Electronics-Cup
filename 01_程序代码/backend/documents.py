"""Version-pinned document reading and deterministic, inspectable source locations.

PDF coordinates use the displayed (rotated) page, normalized to [0, 1].
Text ranges use Unicode code points in the stored page text, never DOM offsets.
Markdown and DOCX heading levels come from what the file itself declares
(heading syntax or Word outline levels), not from font size or position.
Local OCR supplies text only when a verified model pack is installed; its boxes
are line-level and results stay marked for review. No visual verification or
semantic confidence is used.
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

PARSER = "source-layout-v1"
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
    return d, path


def _box(rect, page):
    import pymupdf
    r = pymupdf.Rect(rect)*page.rotation_matrix
    w,h = page.rect.width,page.rect.height
    return [round(max(0,min(1,x)),7) for x in (r.x0/w,r.y0/h,r.x1/w,r.y1/h)]


def _ocr_page(png_bytes, width, height):
    """Recognize one page image. Returns (text, blocks, info) or None when unavailable."""
    from . import ocr
    recognized=ocr.recognize_page(png_bytes)
    if not recognized: return None
    return recognized["text"],recognized["blocks"],recognized["info"]


def parse_page(path, page_number):
    """Read only the requested page. PDF render and extraction share rotation."""
    fmt = path.suffix.lower().lstrip(".")
    text=""; blocks=[]; images=[]; headings=[]; width,height=595,842; ocr_info=None
    if fmt == "pdf":
        import pymupdf
        with pymupdf.open(path) as doc:
            if not 1<=page_number<=len(doc): raise ValueError("页码超出范围")
            p=doc[page_number-1]; width,height=p.rect.width,p.rect.height
            raw=p.get_text("rawdict",sort=True,flags=pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
            for bi,b in enumerate(raw["blocks"]):
                if b["type"] != 0: continue
                start=len(text); chars=[]
                for line in b["lines"]:
                    for span in line["spans"]:
                        for char in span["chars"]:
                            chars.append({"start":len(text),"end":len(text)+len(char["c"]),"text":char["c"],"box":_box(char["bbox"],p)})
                            text+=char["c"]
                    text+="\n"
                end=len(text)
                if text[start:end].strip():
                    blocks.append({"id":f"p{page_number}-b{bi}","start":start,"end":end,"text":text[start:end],"kind":"text","box":_box(b["bbox"],p),"chars":chars})
                text+="\n"
            images=[{"box":_box(i["bbox"],p)} for i in p.get_image_info()]
            rotation=p.rotation
            if not text.strip():
                # A page with no extractable text may be a scan. Recognition only
                # runs with a verified pack; otherwise the page keeps its honest
                # "needs recognition" state.
                scale=min(2,2000/max(width,height))
                pixmap=p.get_pixmap(matrix=pymupdf.Matrix(scale,scale),alpha=False)
                recognized=_ocr_page(pixmap.tobytes("png"),width,height)
                if recognized: text,blocks,ocr_info=recognized
    elif fmt in {"png","jpg","jpeg","webp"}:
        from PIL import Image
        if page_number!=1: raise ValueError("页码超出范围")
        with Image.open(path) as im: width,height=im.size
        rotation=0; images=[{"box":[0,0,1,1]}]
        recognized=_ocr_page(path.read_bytes(),width,height)
        if recognized: text,blocks,ocr_info=recognized
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
    elif fmt=="docx":
        from . import docx_reader
        if page_number!=1: raise ValueError("页码超出范围")
        # Word stores no fixed page layout, so one logical page keeps paragraph
        # and table order without inventing page breaks.
        body=docx_reader.page(path)
        text=body["text"]; blocks=body["blocks"]; headings=body["headings"]; rotation=0
    else: raise ValueError("此格式暂不支持原文定位")
    blank=not bool(text.strip())
    # Only claim recognition is needed where that is actually true. A DOCX with
    # no embedded image is simply a document without text, and a page that was
    # recognized no longer needs anything.
    needs_ocr=blank and fmt not in {"md","txt"} and not ocr_info
    if needs_ocr and fmt=="docx":
        from . import docx_reader
        needs_ocr=docx_reader.has_media(path)
    return {"number":page_number,"width":width,"height":height,"rotation":rotation,"text":text,"blocks":blocks,"images":images,"headings":headings,"text_available":not blank,"ocr_required":needs_ocr,"ocr":ocr_info,"vision_verified":False}


def _parse_file(path):
    if path.suffix.lower()==".pdf":
        import pymupdf
        with pymupdf.open(path) as doc:
            count=len(doc)
            toc=[{"level":x[0],"title":x[1],"page":x[2]} for x in doc.get_toc() if 1<=x[2]<=count]
    else: count=1; toc=[]
    pages=[parse_page(path,n) for n in range(1,count+1)]
    return {"pages":pages,"outline":toc or [h for p in pages for h in p["headings"]],"structure":"原文件书签" if toc else "Markdown 标题" if path.suffix.lower()==".md" else "Word 标题与表格" if path.suffix.lower()==".docx" else "按页","parser":PARSER}


def _save_parse(did, sha, body, reason, page=None):
    pid=uuid.uuid4().hex
    with s.conn() as c:
        c.execute("INSERT INTO ef_document_parses VALUES (?,?,?,?,?,?,?,?)", (pid,did,sha,PARSER,json.dumps(body,ensure_ascii=False),s.now(),reason,page))
    return {**body,"parse_id":pid}


def layout(did, sha=None, parse_id=None):
    d,path=version(did,sha)
    with LOCK:
        with s.conn() as c:
            row=c.execute("SELECT * FROM ef_document_parses WHERE document_id=? AND sha256=?"+(" AND id=?" if parse_id else " AND parser=?")+" ORDER BY created_at DESC,id DESC LIMIT 1",(did,d["sha256"],parse_id or PARSER)).fetchone()
        if parse_id and not row: raise ValueError("解析版本不属于当前文档")
        try: body={**json.loads(row["body"]),"parse_id":row["id"]} if row else _save_parse(did,d["sha256"],_parse_file(path),"读取原件")
        except Exception as exc: raise ValueError("原文件无法解析，可能已损坏、加密或格式不受支持。原文件已保留。") from exc
    return {**body,"document":d}


def view(did, sha=None, parse_id=None):
    data=layout(did,sha,parse_id)
    with s.conn() as c:
        versions=[dict(r) for r in c.execute("SELECT sha256,created_at FROM ef_document_versions WHERE document_id=? ORDER BY created_at DESC",(did,))]
    return {"document":data["document"],"parse_id":data["parse_id"],"parser":PARSER,"page_count":len(data["pages"]),"outline":data["outline"],"structure":data["structure"],"versions":versions,"text_pages":sum(p["text_available"] for p in data["pages"])}


def get_page(did, number, sha=None, parse_id=None):
    data=layout(did,sha,parse_id)
    if not 1<=number<=len(data["pages"]): raise ValueError("页码超出范围")
    return {**data["pages"][number-1],"parse_id":data["parse_id"],"sha256":data["document"]["sha256"]}


def rects_for(page, start, end):
    # Merge adjacent glyphs on a line, keeping distinct rows and vertical text.
    boxes=[ch["box"] for b in page["blocks"] for ch in b["chars"] if ch["end"]>start and ch["start"]<end]
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
    loc={**locator,"sha256":sha,"parse_id":data["parse_id"],"page":n,"source_url":data["document"]["source_url"]}
    status="located" if len(found)==1 else "ambiguous" if found else "unlocated"
    if status=="located":
        start,end=found[0]; actual=p["text"][start:end]
        loc.update(text_range={"start":start,"end":end,"text_sha256":hashlib.sha256(actual.encode()).hexdigest()},rects=rects_for(p,start,end),location_precision="text_range")
    else: loc.update(rects=[],location_precision="page");loc.pop("text_range",None)
    return {"status":status,"matches":len(found),"locator":loc,"is_current":data["document"]["is_current"],"text_available":p["text_available"],"quote":p["text"][found[0][0]:found[0][1]] if status=="located" else quote,"method":"原件文字定位；未调用 OCR 或视觉模型"}


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


def reparse(did, number, sha=None, parse_id=None):
    before=layout(did,sha,parse_id);d,path=version(did,before["document"]["sha256"])
    if d["archived"]: raise ValueError("请先恢复文档")
    if not 1<=number<=len(before["pages"]): raise ValueError("页码超出范围")
    from . import ocr
    if d["format"] in {"png","jpg","jpeg","webp"} and not ocr.capabilities().get("ready"):
        raise ValueError("未连接文字识别，未执行重读")
    t=time.perf_counter(); fresh=parse_page(path,number)
    if not fresh["text_available"]:
        raise ValueError("识别未得到文字，未更改引用" if ocr.capabilities().get("ready")
                         else "当前页没有可提取文字，未连接 OCR；未更改引用")
    body={k:copy.deepcopy(before[k]) for k in ("pages","outline","structure","parser")};old=body["pages"][number-1];body["pages"][number-1]=fresh
    if d["format"] in {"md","docx"}: body["outline"]=fresh["headings"]
    after=_save_parse(did,d["sha256"],body,"用户重读指定页面",number)
    record={"id":str(uuid.uuid4()),"page":number,"sha256":d["sha256"],"before_parse_id":before["parse_id"],"after_parse_id":after["parse_id"],"before":old["text"],"after":fresh["text"],"changed":old["text"]!=fresh["text"],"method":("本机模型识别扫描页；结果待复核" if fresh.get("ocr") else "本机原始文字提取；未调用 OCR 或视觉模型"),"ocr":fresh.get("ocr"),"created_at":s.now(),"duration_ms":round((time.perf_counter()-t)*1000,2)}
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
