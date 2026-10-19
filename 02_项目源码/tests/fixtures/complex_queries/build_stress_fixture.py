"""Deterministic synthetic boundary fixture. Not a public or business dataset."""
from pathlib import Path
import hashlib,json,sqlite3
ROOT=Path(__file__).parent
path=ROOT/'stress.sqlite'
if path.exists():
    raise SystemExit('Existing fixture is frozen; do not overwrite it.')
with sqlite3.connect(path) as c:
    c.execute('PRAGMA foreign_keys=ON')
    # Catalog order places business tables after many plausible distractors.
    for index in range(30):
        topic=['customer_badge','dispatch_archive','inventory_note','invoice_cache','join_snapshot','staff_badge'][index%6]
        name=f'aux_{index:02d}_{topic}'
        c.execute(f'CREATE TABLE {name}(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount NUMERIC,recorded_at TEXT)')
        c.executemany(f'INSERT INTO {name} VALUES(?,?,?,?,?)',[(n,n,f'合成干扰记录{index}-{n}',9000+index*100+n,'2025-01-01') for n in range(1,5)])
    c.executescript('''
    CREATE TABLE customer_profile(customer_id INTEGER PRIMARY KEY,customer_name TEXT NOT NULL,district TEXT NOT NULL);
    CREATE TABLE technician(technician_id INTEGER PRIMARY KEY,technician_name TEXT NOT NULL,workshop TEXT NOT NULL);
    CREATE TABLE catalog_item(item_id INTEGER PRIMARY KEY,item_name TEXT NOT NULL,item_type TEXT NOT NULL);
    CREATE TABLE work_order(order_id INTEGER PRIMARY KEY,customer_id INTEGER NOT NULL REFERENCES customer_profile(customer_id),technician_id INTEGER NOT NULL REFERENCES technician(technician_id),service_date TEXT NOT NULL,status TEXT NOT NULL);
    CREATE TABLE work_item(line_id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL REFERENCES work_order(order_id),item_id INTEGER NOT NULL REFERENCES catalog_item(item_id),quantity INTEGER NOT NULL,unit_price NUMERIC NOT NULL,discount NUMERIC NOT NULL DEFAULT 0);
    ''')
    c.executemany('INSERT INTO customer_profile VALUES(?,?,?)',[(1,'合成客户甲','城东'),(2,'合成客户乙','城西'),(3,'合成客户丙','城东'),(4,'合成客户丁','城北')])
    c.executemany('INSERT INTO technician VALUES(?,?,?)',[(1,'张伟','东店'),(2,'张伟','西店'),(3,'李明','东店'),(4,'王芳','北店')])
    c.executemany('INSERT INTO catalog_item VALUES(?,?,?)',[(1,'机油','配件'),(2,'滤芯','配件'),(3,'工时','服务'),(4,'轮胎','配件'),(5,'检测','服务')])
    orders=[(101,1,1,'2025-01-05','已完成'),(102,2,2,'2025-01-08','已完成'),(103,3,3,'2025-02-03','已完成'),(104,4,4,'2025-02-15','已完成'),(105,1,2,'2025-03-04','已完成'),(106,2,1,'2025-03-09','已取消'),(107,3,3,'2025-03-10','进行中'),(108,4,2,'2024-12-30','已完成'),(109,2,2,'2025-04-11','已完成'),(110,3,1,'2025-04-15','已完成'),(111,1,4,'2025-05-06','已完成'),(112,4,3,'2025-06-08','已完成')]
    c.executemany('INSERT INTO work_order VALUES(?,?,?,?,?)',orders)
    lines=[(1,101,1,2,80,0),(2,101,3,1,120,0),(3,102,2,1,100,0.1),(4,102,3,2,120,0),(5,103,4,2,400,0.05),(6,103,3,1,150,0),(7,104,5,1,60,0),(8,105,1,3,90,0.1),(9,105,2,1,110,0),(10,105,3,2,100,0),(11,106,4,4,400,0),(12,107,1,10,80,0),(13,108,4,1,450,0),(14,109,4,1,420,0),(15,109,5,1,70,0),(16,110,2,2,90,0),(17,110,3,1,130,0),(18,111,4,1,410,0),(19,111,3,1,120,0),(20,112,1,1,85,0),(21,112,3,1,110,0)]
    c.executemany('INSERT INTO work_item VALUES(?,?,?,?,?,?)',lines)
    assert not c.execute('PRAGMA foreign_key_check').fetchall()

cases=[]
def result(sql):
    with sqlite3.connect(path) as c:
        cur=c.execute(sql)
        return {'columns':[d[0] for d in cur.description],'rows':[list(r) for r in cur.fetchall()]}
def case(ident,question,sql,tags,status='completed',notes=''):
    data={'id':ident,'database':'stress','question':question,'expected_status':status,'reference_sql':sql,'parameters':[],'comparison':{'rows':'ordered' if sql and 'ORDER BY' in sql else 'unordered','columns':'position_ignore_alias','numeric_absolute_tolerance':0.005},'tags':tags,'notes':notes}
    if sql:data['expected']=result(sql)
    cases.append(data)
    return data
case('stress_01','2025年已完工的维修单，各位师傅分别接了多少单？输出技师编号、姓名、门店和完成单数，按技师编号升序。',"SELECT t.technician_id,t.technician_name,t.workshop,COUNT(w.order_id) FROM technician t LEFT JOIN work_order w ON w.technician_id=t.technician_id AND w.status='已完成' AND w.service_date>='2025-01-01' AND w.service_date<'2026-01-01' GROUP BY t.technician_id,t.technician_name,t.workshop ORDER BY t.technician_id",['many_tables','natural_alias','schema_linking','left_join'],'completed','维修单=work_order，师傅=technician；aux表中的join_id不是关联声明，不可拿来拼接业务。')
case('stress_02','按客户所在区统计2025年已完成维修单：维修单数、配件折后收入和全部项目折后收入分别是多少？只显示配件折后收入超过300的区，金额两位小数，按区名升序。折后收入按明细单价×数量×(1-折扣)算。',"SELECT c.district,COUNT(DISTINCT w.order_id),ROUND(SUM(CASE WHEN p.item_type='配件' THEN i.unit_price*i.quantity*(1-i.discount) ELSE 0 END),2),ROUND(SUM(i.unit_price*i.quantity*(1-i.discount)),2) FROM customer_profile c JOIN work_order w ON w.customer_id=c.customer_id JOIN work_item i ON i.order_id=w.order_id JOIN catalog_item p ON p.item_id=i.item_id WHERE w.status='已完成' AND w.service_date>='2025-01-01' AND w.service_date<'2026-01-01' GROUP BY c.district HAVING SUM(CASE WHEN p.item_type='配件' THEN i.unit_price*i.quantity*(1-i.discount) ELSE 0 END)>300 ORDER BY c.district",['many_tables','join','grain','conditional_aggregate','having'],'completed','不能以明细行数冒充工单数；配件和所有项目收入分母范围不同。')
ambiguous=case('stress_03','张伟2025年已完成的维修单一共带来多少折后收入？按明细单价×数量×(1-折扣)计算，保留两位小数。',None,['many_tables','same_name','clarification'],'needs_clarification','technician_id=1 东店、technician_id=2 西店均名张伟，应澄清身份，不能默认第一行或合并二人。')
sql="SELECT ROUND(SUM(i.unit_price*i.quantity*(1-i.discount)),2) FROM work_order w JOIN work_item i ON i.order_id=w.order_id WHERE w.technician_id=2 AND w.status='已完成' AND w.service_date>='2025-01-01' AND w.service_date<'2026-01-01'"
ambiguous['clarification_resolution']={'question':'选技师编号2，也就是西店的张伟，其他条件不变。','expected_status':'completed','reference_sql':sql,'parameters':[],'expected':result(sql),'comparison':{'rows':'unordered','columns':'position_ignore_alias','numeric_absolute_tolerance':0.005},'notes':'必须继承2025年、已完成、折后收入口径，只补充tech ID=2。'}
payload={'version':1,'purpose':'合成几十表边界验证，与公开陌生库成绩分开报告','synthetic':True,'sample_notice':'全部业务名、客户和交易均为程序构造的合成测试夹具；不是公开真实数据，不得作为实际业务案例宣传。','frozen_before_model_execution':True,'database':{'id':'stress','path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'tables':35,'business_tables':5,'distractor_tables':30,'declared_foreign_keys':4,'builder':'build_stress_fixture.py'},'cases':cases}
out=ROOT/'stress-cases.frozen.json';out.write_text(json.dumps(payload,ensure_ascii=False,indent=2));digest=hashlib.sha256(out.read_bytes()).hexdigest();(ROOT/'stress-cases.frozen.sha256').write_text(digest+'  stress-cases.frozen.json\n')
print(json.dumps({'path':str(out),'sha256':digest,'expected':{c['id']:c.get('expected') for c in cases},'clarification_resolution':ambiguous['clarification_resolution']['expected']},ensure_ascii=False))
