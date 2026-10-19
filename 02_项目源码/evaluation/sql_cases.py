"""Human-readable, distinct SQL tasks and independent reference queries.

References are evaluation keys, never sent to the product/model as input.
"""
from __future__ import annotations
import json
from pathlib import Path


def sql_cases(fixtures: Path):
    cases=[]
    def add(source,question,sql,tags,level='basic',**extra):
        cases.append(dict(source=source,question=question,reference_sql=sql,tags=tags.split(),level=level,**extra))
    # 20 distinct basic tasks against the unchanged public Chinook teaching DB.
    add('chinook','曲目表一共有多少条记录？只返回数量。','SELECT COUNT(*) FROM Track','count')
    add('chinook','客户来自多少个不同国家？只返回国家数，不把客户数当国家数。','SELECT COUNT(DISTINCT Country) FROM Customer','distinct')
    add('chinook','发票最早和最晚的开票日期是什么？按最早、最晚两列输出。','SELECT MIN(InvoiceDate),MAX(InvoiceDate) FROM Invoice','date min_max')
    add('chinook','所有账单Total合计是多少？保留两位小数，只返回金额。','SELECT ROUND(SUM(Total),2) FROM Invoice','sum')
    add('chinook','平均每张账单的Total是多少？保留两位小数。','SELECT ROUND(AVG(Total),2) FROM Invoice','average grain')
    add('chinook','按客户国家统计客户数量，输出国家和数量，按国家名升序。','SELECT Country,COUNT(*) FROM Customer GROUP BY Country ORDER BY Country','group_count ordering')
    add('chinook','列出Country为USA的客户编号、名字、姓氏，按编号升序。',"SELECT CustomerId,FirstName,LastName FROM Customer WHERE Country='USA' ORDER BY CustomerId",'equality projection')
    add('chinook','列出名称以B开头的国家及客户数，按国家名升序。',"SELECT Country,COUNT(*) FROM Customer WHERE Country LIKE 'B%' GROUP BY Country ORDER BY Country",'prefix_filter')
    add('chinook','有多少首曲目的Composer为NULL？只返回数量，不把空字符串当NULL。','SELECT COUNT(*) FROM Track WHERE Composer IS NULL','null')
    add('chinook','列出所有流派编号和名称，按编号升序。','SELECT GenreId,Name FROM Genre ORDER BY GenreId','lookup')
    add('chinook','2024年开出了多少张发票？只返回张数。',"SELECT COUNT(*) FROM Invoice WHERE InvoiceDate>='2024-01-01' AND InvoiceDate<'2025-01-01'",'date_range half_open')
    add('chinook','账单金额在5到10之间、包含两端的发票有多少张？','SELECT COUNT(*) FROM Invoice WHERE Total BETWEEN 5 AND 10','inclusive_range')
    add('chinook','曲目时长最小和最大各是多少毫秒？按最小、最大输出。','SELECT MIN(Milliseconds),MAX(Milliseconds) FROM Track','units extrema')
    add('chinook','时长严格超过10分钟的曲目有多少首？请用Milliseconds正确换算。','SELECT COUNT(*) FROM Track WHERE Milliseconds>600000','unit_conversion strict_range')
    add('chinook','分别统计各MediaTypeId的曲目数，输出类型编号和数量，按编号升序。','SELECT MediaTypeId,COUNT(*) FROM Track GROUP BY MediaTypeId ORDER BY MediaTypeId','group_by foreign_key_value')
    add('chinook','专辑数量最多的前5个艺术家编号及专辑数是多少？数量降序，相同按编号升序。','SELECT ArtistId,COUNT(*) n FROM Album GROUP BY ArtistId ORDER BY n DESC,ArtistId LIMIT 5','top_k ties')
    add('chinook','客户表Company已填写、即不是NULL的客户有多少人？','SELECT COUNT(*) FROM Customer WHERE Company IS NOT NULL','not_null')
    add('chinook','查询每种不同UnitPrice及对应曲目数，价格按数值升序。','SELECT UnitPrice,COUNT(*) FROM Track GROUP BY UnitPrice ORDER BY UnitPrice','distinct_price numeric_sort')
    add('chinook','职位为Sales Support Agent的员工编号、名、姓是什么？按编号升序。',"SELECT EmployeeId,FirstName,LastName FROM Employee WHERE Title='Sales Support Agent' ORDER BY EmployeeId",'text_filter')
    add('chinook','BillingState为NULL和非NULL的发票各多少张？按NULL数量、非NULL数量输出。','SELECT SUM(CASE WHEN BillingState IS NULL THEN 1 ELSE 0 END),COUNT(BillingState) FROM Invoice','conditional_count null')
    # 10 basic tasks using the independent Northwind schema, not renamed Chinook queries.
    add('northwind','仍在售且库存严格小于补货线的产品有哪些？输出Id、ProductName、UnitsInStock、ReorderLevel，按Id升序。','SELECT Id,ProductName,UnitsInStock,ReorderLevel FROM Product WHERE Discontinued=0 AND UnitsInStock<ReorderLevel ORDER BY Id','multi_condition inventory')
    add('northwind','停产产品有多少种？Discontinued=1表示停产。','SELECT COUNT(*) FROM Product WHERE Discontinued=1','boolean_category')
    add('northwind','运费Freight最高的前5张订单是什么？输出订单Id和运费，金额降序、同额按Id升序。','SELECT Id,Freight FROM "Order" ORDER BY Freight DESC,Id LIMIT 5','top_k freight')
    add('northwind','已发货订单的平均运费是多少？ShippedDate非NULL才计入，保留两位小数。','SELECT ROUND(AVG(Freight),2) FROM "Order" WHERE ShippedDate IS NOT NULL','filtered_average')
    add('northwind','每个供应商Country有多少家供应商？按国家名升序输出国家、数量。','SELECT Country,COUNT(*) FROM Supplier GROUP BY Country ORDER BY Country','group_count suppliers')
    add('northwind','列出类别编号和类别名称，按编号升序。','SELECT Id,CategoryName FROM Category ORDER BY Id','category_lookup')
    add('northwind','产品平均库存和平均在途数量各是多少？按UnitsInStock均值、UnitsOnOrder均值输出，两位小数。','SELECT ROUND(AVG(UnitsInStock),2),ROUND(AVG(UnitsOnOrder),2) FROM Product','two_averages')
    add('northwind','有多少个不同ShipCountry曾接收订单？只返回数量。','SELECT COUNT(DISTINCT ShipCountry) FROM "Order"','distinct_shipping_country')
    add('northwind','OrderDetail中总共有多少条明细、Quantity总和是多少？按明细数、数量和输出。','SELECT COUNT(*),SUM(Quantity) FROM OrderDetail','rows_vs_quantity')
    add('northwind','哪些产品单位价格高于50且未停产？输出Id、名称、单价，按单价降序、Id升序。','SELECT Id,ProductName,UnitPrice FROM Product WHERE UnitPrice>50 AND Discontinued=0 ORDER BY UnitPrice DESC,Id','compound_filter ordering')
    # 10 explicit controlled boundary tasks; names, rows and values are synthetic.
    add('boundary','按订单状态列出订单数，状态按字典序升序。','SELECT status,COUNT(*) FROM orders GROUP BY status ORDER BY status','synthetic status_count')
    add('boundary','订单头金额total_cents为NULL和等于0的订单各多少？按NULL数、零值数输出。','SELECT SUM(total_cents IS NULL),SUM(total_cents=0) FROM orders','synthetic null_zero')
    add('boundary','列出同名“张伟”的员工编号、门店，按编号升序；不要合并两个人。',"SELECT id,region FROM staff WHERE name='张伟' ORDER BY id",'synthetic duplicate_names')
    add('boundary','原始tag字面包含百分号%的订单编号是什么？不是通配匹配，按编号升序。',"SELECT id FROM orders WHERE instr(tag,'%')>0 ORDER BY id",'synthetic literal_percent')
    add('boundary','原始tag字面包含下划线_的订单编号是什么？按编号升序。',"SELECT id FROM orders WHERE instr(tag,'_')>0 ORDER BY id",'synthetic literal_underscore')
    add('boundary','2024年12月31日及之前的订单数是多少？day为ISO日期文本。',"SELECT COUNT(*) FROM orders WHERE day<'2025-01-01'",'synthetic year_boundary')
    add('boundary','各客户名称及地区是什么？输出id、name、region，按id升序。','SELECT id,name,region FROM customers ORDER BY id','synthetic projection')
    add('boundary','月度汇总中金额为零的年月和地区是什么？按年月、地区升序。','SELECT month,region FROM monthly WHERE amount_cents=0 ORDER BY month,region','synthetic explicit_zero')
    add('boundary','预算表中缺少目标值的地区是什么？只返回region，升序。','SELECT region FROM budgets WHERE target_cents IS NULL ORDER BY region','synthetic missing_target')
    add('boundary','long_records实际保存多少行？最小和最大id是什么？依次输出行数、最小id、最大id。','SELECT COUNT(*),MIN(id),MAX(id) FROM long_records','synthetic bounds')
    assert len(cases)==40
    # Preserve 24 established reference queries as explicitly development
    # regression; recompute every answer from copied originals in the builder.
    legacy=json.loads((fixtures/'cases.frozen.json').read_text())
    for old in legacy['cases']:
        if old.get('reference_sql'):
            add(old['database'],old['question'],old['reference_sql'],' '.join(old['tags'])+' legacy_development_reference',
                'complex',legacy_reference_id=old['id'])
    assert len(cases)==64
    extra=[
      ('chinook','每个客户最早的一张发票是哪张？同日取InvoiceId最小的一张，输出客户编号、发票编号、日期，按客户编号升序。',
       'WITH r AS(SELECT CustomerId,InvoiceId,InvoiceDate,ROW_NUMBER() OVER(PARTITION BY CustomerId ORDER BY InvoiceDate,InvoiceId) n FROM Invoice) SELECT CustomerId,InvoiceId,InvoiceDate FROM r WHERE n=1 ORDER BY CustomerId','window first_per_group'),
      ('chinook','哪些客户既买过Rock又买过Jazz曲目？只输出客户编号升序。',
       "SELECT i.CustomerId FROM Invoice i JOIN InvoiceLine l ON l.InvoiceId=i.InvoiceId JOIN Track t ON t.TrackId=l.TrackId JOIN Genre g ON g.GenreId=t.GenreId WHERE g.Name='Rock' INTERSECT SELECT i.CustomerId FROM Invoice i JOIN InvoiceLine l ON l.InvoiceId=i.InvoiceId JOIN Track t ON t.TrackId=l.TrackId JOIN Genre g ON g.GenreId=t.GenreId WHERE g.Name='Jazz' ORDER BY 1",'set_intersection distinct_customer'),
      ('chinook','哪些客户买过Rock却从未买过Metal曲目？只输出客户编号升序。',
       "SELECT i.CustomerId FROM Invoice i JOIN InvoiceLine l ON l.InvoiceId=i.InvoiceId JOIN Track t ON t.TrackId=l.TrackId JOIN Genre g ON g.GenreId=t.GenreId WHERE g.Name='Rock' EXCEPT SELECT i.CustomerId FROM Invoice i JOIN InvoiceLine l ON l.InvoiceId=i.InvoiceId JOIN Track t ON t.TrackId=l.TrackId JOIN Genre g ON g.GenreId=t.GenreId WHERE g.Name='Metal' ORDER BY 1",'set_difference negative_membership'),
      ('chinook','列出金额高于相同账单国家平均金额的发票，输出发票编号、国家、金额；按编号升序，取前20条。',
       'SELECT i.InvoiceId,i.BillingCountry,i.Total FROM Invoice i WHERE i.Total>(SELECT AVG(j.Total) FROM Invoice j WHERE j.BillingCountry=i.BillingCountry) ORDER BY i.InvoiceId LIMIT 20','correlated_subquery'),
      ('chinook','每个流派时长最长的曲目是哪一首？同长取TrackId最小，输出流派编号、曲目编号、毫秒，按流派编号升序。',
       'WITH r AS(SELECT GenreId,TrackId,Milliseconds,ROW_NUMBER() OVER(PARTITION BY GenreId ORDER BY Milliseconds DESC,TrackId) n FROM Track) SELECT GenreId,TrackId,Milliseconds FROM r WHERE n=1 ORDER BY GenreId','window deterministic_tie'),
      ('chinook','没有任何已售出曲目的专辑共有多少张？专辑中有未售曲目不代表整张未售。',
       'SELECT COUNT(*) FROM Album a WHERE NOT EXISTS(SELECT 1 FROM Track t JOIN InvoiceLine l ON l.TrackId=t.TrackId WHERE t.AlbumId=a.AlbumId)','anti_join album_grain'),
      ('chinook','有购买记录的各流派被多少个不同客户购买过？输出流派名称、客户数，客户数降序、名称升序。',
       'SELECT g.Name,COUNT(DISTINCT i.CustomerId) n FROM Genre g JOIN Track t ON t.GenreId=g.GenreId JOIN InvoiceLine l ON l.TrackId=t.TrackId JOIN Invoice i ON i.InvoiceId=l.InvoiceId GROUP BY g.GenreId,g.Name ORDER BY n DESC,g.Name','distinct_multi_join'),
      ('chinook','按年份列账单总额和相对上一年增速百分数，两位小数；第一年或基数0返回NULL，按年份升序。',
       "WITH y AS(SELECT strftime('%Y',InvoiceDate) yr,SUM(Total) total FROM Invoice GROUP BY 1),r AS(SELECT *,LAG(total) OVER(ORDER BY yr) prev FROM y) SELECT yr,ROUND(total,2),ROUND(100.0*(total-prev)/NULLIF(prev,0),2) FROM r ORDER BY yr",'window lag denominator null'),
      ('chinook','每年账单金额最高的两张发票，输出年份、发票编号、金额；同额按发票编号选前两张，按年份和名次排序。',
       "WITH r AS(SELECT strftime('%Y',InvoiceDate) yr,InvoiceId,Total,ROW_NUMBER() OVER(PARTITION BY strftime('%Y',InvoiceDate) ORDER BY Total DESC,InvoiceId) n FROM Invoice) SELECT yr,InvoiceId,Total FROM r WHERE n<=2 ORDER BY yr,n",'partition_top_k'),
      ('chinook','各播放列表有多少不同专辑？包括没有曲目的列表，输出列表编号、名称、专辑数，按编号升序。',
       'SELECT p.PlaylistId,p.Name,COUNT(DISTINCT t.AlbumId) FROM Playlist p LEFT JOIN PlaylistTrack pt ON pt.PlaylistId=p.PlaylistId LEFT JOIN Track t ON t.TrackId=pt.TrackId GROUP BY p.PlaylistId,p.Name ORDER BY p.PlaylistId','left_join many_to_many distinct'),
      ('chinook','只看有曲目的艺术家，各自曲目平均时长高于全库平均的有哪些？输出艺术家编号、名称、平均毫秒（两位小数），按编号升序。',
       'SELECT ar.ArtistId,ar.Name,ROUND(AVG(t.Milliseconds),2) FROM Artist ar JOIN Album a ON a.ArtistId=ar.ArtistId JOIN Track t ON t.AlbumId=a.AlbumId GROUP BY ar.ArtistId,ar.Name HAVING AVG(t.Milliseconds)>(SELECT AVG(Milliseconds) FROM Track) ORDER BY ar.ArtistId','having_scalar_subquery'),
      ('chinook','每张发票明细实际单价乘数量合计与Total差超过0.01的发票有多少张？只返回数量，不对Total在明细联表后求和。',
       'SELECT COUNT(*) FROM Invoice i JOIN(SELECT InvoiceId,SUM(UnitPrice*Quantity) total FROM InvoiceLine GROUP BY InvoiceId) s ON s.InvoiceId=i.InvoiceId WHERE ABS(i.Total-s.total)>0.01','reconciliation grain'),
      ('northwind','所有客户各有多少订单？包括零订单客户，输出客户Id、CompanyName、订单数，按客户Id升序。',
       'SELECT c.Id,c.CompanyName,COUNT(o.Id) FROM Customer c LEFT JOIN "Order" o ON o.CustomerId=c.Id GROUP BY c.Id,c.CompanyName ORDER BY c.Id','left_join zero_count'),
      ('northwind','各供应商当前库存按产品UnitPrice乘UnitsInStock估值是多少？只显示有产品的供应商，输出Id、公司名、两位小数金额，按Id升序。',
       'SELECT s.Id,s.CompanyName,ROUND(SUM(p.UnitPrice*p.UnitsInStock),2) FROM Supplier s JOIN Product p ON p.SupplierId=s.Id GROUP BY s.Id,s.CompanyName ORDER BY s.Id','weighted_aggregate inventory'),
      ('northwind','各员工已发货订单中晚于RequiredDate发货的比例百分数是多少？未发货不进分母，输出员工Id和两位小数比例，按Id升序。',
       'SELECT EmployeeId,ROUND(100.0*SUM(julianday(ShippedDate)>julianday(RequiredDate))/COUNT(*),2) FROM "Order" WHERE ShippedDate IS NOT NULL GROUP BY EmployeeId ORDER BY EmployeeId','date_compare denominator'),
      ('northwind','购买过至少5个不同产品类别的客户有哪些？输出客户Id和类别数，按客户Id升序。',
       'SELECT o.CustomerId,COUNT(DISTINCT p.CategoryId) n FROM "Order" o JOIN OrderDetail d ON d.OrderId=o.Id JOIN Product p ON p.Id=d.ProductId GROUP BY o.CustomerId HAVING COUNT(DISTINCT p.CategoryId)>=5 ORDER BY o.CustomerId','having_distinct categories'),
      ('northwind','列前10张订单的折后商品金额与运费占该金额百分比，输出订单Id、商品金额、百分比；按Id升序，金额及比例两位小数，商品金额为0时比例NULL。',
       'SELECT o.Id,ROUND(s.total,2),ROUND(100.0*o.Freight/NULLIF(s.total,0),2) FROM "Order" o JOIN(SELECT OrderId,SUM(UnitPrice*Quantity*(1-Discount)) total FROM OrderDetail GROUP BY OrderId) s ON s.OrderId=o.Id ORDER BY o.Id LIMIT 10','preaggregate discount denominator'),
      ('northwind','每个有订单客户最后一张订单是什么？同日取Id最大，输出客户Id、订单Id、日期，按客户Id升序。',
       'WITH r AS(SELECT CustomerId,Id,OrderDate,ROW_NUMBER() OVER(PARTITION BY CustomerId ORDER BY OrderDate DESC,Id DESC) n FROM "Order") SELECT CustomerId,Id,OrderDate FROM r WHERE n=1 ORDER BY CustomerId','last_per_group'),
      ('northwind','每位员工最早处理的三张订单，输出员工Id、订单Id、日期；同日按订单Id，按员工Id与日期、订单Id升序。',
       'WITH r AS(SELECT EmployeeId,Id,OrderDate,ROW_NUMBER() OVER(PARTITION BY EmployeeId ORDER BY OrderDate,Id) n FROM "Order") SELECT EmployeeId,Id,OrderDate FROM r WHERE n<=3 ORDER BY EmployeeId,OrderDate,Id','window first_k'),
      ('northwind','买齐CategoryId=1下全部产品的客户有哪些？输出客户Id升序，按每种产品至少买过一次判断，不要求同一张订单。',
       'SELECT c.Id FROM Customer c WHERE NOT EXISTS(SELECT 1 FROM Product p WHERE p.CategoryId=1 AND NOT EXISTS(SELECT 1 FROM "Order" o JOIN OrderDetail d ON d.OrderId=o.Id WHERE o.CustomerId=c.Id AND d.ProductId=p.Id)) ORDER BY c.Id','relational_division'),
      ('northwind','2013年每月运费合计及截至该月累计运费，输出年月、月合计、累计合计，两位小数，月份升序。',
       "WITH m AS(SELECT substr(OrderDate,1,7) ym,SUM(Freight) total FROM \"Order\" WHERE OrderDate>='2013-01-01' AND OrderDate<'2014-01-01' GROUP BY 1) SELECT ym,ROUND(total,2),ROUND(SUM(total) OVER(ORDER BY ym ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),2) FROM m ORDER BY ym",'running_total dates'),
      ('northwind','同一城市同时有客户和供应商时，列出城市、客户Id、供应商Id；只按City相等匹配，按城市及两个Id升序，取前20组。',
       'SELECT c.City,c.Id,s.Id FROM Customer c JOIN Supplier s ON s.City=c.City ORDER BY c.City,c.Id,s.Id LIMIT 20','non_fk_join explicit_semantics'),
      ('northwind','在售产品中，单价低于本类别全部产品平均单价的有哪些？输出Id、名称、单价，按Id升序；均值包含停产产品。',
       'SELECT p.Id,p.ProductName,p.UnitPrice FROM Product p WHERE p.Discontinued=0 AND p.UnitPrice<(SELECT AVG(q.UnitPrice) FROM Product q WHERE q.CategoryId=p.CategoryId) ORDER BY p.Id','correlated_scope'),
      ('northwind','各类别折后销售额占全部类别折后销售额百分数，输出类别名和比例，两位小数，按类别名升序。',
       'WITH s AS(SELECT c.Id,c.CategoryName,SUM(d.UnitPrice*d.Quantity*(1-d.Discount)) total FROM Category c JOIN Product p ON p.CategoryId=c.Id JOIN OrderDetail d ON d.ProductId=p.Id GROUP BY c.Id,c.CategoryName) SELECT CategoryName,ROUND(100.0*total/(SELECT SUM(total) FROM s),2) FROM s ORDER BY CategoryName','share global_denominator'),
      ('boundary','按客户统计已完成订单头金额，必须包括零订单客户；输出客户编号、订单数、非NULL头金额合计（无已知金额则NULL），按客户编号。',
       "SELECT c.id,COUNT(o.id),SUM(o.total_cents) FROM customers c LEFT JOIN orders o ON o.customer_id=c.id AND o.status='已完成' GROUP BY c.id ORDER BY c.id",'synthetic left_join null_scope'),
      ('boundary','有明细的已完成订单头金额总和是多少分？每张订单只算一次，金额相同的不同订单也都要计入。',
       "SELECT SUM(o.total_cents) FROM orders o WHERE o.status='已完成' AND EXISTS(SELECT 1 FROM lines l WHERE l.order_id=o.id)",'synthetic fanout distinct_value_trap'),
      ('boundary','按客户列已完成订单头金额、已付款金额、未付余额，金额单位分；各集合先按订单聚合，缺付款按0，未知头金额不强补，按客户编号。',
       "WITH p AS(SELECT order_id,SUM(paid_cents) paid FROM payments GROUP BY order_id) SELECT o.customer_id,SUM(o.total_cents),SUM(COALESCE(p.paid,0)),SUM(o.total_cents-COALESCE(p.paid,0)) FROM orders o LEFT JOIN p ON p.order_id=o.id WHERE o.status='已完成' GROUP BY o.customer_id ORDER BY o.customer_id",'synthetic two_fact_tables preaggregate'),
      ('boundary','按订单列明细折后金额（分），折扣以discount_bp万分点计，金额为qty×price_cents×(1-discount_bp/10000)，保留两位小数，按订单编号。',
       'SELECT order_id,ROUND(SUM(qty*price_cents*(1-discount_bp/10000.0)),2) FROM lines GROUP BY order_id ORDER BY order_id','synthetic basis_points weighted_discount'),
      ('boundary','按地区列2025年月度金额累计值，输出地区、年月、当月金额、累计金额（均分），按地区年月升序。',
       "SELECT region,month,amount_cents,SUM(amount_cents) OVER(PARTITION BY region ORDER BY month ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM monthly WHERE month>='2025-01' AND month<'2026-01' ORDER BY region,month",'synthetic partition_running_sum'),
      ('boundary','2025年各地区月度金额合计与预算达成率，输出地区、金额合计、百分比；目标0或NULL时比例NULL，金额单位分，比率两位小数。',
       "SELECT m.region,SUM(m.amount_cents),ROUND(100.0*SUM(m.amount_cents)/NULLIF(b.target_cents,0),2) FROM monthly m JOIN budgets b ON b.region=m.region WHERE m.month>='2025-01' AND m.month<'2026-01' GROUP BY m.region,b.target_cents ORDER BY m.region",'synthetic zero_denominator null_target'),
      ('boundary','有订单但完全没有付款记录的客户编号有哪些？只输出编号升序，不把部分未付款当完全未付款。',
       'SELECT c.id FROM customers c WHERE EXISTS(SELECT 1 FROM orders o WHERE o.customer_id=c.id) AND NOT EXISTS(SELECT 1 FROM orders o JOIN payments p ON p.order_id=o.id WHERE o.customer_id=c.id) ORDER BY c.id','synthetic nested_anti_join'),
      ('boundary','各员工参与的不同订单数、工时已知行数和总工时是什么？同一员工同单多条工时不重复算订单；输出员工编号及三项值，按编号。',
       'SELECT s.id,COUNT(DISTINCT w.order_id),COUNT(w.hours),ROUND(SUM(CAST(w.hours AS NUMERIC)),2) FROM staff s LEFT JOIN worklogs w ON w.staff_id=s.id GROUP BY s.id ORDER BY s.id','synthetic distinct_grain nullable_measure'),
      ('boundary','同一地区月度金额比上一条已记录月份增加多少分？第一条返回NULL，不补缺失月份；按地区年月输出地区、年月、差额。',
       'SELECT region,month,amount_cents-LAG(amount_cents) OVER(PARTITION BY region ORDER BY month) FROM monthly ORDER BY region,month','synthetic lag_missing_month'),
      ('boundary','每个地区最高月度金额对应哪些月份？并列全部保留，输出地区、年月、金额（分），按地区、年月升序。',
       'WITH r AS(SELECT *,DENSE_RANK() OVER(PARTITION BY region ORDER BY amount_cents DESC) n FROM monthly) SELECT region,month,amount_cents FROM r WHERE n=1 ORDER BY region,month','synthetic dense_rank_ties'),
      ('boundary','只有unit为kWh的energy测量值按日期列出，两位小数，并同时给出截至该日期（含当日）的同单位值累计和；不能混入Wh。',
       "SELECT day,ROUND(CAST(value AS NUMERIC),2),ROUND(SUM(CAST(value AS NUMERIC)) OVER(ORDER BY day),2) FROM measurements WHERE series='energy' AND unit='kWh' ORDER BY day",'synthetic incompatible_units'),
      ('boundary','对已完成订单，分别返回忽略NULL的金额平均值和把NULL补0时的均值：输出原始AVG、补0AVG，两位小数，单位分。',
       "SELECT ROUND(AVG(total_cents),2),ROUND(AVG(COALESCE(total_cents,0)),2) FROM orders WHERE status='已完成'",'synthetic null_averaging explicit_comparison'),
    ]
    for source,q,sql,tags in extra:add(source,q,sql,tags,'complex')
    assert len(extra)==36 and len(cases)==100
    for index,case in enumerate(cases,1):
        case['id']=f'sql-{index:03d}'
    return cases
