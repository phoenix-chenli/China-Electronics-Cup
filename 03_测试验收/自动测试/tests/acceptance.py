"""Stage 10 acceptance: real HTTP, independent Decimal oracle, isolated data.

Run with Python 3.13: 03_测试验收/自动测试/tests/acceptance.py
Browser fixture: append --serve, uses a temporary test database
No external provider calls or production writes.
"""
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import argparse, json, os, shutil, socket, sqlite3, sys, threading, time, uuid, tempfile
import urllib.request, urllib.error

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'01_程序代码'))
sys.path.insert(0,str(ROOT/'01_程序代码/启动与打包'))
from runtime_bootstrap import activate
activate()
parser=argparse.ArgumentParser();parser.add_argument('--serve',action='store_true');parser.add_argument('--report',default='当前综合用例.json');args=parser.parse_args()
OUT=Path(tempfile.mkdtemp(prefix='evidenceflow-acceptance-'))
DATA=OUT/('browser-fixture' if args.serve else 'api-fixture-'+uuid.uuid4().hex[:8])
DATA.mkdir(exist_ok=True);os.environ['EVIDENCEFLOW_DATA_DIR']=str(DATA)
source=ROOT/'02_数据资料/示例数据/business.db'
if not (DATA/'business.db').exists():
    with sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True) as a,sqlite3.connect(DATA/'business.db') as b:a.backup(b)
from backend import main,store as s,conversations as conv,documents
import uvicorn,pymupdf

FACTS=[('营业收入','营业收入：1240000元。'),('退款金额','退款金额：40000元。'),('客户数量','客户数量：36家。'),('合同数量','合同数量：48份。'),('回款周期','回款周期：30天。'),('交付时间','交付时间：2024年12月20日。'),('服务期限','服务期限：12个月。'),('上线步骤','上线步骤：资产盘点、风险分级、分阶段切换。'),('回滚措施','回滚措施：每阶段保留独立回滚窗口。'),('历史门槛','历史门槛：800000元。')]

def prepare():
    main.init_app();conv.initialize()
    path=s.DOCS/'验收_经营摘要_合成数据.pdf'
    if not path.exists():
        pdf=pymupdf.open()
        for page_no in range(2):
            page=pdf.new_page(width=595,height=842)
            page.insert_text((48,48),'证流 · 合成验收数据',fontname='china-s',fontsize=11)
            page.insert_text((48,100),'2024 年度经营摘要' if page_no==0 else '项目交付与执行规则',fontname='china-s',fontsize=22)
            for i,(_,fact) in enumerate(FACTS[page_no*5:page_no*5+5]):page.insert_text((48,170+i*72),fact,fontname='china-s',fontsize=15)
            page.insert_text((48,720),'这些数值仅用于软件验收，不是企业实际财报。',fontname='china-s',fontsize=11)
            page.insert_text((290,800),str(page_no+1),fontsize=10)
        pdf.set_toc([[1,'经营数据',1],[1,'交付与规则',2]]);pdf.save(path);pdf.close()
    doc=s.index_document(path)
    (OUT/'fixtures').mkdir(exist_ok=True)
    (OUT/'fixtures/空表.csv').write_bytes(b'')
    (OUT/'fixtures/损坏文档.pdf').write_bytes(b'%PDF-1.7\nStage10 broken PDF fixture\n')
    (OUT/'fixtures/一千行数据.csv').write_text('编号,金额,说明\n'+''.join(f'{i},{i*1.25},第{i}条合成验收记录\n' for i in range(1,1001)))
    (OUT/'fixtures/长中文问题.txt').write_text('请核对2024年华东销售冠军的净销售额、奖金及成功经验，并逐项提供来源。'*20)
    return doc

DOC=prepare()
if args.serve:
    (OUT/'browser-fixture.json').write_text(json.dumps({'data_dir':str(DATA),'document':DOC,'port':8876},ensure_ascii=False,indent=2))
    uvicorn.run(main.app,host='127.0.0.1',port=8876,log_level='warning');raise SystemExit

sock=socket.socket();sock.bind(('127.0.0.1',0));BASE='http://127.0.0.1:'+str(sock.getsockname()[1])
server=uvicorn.Server(uvicorn.Config(main.app,log_level='error'));thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
for _ in range(500):
    if server.started:break
    time.sleep(.01)
assert server.started
def request(path,body=None,method=None):
    req=urllib.request.Request(BASE+path,data=json.dumps(body).encode() if body is not None else None,method=method,headers={'Content-Type':'application/json'})
    try:r=urllib.request.urlopen(req,timeout=25)
    except urllib.error.HTTPError as e:raise AssertionError(f'{path}: HTTP {e.code} '+e.read().decode())
    return json.load(r)
def query(text,**extra):return request('/api/query',{'question':text,**extra})
def raw(table):
    with sqlite3.connect(s.readonly_uri(s.BUSINESS_DB),uri=True) as c:
        c.row_factory=sqlite3.Row;return [dict(x) for x in c.execute('SELECT * FROM '+s.quoted(table))]
ORDERS=raw('orders');PRODUCTS={x['product_id']:x['product_name'] for x in raw('products')}
def filtered(year,region=None,product=None):return [o for o in ORDERS if o['order_date'].startswith(str(year)) and (not region or o['region']==region) and (not product or PRODUCTS[o['product_id']]==product)]
def total(rows,gross=False):return sum((Decimal(str(o['sales_amount']))-(Decimal(0) if gross else Decimal(str(o['returned_amount']))) for o in rows),Decimal(0))
def same(actual,expected):assert Decimal(str(actual))==Decimal(str(expected)),(actual,str(expected))
def evidence_integrity(r):
    ids={e['id'] for e in r['evidence']}
    assert all(set(c['evidence_ids'])<=ids for c in r['claims'])
    supported=sum(bool(c['evidence_ids']) and c['status']!='needs_evidence' for c in r['claims'])
    assert r['coverage']['supported']==supported
    for e in r['evidence']:
        if e['type']=='document':
            bound=documents.resolve(e['locator'],e['snippet']);assert bound['status']=='located',bound
def session(project='default'):return request('/api/projects/'+project+'/sessions',{})['id']
def ask(sid,text):
    d=request('/api/sessions/'+sid); t=request('/api/tasks',{'session_id':sid,'question':text,'request_id':str(uuid.uuid4()),'parent_message_id':d['selected_message_id']})
    start=time.perf_counter()
    while t['status'] in conv.ACTIVE and time.perf_counter()-start<20:
        time.sleep(.01);t=request('/api/tasks/'+t['id'])
    assert t['status']=='completed',t
    return t['result']
report={'scope':'Stage 10 local deterministic acceptance','started_at':s.now(),'data_dir':str(DATA),'cases':[]}
def check(name,group,fn):
    start=time.perf_counter()
    try:
        detail=fn();item={'id':name,'group':group,'passed':True,'detail':detail}
    except Exception as e:item={'id':name,'group':group,'passed':False,'error':str(e)}
    item['duration_ms']=round((time.perf_counter()-start)*1000,2);report['cases'].append(item)
    print(json.dumps(item,ensure_ascii=False),flush=True)

def sales_case(text,year,region=None,product=None,gross=False,rank=False):
    r=query(text);rows=filtered(year,region,product)
    if rank:
        grouped=defaultdict(list)
        for o in rows:grouped[o['product_id']].append(o)
        expected=sorted([(k,total(v,gross)) for k,v in grouped.items()],key=lambda x:(-x[1],x[0]))
        assert [x['product_id'] for x in r['rows']]==[x[0] for x in expected]
        for got,(_,amount) in zip(r['rows'],expected):same(got['gross_sales' if gross else 'net_sales'],amount)
    else:same(r['rows'][0]['gross_sales' if gross else 'net_sales'],total(rows,gross))
    evidence_integrity(r);return {'question':text,'rows':r['rows'],'run_id':r['run_id']}

for i,(text,year,region,product,gross,rank) in enumerate([
 ('2024年华东净销售额合计是多少？',2024,'华东',None,False,False),
 ('2024年华东未扣退货销售额合计是多少？',2024,'华东',None,True,False),
 ('2024年华北净销售额是多少？',2024,'华北',None,False,False),
 ('2025年全国净销售额合计是多少？',2025,None,None,False,False),
 ('2024年华东云安全平台净销售额是多少？',2024,'华东','云安全平台',False,False),
 ('2024年华东产品净销售额排名',2024,'华东',None,False,True),
 ('2024年华北销售冠军是谁？',2024,'华北',None,False,True),
 ('2024年华南产品净销售额排名',2024,'华南',None,False,True),
 ('2024年全国产品销售额未扣退货排名',2024,None,None,True,True),
 ('2025年西南统一运维套件净销售额是多少？',2025,'西南','统一运维套件',False,False),
],1):check(f'sql-{i:02}','sql',lambda t=text,y=year,r=region,p=product,g=gross,k=rank:sales_case(t,y,r,p,g,k))

def counts():
    r=query('2024年华东有多少客户和订单？');rows=filtered(2024,'华东');same(r['rows'][0]['customer_count'],len({o['customer_id'] for o in rows}));same(r['rows'][0]['order_count'],len(rows));evidence_integrity(r);return r['rows']
check('sql-11-distinct-customers','sql',counts)
def growth():
    r=query('2025年华东净销售额比2024年增长多少？');expected=(total(filtered(2025,'华东'))/total(filtered(2024,'华东'))-1);assert abs(Decimal(str(r['metrics'][-1]['value']))-expected)<Decimal('0.000000001');return {'expected':str(expected),'actual':r['metrics'][-1]['value']}
check('sql-12-growth','sql',growth)
def bonus():
    r=query('2024年华东云安全平台奖金是多少？');amount=total(filtered(2024,'华东','云安全平台'));expected=(max(0,amount-800000)*Decimal('.025')).quantize(Decimal('.01'),rounding=ROUND_HALF_UP);same(r['formulas'][0]['value'],expected);assert '净销售额为' in r['claims'][0]['text'];assert '奖金为 ¥900,000' not in r['answer'];evidence_integrity(r);return {'net_sales':str(amount),'expected_bonus':str(expected),'actual':r['formulas'][0]['value']}
check('sql-13-bonus','sql',bonus)
def target_all():
    r=query('2024年华东全部产品目标完成率是多少？');targets=[t for t in raw('targets') if t['target_year']==2024 and t['region']=='华东'];amount=total([o for o in filtered(2024,'华东') if o['product_id'] in {t['product_id'] for t in targets}]);expected=amount/sum(Decimal(str(t['target_amount'])) for t in targets);metric=next(m for m in r['metrics'] if m['label']=='目标达成率');assert abs(Decimal(str(metric['value']))-expected)<Decimal('0.000000001'),(metric,str(expected));return {'expected':str(expected),'actual':metric['value']}
check('sql-14-target-grain','sql',target_all)

def target_and_bonus():
    r=query('2024年华东全部产品目标完成率和奖金是多少？')
    amount=total(filtered(2024,'华东'))
    expected=(max(0,amount-800000)*Decimal('.025')).quantize(Decimal('.01'))
    same(r['formulas'][0]['value'],expected)
    sales_param=next(p for p in r['formulas'][0]['parameters'] if p['name']=='sales')
    same(sales_param['value'],amount)
    source=next(e for e in r['evidence'] if e['id']==sales_param['evidence_ids'][0])
    same(source['locator']['rows'][0]['net_sales'],amount)
    evidence_integrity(r)
    return {'bonus_base':str(amount),'bonus':str(expected),'target_ratio':next(m['value'] for m in r['metrics'] if m['label']=='目标达成率')}
check('sql-17-target-and-bonus-independent-bases','sql',target_and_bonus)

def target_region_alignment():
    r=query('2024年全国全部产品目标完成率是多少？')
    targets=[t for t in raw('targets') if t['target_year']==2024]
    keys={(t['region'],t['product_id']) for t in targets}
    amount=total([o for o in filtered(2024) if (o['region'],o['product_id']) in keys])
    expected=amount/sum(Decimal(str(t['target_amount'])) for t in targets)
    metric=next(m for m in r['metrics'] if m['label']=='目标达成率')
    assert abs(Decimal(str(metric['value']))-expected)<Decimal('0.000000001')
    assert '未配置同范围目标' in r['answer'];evidence_integrity(r)
    assert all(e['locator'].get('sql') for e in r['evidence'] if e['type']=='sql_result')
    return {'expected':str(expected),'actual':metric['value'],'answer':r['answer']}
check('sql-15-target-region-alignment','sql',target_region_alignment)
def duplicate_target():
    with sqlite3.connect(s.BUSINESS_DB) as c:c.execute("INSERT INTO targets VALUES ('QA-DUP',2024,'华东','P001',550000,'验收重复目标')")
    try:
        r=query('2024年华东云安全平台目标完成率是多少？')
        assert '未计算达成率' in r['answer']
        assert not any(m['label']=='目标达成率' for m in r['metrics'])
        return r['answer']
    finally:
        with sqlite3.connect(s.BUSINESS_DB) as c:c.execute("DELETE FROM targets WHERE target_id='QA-DUP'")
check('sql-16-target-duplicate-abstention','sql',duplicate_target)


for i,(term,quote) in enumerate(FACTS):
    def doc_case(i=i,term=term,quote=quote):
        scope={'document_id':DOC['id'],'sha256':DOC['sha256'],'page':i//5+1};r=query(term+'是什么？',document_context=scope)
        assert quote in r['answer'],r['answer'];assert r['evidence']
        for e in r['evidence']:
            assert e['locator']['document_id']==DOC['id'] and e['locator']['sha256']==DOC['sha256'] and e['locator']['page']==scope['page']
        evidence_integrity(r);return {'question':term+'是什么？','expected_quote':quote,'page':scope['page'],'sha256':DOC['sha256'],'answer':r['answer']}
    check(f'doc-{i+1:02}','document',doc_case)

def no_match():
    r=query('火星矿石开采许可证是什么？',document_context={'document_id':DOC['id'],'sha256':DOC['sha256'],'page':1});assert not r['evidence'];assert '没有匹配文字' in r['answer'];return r['answer']
check('doc-11-abstention','document',no_match)
def clarification():
    r=query('华东销售冠军是谁？');assert r['status']=='needs_clarification' and r['clarification']['field']=='year';assert not r['sql'];return r['clarification']['title']
check('missing-year','boundary',clarification)
def five_turns():
    a=request('/api/projects',{'name':'五轮连续验收'})['id'];sid=session(a);results=[]
    sequence=[('2024年华东云安全平台净销售额是多少？','华东',2024),('它的奖金是多少？','华东',2024),('那华北呢？','华北',2024),('换成华南','华南',2024),('换成2025年','华南',2025)]
    for text,region,year in sequence:
        r=ask(sid,text);assert r['query_spec']['region']==region and r['query_spec']['year']==year;assert r['query_spec']['product']=='云安全平台';results.append({'question':text,'spec':r['query_spec'],'answer':r['answer']})
    detail=request('/api/sessions/'+sid);assert len(detail['messages'])==10
    other=request('/api/projects',{'name':'独立上下文验收'})['id'];blank=ask(session(other),'它的奖金是多少？');assert blank['status']=='needs_clarification';assert blank['query_spec']['region'] is None
    assert request('/api/sessions/'+sid)['messages']==detail['messages'];return {'turns':results,'other_project':blank['status'],'messages':10}
check('five-turns-project-isolation','conversation',five_turns)
def concurrent():
    with ThreadPoolExecutor(max_workers=4) as pool:
        rs=list(pool.map(lambda _:query('2024年华東销售额是多少？'.replace('華東','华东').replace('华東','华东')),range(8)))
    assert len({r['run_id'] for r in rs})==8
    for r in rs:same(r['rows'][0]['gross_sales'],total(filtered(2024,'华东'),True))
    return {'queries':8,'workers':4,'unique_run_ids':8}
check('concurrent-eight','scale',concurrent)
def scale(n):
    with sqlite3.connect(s.BUSINESS_DB) as c:
        c.execute("DELETE FROM orders WHERE order_id LIKE 'S10-%'")
        c.executemany('INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)',[(f'S10-{i}','2028-04-10','C001','P001','PR001','华东',1,100+(i%7),i%3,'CNY') for i in range(n)])
    expected=sum((Decimal(100+i%7)-Decimal(i%3) for i in range(n)),Decimal(0));timings=[]
    for _ in range(3):
        start=time.perf_counter();r=query('2028年华东净销售额是多少？');timings.append(round((time.perf_counter()-start)*1000,2));same(r['rows'][0]['net_sales'],expected);same(r['rows'][0]['order_count'],n)
    return {'orders':n,'expected':str(expected),'http_ms':timings,'database_bytes':s.BUSINESS_DB.stat().st_size}
for n in [1000,10000,100000]:check('scale-'+str(n),'scale',lambda n=n:scale(n))
server.should_exit=True;thread.join(5);conv.manager.pool.shutdown(wait=True)
report.update(finished_at=s.now(),passed=sum(x['passed'] for x in report['cases']),total=len(report['cases']),boundary='Synthetic deterministic tests. Document cases verify retrieval and exact source positions, not open-ended model reasoning; 100k scale is bounded local measurement, not production concurrency certification.')
(ROOT/'03_测试验收/验收证据'/args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({'passed':report['passed'],'total':report['total'],'report':str(ROOT/'03_测试验收/验收证据'/args.report)}))
raise SystemExit(0 if report['passed']==report['total'] else 1)
