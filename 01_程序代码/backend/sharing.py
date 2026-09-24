"""Offline export. No public links, network publishing, scripts, or automatic originals."""
from __future__ import annotations
import hashlib,html,io,json,re,zipfile
from pathlib import Path
from typing import Literal
from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import Field
from . import store as s, scenarios, documents, connections
router=APIRouter(prefix='/api/sharing')
class Share(connections.Strict):
    kind:Literal['analysis','scenario']='analysis'
    id:str
    originals:list[str]=Field(default_factory=list,max_length=30)

def snapshot(req):
    return s.get_run(req.id) if req.kind=='analysis' else scenarios.body('ef_scenario_calculations',req.id)
def eligible(data):
    result={}
    for e in data.get('evidence',[]):
        loc=e.get('locator',{});did,sha=loc.get('document_id'),loc.get('sha256')
        if not did or not sha:continue
        key=did+':'+sha
        if key in result:continue
        try:doc,path=documents.version(did,sha);result[key]={'id':key,'name':doc['name'],'version':sha,'size':path.stat().st_size,'available':True,'path':path}
        except ValueError:result[key]={'id':key,'name':e['source'],'version':sha,'available':False}
    return result
@router.post('/preview')
def preview(req:Share):
    data=snapshot(req);files=eligible(data)
    return {'kind':req.kind,'id':req.id,'title':data.get('question') or '奖金情景','evidence_count':len(data.get('evidence',[])),'originals':[{k:v for k,v in f.items() if k!='path'} for f in files.values()],'scope':'离线文件包，无公开链接','includes':['结果快照','查询条件或假设参数','逐条来源与版本','SHA-256 文件清单'],'warnings':['包内含当前答案、查询结果与来源片段。原件默认不附带。']}
def clean(v):
    if isinstance(v,dict):return {k:clean(x) for k,x in v.items() if k not in {'source_url','path','endpoint','secret','has_secret'}}
    if isinstance(v,list):return [clean(x) for x in v]
    return v
@router.post('/download')
def download(req:Share):
    data=snapshot(req);available=eligible(data)
    if len(set(req.originals))!=len(req.originals) or any(k not in available or not available[k]['available'] for k in req.originals):raise ValueError('选中的原件不可用或不属于此结果')
    if sum(available[k]['size'] for k in req.originals)>80*1024*1024:raise ValueError('原件合计超过 80 MB，请减少选择')
    data=clean(data);data['export_kind']=req.kind;data['exported_at']=s.now()
    entries={};included={}
    for i,key in enumerate(req.originals,1):
        item=available[key];name='originals/'+str(i)+'-'+re.sub(r'[^\w.\-\u4e00-\u9fff]','_',item['name']);entries[name]=item['path'].read_bytes();included[key]=name
    esc=lambda v:html.escape(str(v))
    evidence=[]
    for i,e in enumerate(data.get('evidence',[]),1):
        loc=e.get('locator',{});key=str(loc.get('document_id'))+':'+str(loc.get('sha256'))
        original='<a href="'+esc(included[key])+'">随包原件</a>' if key in included else '<span>原件未附带</span>' if loc.get('document_id') else ''
        evidence.append(f'<section id="source-{esc(e["id"])}"><h3>[{i}] {esc(e["source"])}</h3>{original}<pre>{esc(e.get("snippet",""))}</pre><details><summary>版本与定位</summary><pre>{esc(json.dumps(loc,ensure_ascii=False,indent=2))}</pre></details></section>')
    if req.kind=='analysis':
        content='<h1>'+esc(data['question'])+'</h1>'
        for claim in data.get('claims',[]):
            links=' '.join('<a href="#source-'+esc(eid)+'">来源 '+str(next((i+1 for i,e in enumerate(data['evidence']) if e['id']==eid),'?'))+'</a>' for eid in claim.get('evidence_ids',[]))
            content+='<p>'+esc(claim['text'])+' '+links+'</p>'
        if not data.get('claims'):content+='<p>'+esc(data.get('answer',''))+'</p>'
        spec=data.get('query_spec',{})
        labels={'net_sales':'净销售额','gross_sales':'销售额','growth_rate':'增长率','count':'数量'}
        conditions=[('时间',str(spec['year'])+' 年' if spec.get('year') else '未限定'),('区域',spec.get('region') or '全部区域'),('指标',labels.get(spec.get('metric'),spec.get('metric',''))),('规则','交易当时规则' if spec.get('rule_mode')=='historical' else '当前规则模拟')]
        content+='<h2>查询条件</h2><p>'+ ' · '.join(esc(k)+'：'+esc(v) for k,v in conditions)+'</p><details><summary>全部条件</summary><pre>'+esc(json.dumps(spec,ensure_ascii=False,indent=2))+'</pre></details>'
        rows=data.get('rows',[])
        if rows:
            cols=list(rows[0]);names={'product_id':'产品编号','product_name':'产品','net_sales':'净销售额（元）','order_count':'订单数','region':'区域','year':'年份','customer_count':'客户数','growth_rate':'增长率'};content+='<h2>结果</h2><div class="table"><table><tr>'+''.join('<th>'+esc(names.get(k,k))+'</th>' for k in cols)+'</tr>'+''.join('<tr>'+''.join('<td>'+esc(format(row[k],',.2f').rstrip('0').rstrip('.') if isinstance(row.get(k),(int,float)) else row.get(k,''))+'</td>' for k in cols)+'</tr>' for row in rows)+'</table></div>'
    else:
        content='<h1>奖金情景</h1><p>基准 '+esc(data['baseline_value'])+' 元 · 情景 '+esc(data['value'])+' 元 · 变化 '+esc(data['delta'])+' 元</p><p>情景是假设计算，不代表实际收入。</p>'
        content+='<h2>公式与参数</h2><p>奖金 = max(0, 基础净销售额 × (1 + 增长率) − 起奖门槛) × 奖金比例</p><div class="table"><table><tr><th>参数</th><th>基准</th><th>情景</th><th>单位</th><th>类型</th></tr>'
        for p in data['parameters']:
            unit='比例' if p['unit']=='ratio' else '元'
            content+='<tr>'+''.join('<td>'+esc(v)+'</td>' for v in [p['label'],p['baseline_value'],p['value'],unit,'用户假设' if p['assumption'] else '来源基准'])+'</tr>'
        content+='</table></div><p>'+esc(data.get('contribution_note',''))+'</p><details><summary>分项变化与计算详情</summary><pre>'+esc(json.dumps({k:data.get(k) for k in ['expression','parameters','rule_mode','contributions','rounding']},ensure_ascii=False,indent=2))+'</pre></details>'
    page='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:; base-uri \'none\'"><title>证流 · 离线分析</title><style>body{font:16px/1.7 system-ui,sans-serif;color:#143464;background:#edf5ff;margin:0;padding:32px}main{max-width:980px;margin:auto;padding:32px;background:#fff;border-radius:16px}h1{font-size:26px}h2{font-size:21px;border-top:1px solid #d9e6f5;padding-top:24px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.6 ui-monospace,monospace;background:#f6f9fd;padding:16px}a{color:#1267d0}section{padding:8px 0;border-top:1px solid #e1ecf7}.table{overflow:auto}table{border-collapse:collapse;width:100%}td,th{padding:8px;border:1px solid #d9e6f5;text-align:left}footer{font-size:13px;color:#627591}@media(max-width:600px){body{padding:12px}main{padding:16px}}</style><main><small>证流 EvidenceFlow · 离线快照</small>'+content+'<h2>来源</h2>'+''.join(evidence)+'<footer>导出时间 '+esc(data['exported_at'])+' · 本文件不会更新。原始查询与数值见 snapshot.json，完整性摘要见 manifest.json。</footer></main></html>'
    entries['index.html']=page.encode();entries['snapshot.json']=json.dumps(data,ensure_ascii=False,indent=2).encode()
    manifest={'format':'evidenceflow-offline-v1','created_at':data['exported_at'],'files':[{'path':name,'bytes':len(v),'sha256':hashlib.sha256(v).hexdigest()} for name,v in entries.items()]}
    entries['manifest.json']=json.dumps(manifest,ensure_ascii=False,indent=2).encode()
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
        for name,v in entries.items():z.writestr(name,v)
    return Response(output.getvalue(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="EvidenceFlow-'+req.kind+'-'+req.id[:8]+'.zip"','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
