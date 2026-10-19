"""Eight explicit development chains, each with eight consecutive real requests.

Keys derive from unchanged public originals or declared synthetic originals.
They are not inferred from model answers. Branch/version probes are separate.
"""
from __future__ import annotations


def context_cases():
    cases=[]
    def add(chain,question,sources,*,sql=None,status='completed',facts=None,conditions=(),forbidden=(),patch=None,mode='database',pages=(),extra=None):
        index=sum(c['chain_id']==chain for c in cases)+1
        ident=f'{chain}-t{index}'
        entry=dict(id=ident,chain_id=chain,turn_index=index,question=question,source_ids=list(sources),mode=mode,
                   tags=['context','eight_turn_chain',chain],split='development')
        if index>1:entry['parent_case_id']=f'{chain}-t{index-1}'
        if patch:entry['context_patch']=patch
        key=dict(status=status,require_evidence=status=='completed',require_text_review=True,require_context_review=True,
                 chain_id=chain,turn_index=index,review_requirements=dict(facts=facts or [],context_conditions=list(conditions),
                  forbidden=list(forbidden),source_pages=list(pages),interpretation_origin='model_interpretation is not user confirmation'))
        if sql:key['reference_sql']=sql
        if extra:key.update(extra)
        cases.append({**entry,'private_key':key})
    # C1: real public teaching schema, explicit time/country replacement.
    c='ctx01';s=['chinook']
    add(c,'只统计2024年BillingCountry为USA的Invoice.Total合计，保留两位小数。',s,
        sql="SELECT ROUND(SUM(Total),2) FROM Invoice WHERE InvoiceDate>='2024-01-01' AND InvoiceDate<'2025-01-01' AND BillingCountry='USA'",
        conditions=['2024年','账单国家USA','发票头Total合计'],forbidden=['称为利润','补造币种'])
    add(c,'把年份改成2025年，其他口径不变。',s,
        sql="SELECT ROUND(SUM(Total),2) FROM Invoice WHERE InvoiceDate>='2025-01-01' AND InvoiceDate<'2026-01-01' AND BillingCountry='USA'",
        conditions=['2025年替换2024年','USA','Total合计'])
    add(c,'加入Canada，与USA分别列出国家和金额，按国家名升序。',s,
        sql="SELECT BillingCountry,ROUND(SUM(Total),2) FROM Invoice WHERE InvoiceDate>='2025-01-01' AND InvoiceDate<'2026-01-01' AND BillingCountry IN('USA','Canada') GROUP BY BillingCountry ORDER BY BillingCountry",
        conditions=['2025年','USA或Canada','按国家分组'])
    add(c,'只看Canada，只给合计金额。',s,
        sql="SELECT ROUND(SUM(Total),2) FROM Invoice WHERE InvoiceDate>='2025-01-01' AND InvoiceDate<'2026-01-01' AND BillingCountry='Canada'",
        conditions=['2025年','Canada替换国家集合','Total合计'])
    add(c,'去掉国家限制，其他条件保持，只给金额合计。',s,
        sql="SELECT ROUND(SUM(Total),2) FROM Invoice WHERE InvoiceDate>='2025-01-01' AND InvoiceDate<'2026-01-01'",
        conditions=['2025年','不限制国家','Total合计'])
    monthly="FROM Invoice WHERE InvoiceDate>='2025-01-01' AND InvoiceDate<'2026-01-01' GROUP BY strftime('%Y-%m',InvoiceDate) ORDER BY 1"
    add(c,'改为按月列出年月和金额，年月升序；没有记录的月份不补造零。',s,
        sql="SELECT strftime('%Y-%m',InvoiceDate),ROUND(SUM(Total),2) "+monthly,
        conditions=['2025年','所有国家','每月Total合计'],forbidden=['把未记录月份当零'])
    add(c,'把指标改为发票张数，其他不变。',s,
        sql="SELECT strftime('%Y-%m',InvoiceDate),COUNT(*) "+monthly,
        conditions=['2025年','所有国家','每月COUNT发票头'])
    add(c,'撤销上一轮指标修改，回到按月金额。',s,
        sql="SELECT strftime('%Y-%m',InvoiceDate),ROUND(SUM(Total),2) "+monthly,
        conditions=['恢复第6轮Total合计','2025年','按月','所有国家'])
    # C2: controlled fanout, amount definition and denominator changes.
    c='ctx02';s=['boundary']
    add(c,'统计2025年已完成订单：每个客户的订单头total_cents合计，单位分；NULL未知不补零。输出客户编号、金额，编号升序。',s,
        sql="SELECT customer_id,SUM(total_cents) FROM orders WHERE status='已完成' AND day>='2025-01-01' AND day<'2026-01-01' GROUP BY customer_id ORDER BY customer_id",
        conditions=['2025年已完成','订单头粒度','SUM忽略NULL但不将全NULL变0','单位分'])
    add(c,'只保留在该期间已完成订单里买过P-A的客户；仍合计这些客户全部已完成订单头金额。',s,
        sql="SELECT o.customer_id,SUM(o.total_cents) FROM orders o WHERE o.status='已完成' AND o.day>='2025-01-01' AND o.day<'2026-01-01' AND EXISTS(SELECT 1 FROM orders x JOIN lines l ON l.order_id=x.id WHERE x.customer_id=o.customer_id AND x.status='已完成' AND x.day>='2025-01-01' AND x.day<'2026-01-01' AND l.sku='P-A') GROUP BY o.customer_id ORDER BY o.customer_id",
        conditions=['客户资格半连接','订单头金额不能被明细放大','2025已完成'])
    line25="FROM orders o JOIN lines l ON l.order_id=o.id WHERE o.status='已完成' AND o.day>='2025-01-01' AND o.day<'2026-01-01' AND l.sku='P-A' GROUP BY o.customer_id"
    amount='SUM(l.qty*l.price_cents*(1-l.discount_bp/10000.0))'
    add(c,'金额改成P-A明细折后金额，qty乘price_cents再乘(1-discount_bp/10000)，单位分、两位小数；按客户编号列出。',s,
        sql=f'SELECT o.customer_id,ROUND({amount},2) '+line25+' ORDER BY o.customer_id',
        conditions=['P-A明细粒度','2025已完成','折扣基点/10000','分'])
    ratio=f'WITH x AS(SELECT o.customer_id,{amount} amount '+line25+") SELECT customer_id,ROUND(100.0*amount/NULLIF((SELECT SUM(amount) FROM x),0),2) FROM x ORDER BY customer_id"
    add(c,'改为列每个客户占上述P-A金额总额的比例百分数，两位小数；分母只用当前范围。',s,
        sql=ratio,conditions=['2025已完成P-A折后金额','当前范围总额作分母','百分数'])
    add(c,'将时期改为2024年，比例定义和P-A限制保留。',s,sql=ratio.replace('2025-01-01','2024-01-01').replace('2026-01-01','2025-01-01'),
        conditions=['2024替换2025','P-A','重新计算2024分母'])
    add(c,'去掉P-A限制，仍按该年全部明细折后金额计算各客户占比。',s,
        sql=ratio.replace('2025-01-01','2024-01-01').replace('2026-01-01','2025-01-01').replace(" AND l.sku='P-A'",''),
        conditions=['2024','全部SKU','折后明细金额比例'])
    add(c,'暂不计算占比：单看2025年已完成订单头，total_cents为NULL和等于0分别多少张？依次输出两项。',s,
        sql="SELECT SUM(total_cents IS NULL),SUM(total_cents=0) FROM orders WHERE status='已完成' AND day>='2025-01-01' AND day<'2026-01-01'",
        conditions=['2025已完成订单头','NULL与0分开','不沿用比例或SKU过滤'])
    add(c,'恢复第4轮的2025年P-A比例口径，取比例最高的前三位客户；同值按编号升序。',s,
        sql=ratio.rsplit('ORDER BY',1)[0]+'ORDER BY 2 DESC,customer_id LIMIT 3',
        conditions=['恢复第4轮范围和分母','2025已完成P-A','比例前3','客户编号破同值'])
    # C3: same-name ambiguity, explicit recovery, absent cost.
    c='ctx03';s=['boundary']
    add(c,'张伟一共登记了多少工时？',s,status=['needs_clarification','insufficient_evidence'],
        facts=['实际存在东店编号1与西店编号2两位张伟，须区分对象'],conditions=['姓名歧义未解决'],forbidden=['随机选一位','把两人静默合并'])
    add(c,'选择西店、员工编号2；按worklogs.hours求和，NULL未知不补零。',s,
        sql='SELECT SUM(CAST(hours AS REAL)) FROM worklogs WHERE staff_id=2',conditions=['员工2西店张伟','工时SUM','无年份限制'])
    add(c,'改成东店、员工编号1，其他保持。',s,sql='SELECT SUM(CAST(hours AS REAL)) FROM worklogs WHERE staff_id=1',
        conditions=['员工1替换2','工时SUM'])
    add(c,'只保留关联orders.day在2025年的工时。',s,
        sql="SELECT SUM(CAST(w.hours AS REAL)) FROM worklogs w JOIN orders o ON o.id=w.order_id WHERE w.staff_id=1 AND o.day>='2025-01-01' AND o.day<'2026-01-01'",
        conditions=['员工1','关联订单日2025','不能拿员工ID当金额'])
    add(c,'分别列出他在2024年、2025年的工时，按年份升序。',s,
        sql="SELECT strftime('%Y',o.day),SUM(CAST(w.hours AS REAL)) FROM worklogs w JOIN orders o ON o.id=w.order_id WHERE w.staff_id=1 AND strftime('%Y',o.day) IN('2024','2025') GROUP BY 1 ORDER BY 1",
        conditions=['员工1','按关联订单年','2024和2025分列'])
    add(c,'请计算他2025年带来的利润，给出利润金额；没有依据时指出缺项。',s,status=['needs_clarification','insufficient_evidence'],
        facts=['原件没有成本/利润定义，工时不等于利润'],conditions=['员工1','2025','利润未定义'],forbidden=['工时或订单金额冒称利润'])
    add(c,'不算利润，回到第4轮，只给员工编号1的2025年工时。',s,
        sql="SELECT SUM(CAST(w.hours AS REAL)) FROM worklogs w JOIN orders o ON o.id=w.order_id WHERE w.staff_id=1 AND o.day>='2025-01-01' AND o.day<'2026-01-01'",
        conditions=['明确恢复工时','员工1','2025','未采用利润猜测'])
    add(c,'去掉员工限制，仍按2025年关联订单统计总工时，NULL未知不补零。',s,
        sql="SELECT SUM(CAST(w.hours AS REAL)) FROM worklogs w JOIN orders o ON o.id=w.order_id WHERE o.day>='2025-01-01' AND o.day<'2026-01-01'",
        conditions=['全部员工','2025','SUM已记录工时'])
    # C4: known public original, explicitly development, direct source keys.
    c='ctx04';s=['shijingshan']
    pub=lambda q,facts,conditions,pages=(1,2),**kw:add(c,q,s,mode='document',facts=facts,conditions=conditions,pages=pages,**kw)
    pub('根据公报，2024年石景山区地区生产总值及按不变价格计算的同比增速是多少？',
        ['地区生产总值1312.9亿元','按不变价格计算同比增长6.5%','初步核算限定'],['石景山区','2024','GDP原值与同比'])
    pub('改看第三产业，保留上述年份和两项指标。',['第三产业增加值1222.1亿元','按不变价格增长7.3%'],['2024','第三产业替换GDP'],pages=[1,2,4])
    pub('再改为第二产业。',['第二产业增加值90.8亿元','按不变价格下降3.1%'],['2024','第二产业替换第三产业'],forbidden=['负增长说成增长3.1%'])
    pub('只比较这两个产业的增加值，按第二产业、第三产业顺序给出。',['第二产业90.8亿元','第三产业1222.1亿元'],['2024','对象为第二和第三产业','只问增加值','顺序固定'])
    pub('现在只看2023年第三产业增加值；不允许用2024年和增速倒推冒充原文值。',[],['2023','第三产业','只能读直接记载'],status='insufficient_evidence',forbidden=['1222.1作为2023值','倒推数值称原文'])
    pub('撤销年份修改，回到2024年第三产业，只给增加值。',['1222.1亿元'],['2024恢复','第三产业增加值'])
    pub('把对象换成工业，同时给增加值和按不变价格计算的同比方向及幅度。',['工业增加值30.7亿元','按不变价格同比下降7.2%'],['2024','工业替换第三产业'],pages=[2,3],forbidden=['同比增长7.2%'])
    pub('保留刚才的对象与年份，只给同比方向和幅度。',['工业按不变价格同比下降7.2%'],['2024','工业','只问同比'],pages=[2,3],forbidden=['把第三产业7.3%携带进来'])
    # C5: one controlled experiment, original rule/CSV/chart representations.
    c='ctx05';s=['formula_native','formula_constraints','context_parameters','context_energy_chart']
    mm=lambda q,facts,conditions,**kw:add(c,q,s,mode='multi_source',facts=facts,conditions=conditions,**kw)
    mm('式1能量变化率的公式、各参数单位，以及实验记录明确指出的不能照算情形是什么？',['Q=(a+b)/t','a与b单位kWh，t单位h，Q单位kW','缺少t、t=0或a与b量纲不同，不能照算','未给单位时不能确认结果为kW'],['式1','读取规则不直接猜计算值','不把数学常识冒充原件明确条件'])
    mm('使用批次F-03的实际参数代入式1，给出计算过程和结果。',['a=12kWh','b=9kWh','t=3h','Q=7kW'],['F-03','实际原值','式1'])
    mm('换成批次F-04，其他保持；不要补造缺失参数。',['F-04 a=10kWh、b=8kWh','t未记录，实际Q不能确认'],['F-04','实际值','缺参不能计算'],status='insufficient_evidence',forbidden=['搬用F-03的3h','把缺时长当0'])
    mm('仍针对F-04，仅假设t=6h，给出假设下的结果；明确这不是实际记录。',['a=10kWh','b=8kWh','用户假设t=6h','假设Q=3kW'],['F-04','显式用户假设6h','保留假设标签'],forbidden=['3kW称F-04实测值'])
    mm('撤销刚才的t=6h假设，请给出F-04的实际Q；缺少依据时明确指出缺项。',['t仍未记录，F-04实际Q不能确认'],['F-04','移除假设','恢复实际资料边界'],status='insufficient_evidence',forbidden=['沿用6h或3kW'])
    mm('改看F-03参数图，图中a和b各标多少？这些是能量还是功率？',['a=12kWh','b=9kWh','均为能量而不是功率'],['F-03真实图像','图中印刷原值','单位kWh'],forbidden=['将kWh变为kW'])
    mm('请给出F-04的实际耗时，仅以这张图为依据；不要从F-03迁移或补造。',['图仅标F-03的a/b，不提供F-04实际耗时'],['不跨批次迁移参数'],status='insufficient_evidence',forbidden=['F-04 t=3h或6h'])
    mm('回到F-03，按实际资料分别列出原始参数、计算得到的Q及各自出处。',['原始a12kWh/b9kWh/t3h','按式1派生Q=7kW','区分原始值与计算值'],['F-03','无F-04假设','公式与参数证据齐全'])
    # C6: actual API context_patch, not prose masquerading as a UI operation.
    c='ctx06';s=['boundary']
    add(c,'在monthly表只看2024年12月东区，合计amount_cents，单位分；NULL未知不补零。',s,
        sql="SELECT SUM(amount_cents) FROM monthly WHERE month='2024-12' AND region='东区'",conditions=['2024年12月','东区','amount_cents合计'])
    add(c,'按口径条调整后的时间范围重新给合计，其他保持。',s,
        patch=[{'op':'replace','field':'time_range','value':'2025年'}],
        sql="SELECT SUM(amount_cents) FROM monthly WHERE month>='2025-01' AND month<'2026-01' AND region='东区'",
        conditions=['明确patch替换到2025全年','东区','分'])
    add(c,'加入西区，分别列出两区及合计，地区升序。',s,
        patch=[{'op':'add','field':'entities','value':['西区']}],
        sql="SELECT region,SUM(amount_cents) FROM monthly WHERE month>='2025-01' AND month<'2026-01' AND region IN('东区','西区') GROUP BY region ORDER BY region",
        conditions=['东区并西区','2025','分别合计'])
    add(c,'去掉地区对象限制，只给全部地区总额。',s,patch=[{'op':'remove','field':'entities'}],
        sql="SELECT SUM(amount_cents) FROM monthly WHERE month>='2025-01' AND month<'2026-01'",
        conditions=['移除地区','2025','总额而非分组'])
    add(c,'重新只看东区，仍给该年的合计。',s,
        sql="SELECT SUM(amount_cents) FROM monthly WHERE month>='2025-01' AND month<'2026-01' AND region='东区'",
        conditions=['东区重新加入','2025','总额'])
    add(c,'增加这个月份限制，其他保持。',s,patch=[{'op':'add','field':'filters','value':['month仅限2025年2月至4月，包含两端']}],
        sql="SELECT SUM(amount_cents) FROM monthly WHERE month>='2025-02' AND month<'2025-05' AND region='东区'",
        conditions=['2025','东区','额外2至4月过滤'])
    add(c,'移除刚才增加的月份过滤，保留时间范围与对象。',s,patch=[{'op':'remove','field':'filters'}],
        sql="SELECT SUM(amount_cents) FROM monthly WHERE month>='2025-01' AND month<'2026-01' AND region='东区'",
        conditions=['2025全年','东区','不再只看2至4月'])
    add(c,'与第5轮相比，当前口径和合计是否恢复一致？说明依据。',s,
        facts=['第5/7轮均2025全年东区amount_cents合计20000分','第6轮月份过滤已移除'],
        conditions=['只对真实祖先第5与7轮','不存在其他分支年份'])
    # C7: document-defined business scope, distinct from user confirmation.
    c='ctx07';s=['boundary','definitions']
    biz=lambda q,facts,conditions,**kw:add(c,q,s,mode='multi_source',facts=facts,conditions=conditions,**kw)
    biz('按口径说明V1，2025年登记金额合计多少？先说明该名称对应哪个原始字段。',['登记金额对应orders.total_cents，单位分','仅已完成订单','2025合计56000分','NULL未知不补零'],['V1定义','2025','登记金额'])
    biz('指标改为到账金额，其他口径不变。',['到账金额对应payments.paid_cents','2025已完成订单实收合计39000分'],['V1到账定义','2025','通过order_id关联','不重复金额'])
    biz('将年份改为2024年。',['2024已完成订单到账10000分'],['2024替换2025','到账金额'])
    biz('刚才到账金额定义来自哪里？它是不是我在系统内确认过的个人业务定义？',['来自本轮导入口径说明V1','不等同用户在语义词典显式确认','不是DDL本身提供的业务含义'],['区分文档规则/DDL/用户确认'],forbidden=['伪造user_confirmed'])
    biz('回到2025年登记金额，只给原始单位下的合计。',['56000分'],['2025','登记金额','原始分单位'])
    biz('将上述合计从分换算成元，写出换算关系。',['100分=1元','56000分/100=560元','派生换算不冒充原格值'],['V1明确换算','2025登记金额'])
    biz('撤销单位换算，只报告原始单位及数值。',['56000分'],['恢复分单位','2025登记金额'])
    biz('比较第5轮和第7轮的定义、范围和结果，是否相同？',['均V1登记金额、2025、已完成订单、NULL未知','均56000分'],['真实祖先比较','不携带第3轮2024或到账指标'])
    # C8: 200-row ancestors exceed history budget through real result content.
    c='ctx08';s=['boundary'];cols='id,label,region,amount_cents,description'
    add(c,'从long_records按id升序列出id 1至200，返回id、label、region、amount_cents和完整description五列。',s,
        sql=f'SELECT {cols} FROM long_records WHERE id BETWEEN 1 AND 200 ORDER BY id',
        conditions=['ID范围1至200','五个完整原字段','id升序','不是前200个省略行'])
    add(c,'同一批id 1至200，五列不变，改为id降序。',s,
        sql=f'SELECT {cols} FROM long_records WHERE id BETWEEN 1 AND 200 ORDER BY id DESC',
        conditions=['ID范围仍1至200','降序','完整原文本'])
    add(c,'在同一范围只保留东区，恢复id升序，仍返回五列。',s,
        sql=f"SELECT {cols} FROM long_records WHERE id BETWEEN 1 AND 200 AND region='东区' ORDER BY id",
        conditions=['范围1至200','东区','升序'])
    add(c,'范围、东区和排序保持，只显示id、region、amount_cents三列。',s,
        sql="SELECT id,region,amount_cents FROM long_records WHERE id BETWEEN 1 AND 200 AND region='东区' ORDER BY id",
        conditions=['不因换列修改范围','东区','三列'])
    add(c,'取第1轮结果的第12行对象，只输出它的id、label、amount_cents；不是当前结果第12行。',s,
        sql='SELECT id,label,amount_cents FROM long_records WHERE id=12',conditions=['祖先第1轮升序第12行=12','解除当前东区以定位历史对象'],
        extra={'history_review':{'ancestor_turn':1,'row_start':11,'row_limit':1,'required_object_id':12,'recovery_or_equivalent_grounded_reconstruction':True}})
    add(c,'再取第2轮降序结果的第12行，与刚才的对象对照；按id升序输出两个对象的id和amount_cents。',s,
        sql='SELECT id,amount_cents FROM long_records WHERE id IN(12,189) ORDER BY id',conditions=['第2轮范围1至200降序第12=189','前轮对象12','不是全集240降序'])
    add(c,'回到第3轮的范围和东区筛选，只返回记录数。',s,
        sql="SELECT COUNT(*) FROM long_records WHERE id BETWEEN 1 AND 200 AND region='东区'",
        conditions=['祖先第3轮范围','东区','COUNT全部67条'])
    add(c,'恢复第1轮的范围、五列和升序，重新列出完整结果。',s,
        sql=f'SELECT {cols} FROM long_records WHERE id BETWEEN 1 AND 200 ORDER BY id',
        conditions=['1至200','移除东区','完整五列','id升序'],extra={'history_review':{'ancestor_turn':1,'restored_original_scope':True}})
    assert len(cases)==64
    assert all(sum(v['chain_id']==c for v in cases)==8 for c in {v['chain_id'] for v in cases})
    return cases
