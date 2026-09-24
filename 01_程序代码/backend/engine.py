"""Deterministic, inspectable planning for the configured local sales workspace."""
from __future__ import annotations
import ast
import copy
import json
import re
import time
import uuid
from decimal import Decimal, ROUND_HALF_UP
from datetime import date
from threading import Event
from . import store as s

FEATURES={"clarification":True,"semantic_validation":True,"evidence_binding":True,"document_reparse":True,"temporal_rules":True,"unit_guard":True}
METRICS={"net_sales":"净销售额","gross_sales":"销售额（未扣退货）","count":"客户与订单数","growth_rate":"净销售额增长率","document":"文档查找","bonus":"奖金","target":"目标达成率","preview":"数据预览"}
REGIONS=["华东","华北","华南","西南"]

def decimal(v): return Decimal(str(v))
def money(v): return "¥"+format(decimal(v).quantize(Decimal(".01"),rounding=ROUND_HALF_UP),",.2f")
def calculate(expression, variables):
    """Small AST evaluator; no calls except max/min; no access, eval or exec."""
    tree=ast.parse(expression,mode="eval")
    if len(list(ast.walk(tree)))>80: raise ValueError("公式过于复杂")
    def visit(n):
        if isinstance(n,ast.Expression): return visit(n.body)
        if isinstance(n,ast.Constant) and type(n.value) in (int,float): return decimal(n.value)
        if isinstance(n,ast.Name) and n.id in variables: return decimal(variables[n.id])
        if isinstance(n,ast.UnaryOp) and isinstance(n.op,(ast.USub,ast.UAdd)): return -visit(n.operand) if isinstance(n.op,ast.USub) else visit(n.operand)
        if isinstance(n,ast.BinOp):
            a,b=visit(n.left),visit(n.right)
            if isinstance(n.op,ast.Add): return a+b
            if isinstance(n.op,ast.Sub): return a-b
            if isinstance(n.op,ast.Mult): return a*b
            if isinstance(n.op,ast.Div):
                if b==0: raise ValueError("分母为零")
                return a/b
        if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in {"max","min"} and not n.keywords and 1<=len(n.args)<=5:
            return (max if n.func.id=="max" else min)(visit(a) for a in n.args)
        raise ValueError("公式包含不支持的内容或缺少参数")
    return visit(tree)
def check_parameters(params):
    expected={"sales":"CNY","threshold":"CNY","rate":"ratio"}
    for p in params:
        if p["name"] in expected and p["unit"]!=expected[p["name"]]: raise ValueError(f"{p['label']}单位不一致")
        if not p.get("evidence_ids") and not p.get("assumption"): raise ValueError(f"{p['label']}没有来源")

def vocab():
    products=[r["product_name"] for r in s.read("SELECT product_name FROM products")[0]]
    years=[int(r["year"]) for r in s.read("SELECT DISTINCT strftime('%Y',order_date) AS year FROM orders ORDER BY year DESC")[0] if r["year"]]
    return products,years

def parse(question, previous=None, overrides=None, flags=None):
    q=question.strip(); previous=previous or {}; overrides=overrides or {}; flags=flags or FEATURES
    products,years=vocab()
    follow=bool(previous) and (len(q)<22 or any(w in q for w in ["那","换成","改成","继续","上面","刚才","它的","这个产品"]))
    # A fully stated time + region is a new scope unless the user refers back.
    # Otherwise a previous resolved product can silently narrow a new total.
    if re.search(r"20\d{2}", q) and any(r in q for r in REGIONS) and not any(w in q for w in ["那", "换成", "改成", "继续", "上面", "刚才", "它的", "这个产品", "该产品"]):
        follow = False
    spec=copy.deepcopy(previous) if follow else {}
    for k in ["resolved_entity","resolved_product_id","origins","unsupported"]: spec.pop(k,None)
    origins={k:"inherited" for k in spec}
    extracted={}
    yr=list(dict.fromkeys(int(x) for x in re.findall(r"20\d{2}",q)))
    if yr: extracted["year"]=yr[0]
    if len(yr)>1: extracted["compare_year"]=yr[1]
    if follow and len(yr)==1 and previous.get("year") and re.search(r"^(与|和|跟|对比).*20\d{2}.*(增长|比)", q):
        extracted["year"] = previous["year"]
        extracted["compare_year"] = yr[0]
    if follow and any(w in q for w in ["它的", "这个产品", "该产品"]):
        if previous.get("resolved_entity") in products: extracted["product"] = previous["resolved_entity"]
    if "今年" in q: extracted["year"]=date.today().year
    if "去年" in q: extracted["year"]=date.today().year-1
    for r in REGIONS:
        if r in q: extracted["region"]=r; break
    if any(w in q for w in ["全域","全部区域","所有区域","全国"]): extracted["region"]=None
    for p in sorted(products,key=len,reverse=True):
        if p in q: extracted["product"]=p; break
    aliases={"云安全":"云安全平台","存储":"智能存储","边缘":"边缘计算节点","数据中台":"数据中台服务"}
    if not extracted.get("product"):
        for alias,p in aliases.items():
            if alias in q and p in products: extracted["product"]=p; break
    applied=[]
    for d in sorted(s.definition_list(),key=lambda d:max(len(n) for n in [d['name']]+d.get('aliases',[]))):
        if any(n.casefold() in q.casefold() for n in [d['name']]+d.get('aliases',[])):
            if d["kind"]=="alias": extracted[d["field"]]=d["target"]
            elif d["kind"]=="metric": extracted["metric"]=d["target"]
            else: continue
            applied.append({k:d[k] for k in ['id','name','aliases','kind','field','target','description','updated_at'] if k in d})
    extracted['definitions_snapshot']=applied
    formula_only=any(w in q for w in ["公式是什么","奖金公式","计算公式","规则是什么"])
    needs_experience=any(w in q for w in ["经验","方法","复盘"])
    if not extracted.get("metric"):
        if formula_only: extracted["metric"]="document"
        elif any(w in q for w in ["同比","增长率","环比"]) or (len(yr)>1 and "增长" in q): extracted["metric"]="growth_rate"
        elif any(w in q for w in ["订单数","客户数","多少客户","多少订单","客户和订单","几笔","几位"]): extracted["metric"]="count"
        elif any(w in q for w in ["达成率","目标完成"]): extracted["metric"]="target"
        elif "未扣退货" in q or "毛销售额" in q: extracted["metric"]="gross_sales"
        elif any(w in q for w in ["净销售额","净销售","销售冠军","最高","排名","卖得最好"]): extracted["metric"]="net_sales"
        elif "销售额" in q: extracted["metric"]="gross_sales"
        elif "奖金" in q: extracted["metric"]="bonus"
        elif any(w in q for w in ["文档","规则","制度","经验","复盘","方法","口径","图片"]): extracted["metric"]="document"
    if not follow and "metric" not in extracted: extracted["metric"]="document"
    if any(w in q for w in ["最高","冠军","排名","第一","最好"]): extracted["asks_top"]=True
    elif any(w in q for w in ["总额","合计","一共","总共"]) or not follow: extracted["asks_top"]=False
    if needs_experience: extracted["needs_experience"]=True
    elif not follow: extracted["needs_experience"]=False
    if "奖金" in q and not formula_only: extracted["needs_formula"]=True
    elif not follow: extracted["needs_formula"]=False
    if "当前规则" in q or "最新规则" in q: extracted["rule_mode"]="current"
    if "历史规则" in q or "当时规则" in q: extracted["rule_mode"]="historical"
    if "同比" in q and "year" in extracted and len(yr)==1: extracted["compare_year"]=extracted["year"]-1
    for k,v in extracted.items(): spec[k]=v; origins[k]="explicit"
    for k,v in overrides.items():
        if k not in {"year","compare_year","region","product","metric","rule_mode","scenario_pct","asks_top","needs_formula","needs_experience","rule_id"}: continue
        if k=="compare_year" and isinstance(v,str) and "," in v:
            a,b=v.split(","); spec["year"]=int(a); v=int(b); origins["year"]="confirmed"
        spec[k]=v; origins[k]="confirmed"
    for k,v in {"metric":"document","region":None,"product":None,"asks_top":False,"needs_formula":False,"needs_experience":False,"rule_mode":"historical","scenario_pct":0,"currency":"CNY","grain":"order"}.items():
        if k not in spec: spec[k]=v; origins[k]="default"
    if spec["metric"] not in METRICS: raise ValueError("不支持的指标")
    if spec.get("region") not in REGIONS+[None,""]: raise ValueError("区域不在当前工作区中")
    if spec.get("product") and spec["product"] not in products: raise ValueError("产品不在当前工作区中")
    for k in ["year","compare_year"]:
        if spec.get(k) is not None:
            spec[k]=int(spec[k])
            if not 2000<=spec[k]<=2100: raise ValueError("年份应在 2000 至 2100 之间")
    spec["scenario_pct"]=float(spec.get("scenario_pct",0))
    if not -100<=spec["scenario_pct"]<=200: raise ValueError("情景调整范围为 -100% 至 +200%")
    if spec["rule_mode"] not in {"historical","current"}: raise ValueError("无效的规则口径")
    spec["origins"]=origins; spec["normalized_question"]=q; spec["formula_only"]=formula_only
    spec["available_years"]=sorted(set(years+[spec["year"]] if spec.get("year") else years))
    spec["available_products"]=products
    if "环比" in q: spec["unsupported"]="当前按年分析，请指定两个年份；月度环比暂未支持。"
    if any(w in q for w in ["销售人员", "销售员", "员工销售", "个人业绩"]):
        spec["unsupported"]="当前订单没有销售人员归属，无法计算个人业绩；项目负责人不能替代销售员。可以按产品比较销售额。"
    return spec

def candidate_blocks(query, focus=None, year=None, formula=False):
    with s.conn() as c:
        rows=[dict(r) for r in c.execute("SELECT b.*,d.name,d.sha256,d.format,d.status FROM blocks b JOIN documents d ON b.document_id=d.id WHERE d.status IN ('ready','needs_review')")]
    terms={x for x in re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9_]+",query)}
    # Chinese bigrams give lexical recall; evidence remains a quoted excerpt, not generated causality.
    terms|={query[i:i+2] for i in range(len(query)-1) if re.fullmatch(r"[\u4e00-\u9fff]{2}",query[i:i+2])}
    scored=[]
    for b in rows:
        if not b["text"].strip() or b["content_type"]=="image": continue
        text=b["title_path"]+" "+b["text"]
        if focus and not any(x in text or x in b["name"] for x in focus): continue
        if year:
            dyears=re.findall(r"20\d{2}",b["name"]+" "+b["title_path"])
            if dyears and str(year) not in dyears: continue
        if formula and not ("公式" in text or "max(" in text): continue
        score=sum(len(t) for t in terms if t in text)
        if score>=2: scored.append((score,b))
    return [x[1] for x in sorted(scored,key=lambda x:(-x[0],x[1]["id"]))[:4]]

def doc_evidence(block, rid):
    evidence={"id":"doc-"+block["id"],"type":"document","source":block["name"],"snippet":block["text"],"status":"text_supported","run_id":rid,
        "locator":{"document_id":block["document_id"],"page":block["page"],"bbox":json.loads(block["bbox_json"]),"title_path":block["title_path"],"sha256":block["sha256"],"source_url":f"/api/documents/{block['document_id']}/original?version={block['sha256']}","location_precision":"page"}}
    from . import documents
    try:
        bound=documents.resolve(evidence["locator"],evidence["snippet"])
        evidence["locator"]=bound["locator"];evidence["location_status"]=bound["status"]
    except ValueError as exc:evidence["location_status"]="unlocated";evidence["location_error"]=str(exc)
    return evidence

def select_rule(year, mode="historical", requested=None):
    rows,_=s.read("SELECT * FROM business_rules ORDER BY effective_from")
    for d in s.definition_list():
        if d["kind"]=="rule":
            rows.append({"rule_id":d["id"],"rule_name":d["name"],"effective_from":d["effective_from"],"effective_to":d.get("effective_to"),"threshold_amount":d["threshold"],"bonus_rate":d["rate"],"source_document":d.get("source","用户确认规则"),"user_defined":True,"definition":d})
    # Current = applicable today, not blindly the largest publication date.
    start=end=date.today().isoformat() if mode=="current" else None
    if mode=="historical": start,end=f"{year}-01-01",f"{year}-12-31"
    overlap=[r for r in rows if r["effective_from"]<=end and (not r["effective_to"] or r["effective_to"]>=start)]
    if requested: overlap=[r for r in overlap if r["rule_id"]==requested]
    if not overlap: return None, "没有找到适用于这段时间的规则", []
    signatures={(str(r["threshold_amount"]),str(r["bonus_rate"])) for r in overlap}
    if len(signatures)>1 or any(r["effective_from"]>start or r["effective_to"] and r["effective_to"]<end for r in overlap):
        return None,"这段时间存在规则冲突或年中切换，请指定规则或缩小时间范围",overlap
    return overlap[-1],None,overlap

def execute(question, previous=None, overrides=None, features=None, cancelled=None, progress=None, persist=True, document_context=None):
    start=time.perf_counter(); rid=str(uuid.uuid4()); flags={**FEATURES,**(features or {})}; cancelled=cancelled or Event()
    spec=parse(question,previous,overrides,flags)
    if not document_context and spec.get("metric")=="document":
        document_context=spec.get("document_context")
    if spec.get("metric")!="document":
        spec.pop("document_context",None)
    if document_context:
        spec.update(metric="document",document_context=document_context,unsupported=None)
    result={"run_id":rid,"question":question,"workspace_id":"sales-demo","query_spec":spec,"status":"completed","answer":"","headline":"","metrics":[],"claims":[],"evidence":[],"events":[],"rows":[],"columns":[],"sql":"","formulas":[],"checks":[],"features":flags,"data_version":s.data_version(),"created_at":s.now()}
    def guard():
        if cancelled.is_set(): raise InterruptedError("查询已取消")
    def step(key,label,fn,tool):
        guard(); t=time.perf_counter()
        if progress: progress(label)
        value=fn()
        result["events"].append({"step_id":key,"label":label,"tool":tool,"status":"completed","duration_ms":round((time.perf_counter()-t)*1000,2),"finished_at":s.now(),"summary":value if isinstance(value,str) else "已完成","outputs":value if isinstance(value,dict) else {}})
        guard(); return value
    def add_evidence(e):
        if all(x["id"]!=e["id"] for x in result["evidence"]): result["evidence"].append(e)
        return e["id"]
    def claim(text,ids=(),status="supported",value=None,unit=None,kind="fact"):
        result["claims"].append({"id":"claim-"+str(len(result["claims"])+1),"text":text,"evidence_ids":list(ids) if flags["evidence_binding"] else [],"status":status,"value":value,"unit":unit,"kind":kind})
    def finish():
        evidence_ids={e["id"] for e in result["evidence"]}
        total=len(result["claims"])
        supported=sum(c["status"] in {"verified","supported","simulation"} and bool(c["evidence_ids"]) and all(e in evidence_ids for e in c["evidence_ids"]) for c in result["claims"])
        result["coverage"]={"supported":supported,"total":total,"percent":round(supported/total*100) if total else None,"meaning":"已绑定有效来源的结论占比；不等于语义准确率"}
        result["answer"]="\n\n".join(c["text"] for c in result["claims"])
        if not result["headline"]: result["headline"]=result["claims"][0]["text"] if result["claims"] else "请补充查询条件"
        result["latency_ms"]=round((time.perf_counter()-start)*1000,2)
        result["conditions"]=conditions(spec)
        result["actions"]=[{"id":"save","label":"保存","enabled":bool(result["claims"]),"reason":None},{"id":"copy","label":"复制","enabled":bool(result["answer"]),"reason":None},{"id":"table","label":"结果表","enabled":bool(result["columns"]),"reason":None},{"id":"recompute","label":"修改条件","enabled":True,"reason":None}]
        if persist: s.snapshot_run(result)
        return result
    step("understand","确认问题与条件",lambda:{"conditions":{k:v for k,v in spec.items() if k not in {"origins","available_products"}}},"intent.parse")
    if spec.get("unsupported"):
        claim(spec["unsupported"],status="needs_evidence",kind="gap"); return finish()
    if spec["metric"]!="document" and not spec.get("year"):
        if flags["clarification"]:
            result["status"]="needs_clarification"; result["clarification"]={"field":"year","title":"要查看哪一年的数据？","message":"先确定时间范围。","options":[{"label":str(y)+" 年","value":y} for y in spec["available_years"][-3:]],"input_type":"year"}
            return finish()
        spec["year"]=max(spec["available_years"]); spec["origins"]["year"]="default"
    if spec["metric"]=="growth_rate" and (not spec.get("compare_year") or spec["compare_year"]==spec.get("year")):
        spec["compare_year"] = None
        if flags["clarification"]:
            result["status"]="needs_clarification"; result["clarification"]={"field":"compare_year","title":"和哪一年比较？","message":"本期为 "+str(spec.get("year"))+" 年。","options":[{"label":str(y)+" 年","value":y} for y in spec["available_years"] if y!=spec["year"]],"input_type":"year"}
            return finish()
        spec["compare_year"]=spec["year"]-1; spec["origins"]["compare_year"]="default"
    if spec["metric"]=="document":
        if document_context:
            from . import documents
            matches=step("retrieve","读取当前文档",lambda:documents.scoped_blocks(document_context,question),"document.scoped_search")
            for evidence in matches:
                evidence["run_id"]=rid;eid=add_evidence(evidence);claim(evidence["snippet"],[eid],kind="excerpt")
            if not matches:claim("当前页没有匹配文字。可更换关键词或选择原文片段；未调用 OCR 或外部模型。",status="needs_evidence",kind="gap")
            result["headline"]="已找到当前文档的原文片段" if matches else "当前页没有匹配文字"
            return finish()
        docs=step("retrieve","查找文档",lambda:candidate_blocks(question,[spec["product"]] if spec.get("product") else None,spec.get("year"),spec["formula_only"]),"document.search")
        for b in docs[:3]:
            eid=add_evidence(doc_evidence(b,rid)); claim(b["text"],[eid],kind="excerpt")
        if not docs: claim("没有找到直接匹配的文档。可以换一个关键词，或导入包含该内容的文件。",status="needs_evidence",kind="gap")
        result["headline"]="找到 "+str(min(len(docs),3))+" 段相关原文" if docs else "暂未找到相关内容"
        return finish()

    year=spec["year"]; region=spec.get("region"); product=spec.get("product")
    where=["strftime('%Y',o.order_date)=?"]; params=[str(year)]
    if region: where.append("o.region=?"); params.append(region)
    if product:
        where.append("o.product_id=(SELECT product_id FROM products WHERE product_name=?)"); params.append(product)
    filter_sql=" AND ".join(where)
    checked_filter, checked_params = filter_sql, list(params)
    if spec["metric"] == "growth_rate":
        checked_filter = " AND ".join(["strftime('%Y',o.order_date) IN (?,?)"] + where[1:])
        checked_params = [str(year), str(spec["compare_year"])] + params[1:]
    amount="o.sales_amount" if spec["metric"]=="gross_sales" else "(o.sales_amount-o.returned_amount)"
    join=spec["asks_top"] or spec["metric"]=="target"
    used=["orders"]+(["products"] if join or product else [])
    step("schema","定位数据表",lambda:{"tables":used,"reason":"订单日期、区域和交易金额来自 orders；产品名称仅在筛选或排名时关联 products","join_path":"orders.product_id → products.product_id" if join else "无需关联" },"schema.inspect")
    checks=[]
    if flags["semantic_validation"]:
        def validate():
            duplicate=s.read("SELECT product_id,COUNT(*) n FROM products GROUP BY product_id HAVING n>1")[0]
            nulls=s.read(f"SELECT COUNT(*) n FROM orders o WHERE {checked_filter} AND (sales_amount IS NULL OR returned_amount IS NULL OR currency IS NULL)",checked_params)[0][0]["n"]
            currency=s.read(f"SELECT DISTINCT o.currency FROM orders o WHERE {checked_filter}",checked_params)[0]
            missing=s.read(f"SELECT COUNT(*) n FROM orders o LEFT JOIN products p ON p.product_id=o.product_id WHERE {checked_filter} AND p.product_id IS NULL",checked_params)[0][0]["n"] if join else 0
            checks.extend([
                {"name":"金额字段","status":"passed" if nulls==0 else "failed","detail":f"{nulls} 条记录存在金额或币种缺失"},
                {"name":"币种","status":"passed" if all(x["currency"]=="CNY" for x in currency) else "failed","detail":"查询范围内币种："+("、".join(str(x["currency"]) for x in currency) or "无记录")},
                {"name":"关联粒度","status":"passed" if not duplicate and not missing else "failed","detail":f"产品键重复 {len(duplicate)} 组，未匹配订单 {missing} 条；金额先按订单事实聚合"},
            ])
            return {"checks":checks}
        step("semantic","检查字段、币种和关联",validate,"semantic.validate")
        result["checks"]=checks
        if any(c["status"]=="failed" for c in checks):
            claim("数据口径检查未通过，未生成金额结论。请查看过程中的具体问题。",status="needs_evidence",kind="gap"); return finish()
    else: result["checks"]=[{"name":"业务校验","status":"skipped","detail":"本次实验关闭校验，不代表已核对"}]
    amount_key = "gross_sales" if spec["metric"]=="gross_sales" else "net_sales"
    if spec["metric"]=="growth_rate":
        period_params=[str(year),str(spec["compare_year"])]
        filters=[]; extra=[]
        if region: filters.append("o.region=?"); extra.append(region)
        if product: filters.append("o.product_id=(SELECT product_id FROM products WHERE product_name=?)"); extra.append(product)
        sql="SELECT "+", ".join(f"SUM(CASE WHEN strftime('%Y',o.order_date)=? THEN {expression} END) AS {alias}" for expression,alias in [(amount,"current_net_sales"),(amount,"compare_net_sales")])+" FROM orders o"+(" WHERE "+" AND ".join(filters) if filters else "")
        params=period_params+extra
    elif spec["metric"]=="count":
        sql=f"SELECT COUNT(DISTINCT o.customer_id) AS customer_count,COUNT(DISTINCT o.order_id) AS order_count FROM orders o WHERE {filter_sql}"
    elif join:
        sql=f"SELECT p.product_id,p.product_name,ROUND(SUM({amount}),2) AS {amount_key},COUNT(DISTINCT o.order_id) AS order_count FROM orders o JOIN products p ON p.product_id=o.product_id WHERE {filter_sql} GROUP BY p.product_id,p.product_name ORDER BY {amount_key} DESC,p.product_id"
    else:
        sql=f"SELECT ROUND(SUM({amount}),2) AS {amount_key},COUNT(DISTINCT o.order_id) AS order_count FROM orders o WHERE {filter_sql}"
    rows,truncated=step("sql","读取查询结果",lambda:s.read(sql,params),"sqlite.readonly")
    result["sql"]=sql; result["rows"]=rows; result["columns"]=list(rows[0]) if rows else []; result["truncated"]=truncated
    eid=add_evidence({"id":"sql-"+rid,"type":"sql_result","source":"订单"+(" · 产品" if join else ""),"snippet":"按当前条件执行的汇总结果；可查看查询语句和贡献订单。","status":"executed","locator":{"sql":sql,"params":params,"columns":result["columns"],"row_count":len(rows),"rows":rows,"contribution_sql":f"SELECT o.* FROM orders o WHERE {checked_filter} ORDER BY o.order_date","contribution_params":checked_params,"version":result["data_version"]}})
    label=f"{year} 年{region or '全域'}"+(" · "+product if product else "")
    first=rows[0] if rows else {}
    if not first or (first.get(amount_key) is None and spec["metric"] not in {"count","growth_rate"}):
        claim(label+"没有匹配的交易记录。",[eid],kind="gap"); return finish()
    if spec["metric"]=="growth_rate":
        current,base=first.get("current_net_sales"),first.get("compare_net_sales")
        if current is None or base is None or base<=0:
            claim("对比数据缺失或基期不大于零，暂不输出增长率。",[eid],status="needs_evidence",kind="gap")
        else:
            rate=calculate("(current-base)/base",{"current":current,"base":base})
            claim(f"{label}相比 {spec['compare_year']} 年，净销售额增长率为 {rate*100:.2f}%。",[eid],"verified",float(rate),"ratio")
            result["metrics"]=[{"label":"本期净销售额","value":current,"unit":"CNY","evidence_ids":[eid]},{"label":"对比期净销售额","value":base,"unit":"CNY","evidence_ids":[eid]},{"label":"增长率","value":float(rate),"unit":"ratio","evidence_ids":[eid]}]
        return finish()
    if spec["metric"]=="count":
        claim(f"{label}有 {first['customer_count']} 个客户、{first['order_count']} 笔订单。",[eid],"verified")
        result["metrics"]=[{"label":"客户","value":first["customer_count"],"unit":"个","evidence_ids":[eid]},{"label":"订单","value":first["order_count"],"unit":"笔","evidence_ids":[eid]}]
        return finish()
    sales=first[amount_key]
    if join:
        if spec["asks_top"] or product:
            spec["resolved_entity"]=first["product_name"]; spec["resolved_product_id"]=first["product_id"]
        if spec["asks_top"]:
            ties=[r["product_name"] for r in rows if r[amount_key]==sales]
            claim(label+METRICS.get(amount_key,"净销售额")+"最高的产品是"+("、".join(ties))+"。",[eid],"verified")
        else: claim(label+"的产品销售结果已找到。",[eid],"verified")
    else: claim(label+METRICS.get(amount_key,"净销售额")+"为 "+money(sales)+"。",[eid],"verified",sales,"CNY")
    result["metrics"]=[{"label":"销售额" if spec["metric"]=="gross_sales" else "净销售额","value":sales,"unit":"CNY","evidence_ids":[eid]},{"label":"订单","value":first["order_count"],"unit":"笔","evidence_ids":[eid]}]

    if spec["metric"]=="target":
        target_sql="SELECT * FROM targets WHERE target_year=?"+(" AND region=?" if region else "")+(" AND product_id=?" if spec.get("resolved_product_id") else "")
        target_params=[year]+([region] if region else [])+([spec["resolved_product_id"]] if spec.get("resolved_product_id") else [])
        targetrows,target_truncated=step("targets","读取同范围目标",lambda:s.read(target_sql,target_params),"sqlite.readonly")
        keys=[(r["target_year"],r["region"],r["product_id"]) for r in targetrows]
        if target_truncated or len(keys)!=len(set(keys)) or any(r["target_amount"] is None or r["target_amount"]<0 for r in targetrows):
            claim("目标数据存在重复、缺失、负值或超出读取范围，未计算达成率。",[eid],status="needs_evidence",kind="gap")
        elif not targetrows or sum(r["target_amount"] for r in targetrows)<=0:
            claim("没有找到对应范围的目标值。",[eid],status="needs_evidence",kind="gap")
        else:
            # Match both product and region. EXISTS avoids multiplying orders by
            # target rows; targets with no sales still belong in the denominator.
            scope_filter,scope_params=filter_sql,list(params)
            if spec.get("resolved_product_id") and not product:
                scope_filter+=" AND o.product_id=?";scope_params.append(spec["resolved_product_id"])
            match="EXISTS (SELECT 1 FROM targets t WHERE t.target_year=? AND t.region=o.region AND t.product_id=o.product_id)"
            matched_sql=f"SELECT COALESCE(ROUND(SUM({amount}),2),0) AS net_sales,COUNT(DISTINCT o.order_id) AS order_count FROM orders o WHERE {scope_filter} AND {match}"
            matched_params=scope_params+[year]
            matched=step("target_base","核对目标与销售范围",lambda:s.read(matched_sql,matched_params)[0],"sqlite.readonly")
            matched_sales=matched[0]["net_sales"]
            target=sum(decimal(r["target_amount"]) for r in targetrows);rate=calculate("sales/target",{"sales":matched_sales,"target":target})
            te=add_evidence({"id":"target-"+rid,"type":"sql_result","source":"年度目标表","snippet":"同一年度、区域及产品的目标；无销售的目标也纳入分母。","status":"executed","locator":{"sql":target_sql,"params":target_params,"rows":targetrows,"columns":list(targetrows[0]),"row_count":len(targetrows),"version":result["data_version"]}})
            me=add_evidence({"id":"target-base-"+rid,"type":"sql_result","source":"有目标的销售范围","snippet":"销售额仅计入具有同年度、同区域、同产品目标的订单。","status":"executed","locator":{"sql":matched_sql,"params":matched_params,"rows":matched,"columns":list(matched[0]),"row_count":1,"version":result["data_version"]}})
            claim("有目标的产品与区域，目标达成率为 "+f"{rate*100:.2f}%"+ "。",[me,te],"verified",float(rate),"ratio")
            result["metrics"]=[{"label":"有目标的净销售额","value":matched_sales,"unit":"CNY","evidence_ids":[me]},{"label":"目标额","value":float(target),"unit":"CNY","evidence_ids":[te]},{"label":"目标达成率","value":float(rate),"unit":"ratio","evidence_ids":[me,te]}]
            excluded_sql=f"SELECT COUNT(DISTINCT o.order_id) AS order_count,COALESCE(ROUND(SUM({amount}),2),0) AS net_sales FROM orders o WHERE {scope_filter} AND NOT {match}"
            excluded=s.read(excluded_sql,matched_params)[0]
            if excluded[0]["order_count"]:
                xe=add_evidence({"id":"target-gap-"+rid,"type":"sql_result","source":"未配置目标的订单","snippet":"这部分销售没有同范围目标，未计入达成率。","status":"executed","locator":{"sql":excluded_sql,"params":matched_params,"rows":excluded,"columns":list(excluded[0]),"row_count":1,"version":result["data_version"]}})
                claim(f"另有 {excluded[0]['order_count']} 笔订单、净销售额 {money(excluded[0]['net_sales'])} 未配置同范围目标，未计入达成率。",[xe],"verified",kind="gap")
    if spec["needs_formula"] or spec["metric"]=="bonus":
        # Bonus rules bind to net sales even when the displayed ranking is gross.
        bonus_sales, bonus_eid = sales, eid
        if amount_key == "gross_sales" or spec["metric"] == "target":
            bonus_filter, bonus_params = filter_sql, list(params)
            if spec.get("resolved_product_id") and not product:
                bonus_filter += " AND o.product_id=?"
                bonus_params.append(spec["resolved_product_id"])
            bonus_sql=f"SELECT ROUND(SUM(o.sales_amount-o.returned_amount),2) AS net_sales FROM orders o WHERE {bonus_filter}"
            bonus_rows,_=step("bonus_base","读取奖金净销售额",lambda:s.read(bonus_sql,bonus_params),"sqlite.readonly")
            bonus_sales=bonus_rows[0]["net_sales"]
            bonus_eid=add_evidence({"id":"bonus-base-"+rid,"type":"sql_result","source":"奖金计算口径 · 净销售额","snippet":"奖金按净销售额计算，扣除退货。","status":"executed","locator":{"sql":bonus_sql,"params":bonus_params,"rows":bonus_rows,"columns":["net_sales"],"row_count":len(bonus_rows),"version":result["data_version"]}})
        def rulework(): return select_rule(year,spec["rule_mode"],spec.get("rule_id")) if flags["temporal_rules"] else select_rule(year,"current",spec.get("rule_id"))
        rule,gap,alternatives=step("rule","匹配有效规则",rulework,"rule.validity")
        if gap:
            claim(gap+"。",[eid],status="needs_evidence",kind="gap")
            result["rule_conflicts"]=alternatives
        else:
            rule_source=None
            if rule.get("user_defined"):
                rule_source=add_evidence({"id":"rule-"+rule["rule_id"],"type":"definition","source":rule["rule_name"],"snippet":json.dumps(rule["definition"],ensure_ascii=False),"status":"user_confirmed","locator":{"version":result["data_version"]}})
            else:
                with s.conn() as c:
                    b=c.execute("SELECT b.*,d.name,d.sha256 FROM blocks b JOIN documents d ON b.document_id=d.id WHERE d.name=? AND d.status='ready' AND b.text LIKE '%max(%' ORDER BY b.id LIMIT 1",(rule["source_document"],)).fetchone()
                if b:
                    b=dict(b)
                    parsed=re.search(r"超过\s*([0-9.]+)\s*(万元|元).*?([0-9.]+)\s*%",b["text"],re.S)
                    if parsed:
                        threshold=decimal(parsed[1])*(10000 if parsed[2]=="万元" else 1); rate=decimal(parsed[3])/100
                        if threshold==decimal(rule["threshold_amount"]) and rate==decimal(rule["bonus_rate"]): rule_source=add_evidence(doc_evidence(b,rid))
            if not rule_source:
                claim("规则表与原文参数不一致或原文缺失，未计算奖金。",[eid],status="needs_evidence",kind="gap")
            else:
                pct=decimal(spec["scenario_pct"]); adjusted=decimal(bonus_sales)*(1+pct/100)
                ps=[{"name":"sales","label":"净销售额","value":str(adjusted),"unit":"CNY","evidence_ids":[bonus_eid],"assumption":pct!=0},{"name":"threshold","label":"门槛","value":str(rule["threshold_amount"]),"unit":"CNY","evidence_ids":[rule_source]},{"name":"rate","label":"比例","value":str(rule["bonus_rate"]),"unit":"ratio","evidence_ids":[rule_source]}]
                if flags["unit_guard"]: check_parameters(ps)
                expression="max(0, sales - threshold) * rate"
                bonus=step("formula","计算奖金",lambda:calculate(expression,{p["name"]:p["value"] for p in ps}).quantize(decimal(".01"),rounding=ROUND_HALF_UP),"formula.decimal_ast")
                simulation=spec["rule_mode"]=="current" or pct!=0
                result["formulas"].append({"expression":expression,"display":"奖金 = max(0, 净销售额 − 门槛) × 比例","parameters":ps,"value":str(bonus),"unit":"CNY","rule_name":rule["rule_name"],"effective_from":rule["effective_from"],"effective_to":rule["effective_to"],"simulation":simulation,"actual_sales":bonus_sales,"scenario_pct":float(pct)})
                claim(("情景模拟：" if simulation else "")+"按 "+rule["effective_from"][:4]+" 年起生效的规则，奖金为 "+money(bonus)+"。",[bonus_eid,rule_source],"simulation" if simulation else "verified",float(bonus),"CNY","formula")
                result["metrics"].append({"label":"模拟奖金" if simulation else "奖金","value":float(bonus),"unit":"CNY","evidence_ids":[bonus_eid,rule_source]})

    if spec["needs_experience"]:
        # Experience is bound to the resolved entity itself.  A project joined
        # through the order table is useful context, but it is not proof that
        # the project's retrospective describes this product.  Keeping project
        # names out of the retrieval focus prevents unrelated project advice
        # from being presented as the product's direct evidence.
        entity=spec.get("resolved_entity") or product
        focus=[entity] if entity else []
        if entity:
            def projects():
                qp=[str(year)]+([region] if region else [])+[spec.get("resolved_product_id") or s.read("SELECT product_id FROM products WHERE product_name=?",[product])[0][0]["product_id"]]
                wh="strftime('%Y',o.order_date)=?"+(" AND o.region=?" if region else "")+" AND o.product_id=?"
                return s.read("SELECT DISTINCT p.project_name FROM orders o JOIN projects p ON p.project_id=o.project_id WHERE "+wh,qp)[0]
            step("projects","定位相关项目",projects,"sqlite.project_link")
        docs=step("experience","查找相关复盘",lambda:candidate_blocks("复盘 经验 方法 "+question,focus or ["不存在的特定实体"]),"document.entity_match")
        docs=[b for b in docs if entity and entity in (b["title_path"]+" "+b["text"]) and any(w in b["title_path"] for w in ["经验","成功","复盘"]) and "证据边界" not in b["title_path"]]
        if docs:
            for b in docs[:2]:
                de=add_evidence(doc_evidence(b,rid)); claim(b["text"],[eid,de],kind="excerpt")
        else: claim("没有找到"+(spec.get("resolved_entity") or product or "当前范围")+"的直接匹配复盘。可以导入相关文档继续查找。",status="needs_evidence",kind="gap")
    return finish()

def conditions(spec):
    rows=[]
    definitions=[
        ("metric","指标",spec["metric"],METRICS[spec["metric"]],[{"value":x,"label":METRICS[x]} for x in ["net_sales","gross_sales","count","growth_rate"]],spec["metric"]!="document"),
        ("year","时间",spec.get("year"),str(spec["year"])+" 年" if spec.get("year") else "待确认",[{"value":y,"label":str(y)+" 年"} for y in spec["available_years"]],True),
        ("region","区域",spec.get("region"),spec.get("region") or "全域",[{"value":None,"label":"全域"}]+[{"value":r,"label":r} for r in REGIONS],True),
        ("product","产品",spec.get("product"),spec.get("product") or "全部产品",[{"value":None,"label":"全部产品"}]+[{"value":r,"label":r} for r in spec["available_products"]],True),
        ("currency","币种","CNY","人民币",[],False),
        ("grain","聚合","order","按订单",[],False),
    ]
    if spec["metric"]=="growth_rate": definitions.append(("compare_year","对比期",spec.get("compare_year"),str(spec.get("compare_year") or "待确认"),[{"value":y,"label":str(y)+" 年"} for y in spec["available_years"] if y!=spec.get("year")],True))
    if spec["needs_formula"] or spec["metric"]=="bonus": definitions.append(("rule_mode","规则",spec["rule_mode"],"当时规则" if spec["rule_mode"]=="historical" else "当前规则 · 模拟",[{"value":"historical","label":"按当时规则"},{"value":"current","label":"按当前规则模拟"}],True))
    for key,label,value,display,options,editable in definitions:
        if spec["metric"]=="document" and key in {"region","currency","grain","product"}: continue
        rows.append({"key":key,"label":label,"value":value,"display":display,"options":options,"editable":editable,"state":spec["origins"].get(key,"default") if value is not None or key in {"region","product"} else "missing"})
    return rows
