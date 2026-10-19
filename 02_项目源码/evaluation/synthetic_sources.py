"""Deterministic, explicitly synthetic originals for controlled boundary tests."""
from pathlib import Path
import sqlite3


def build_boundary_database(path):
    path=Path(path)
    if path.exists():raise ValueError('合成原件已存在，拒绝覆盖冻结资料')
    path.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(path) as c:
        c.execute('PRAGMA foreign_keys=ON')
        for i in range(30):
            c.execute(f'CREATE TABLE aux_{i:02d}_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER)')
            c.executemany(f'INSERT INTO aux_{i:02d}_archive VALUES(?,?,?,?)',[(n,n,f'明确合成的无关联干扰数据{i}-{n}',900000+i*100+n) for n in range(1,4)])
        c.executescript('''
        CREATE TABLE customers(id INTEGER PRIMARY KEY,name TEXT NOT NULL,region TEXT NOT NULL);
        CREATE TABLE orders(id INTEGER PRIMARY KEY,customer_id INTEGER NOT NULL REFERENCES customers(id),day TEXT NOT NULL,status TEXT NOT NULL,total_cents INTEGER,tag TEXT);
        CREATE TABLE lines(id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL REFERENCES orders(id),sku TEXT NOT NULL,qty INTEGER NOT NULL,price_cents INTEGER NOT NULL,discount_bp INTEGER NOT NULL);
        CREATE TABLE payments(id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL REFERENCES orders(id),paid_cents INTEGER NOT NULL);
        CREATE TABLE staff(id INTEGER PRIMARY KEY,name TEXT NOT NULL,region TEXT NOT NULL);
        CREATE TABLE worklogs(id INTEGER PRIMARY KEY,staff_id INTEGER NOT NULL REFERENCES staff(id),order_id INTEGER NOT NULL REFERENCES orders(id),hours TEXT);
        CREATE TABLE monthly(month TEXT NOT NULL,region TEXT NOT NULL,amount_cents INTEGER,PRIMARY KEY(month,region));
        CREATE TABLE budgets(region TEXT PRIMARY KEY,target_cents INTEGER);
        CREATE TABLE measurements(id INTEGER PRIMARY KEY,day TEXT NOT NULL,series TEXT NOT NULL,value TEXT,unit TEXT NOT NULL);
        CREATE TABLE long_records(id INTEGER PRIMARY KEY,label TEXT NOT NULL,recorded_at TEXT NOT NULL,region TEXT NOT NULL,amount_cents INTEGER NOT NULL,description TEXT NOT NULL);
        ''')
        c.executemany('INSERT INTO customers VALUES(?,?,?)',[(1,'合成客户甲','东区'),(2,'合成客户乙','西区'),(3,'合成客户丙','东区'),(4,'合成客户丁','北区'),(5,'无订单合成客户','北区')])
        c.executemany('INSERT INTO orders VALUES(?,?,?,?,?,?)',[
            (1,1,'2024-12-31','已完成',10000,'普通'),(2,1,'2025-01-01','已完成',10000,'100%已确认'),
            (3,2,'2025-01-15','已完成',0,'ref_A'),(4,2,'2025-02-01','已完成',None,'金额未记录'),
            (5,3,'2025-02-28','已完成',25000,'普通'),(6,3,'2025-03-01','已取消',30000,'取消'),
            (7,4,'2025-03-31','已完成',12000,'literal_%'),(8,4,'2025-04-01','处理中',7000,'待处理'),
            (9,1,'2025-04-30','已完成',10000,'重复金额不同订单'),(10,3,'2025-05-01','已完成',-1000,'退款负值')])
        c.executemany('INSERT INTO lines VALUES(?,?,?,?,?,?)',[
            (1,1,'P-A',1,8000,0),(2,1,'P-B',1,2000,0),(3,2,'P-A',2,5000,0),(4,3,'FREE',1,0,0),
            (5,4,'UNKNOWN',1,1000,0),(6,5,'P-C',3,10000,1000),(7,5,'P-B',1,2000,0),
            (8,6,'P-C',3,10000,0),(9,7,'P-B',6,2000,0),(10,8,'P-A',1,7000,0),
            (11,9,'P-A',1,5000,0),(12,9,'P-B',1,5000,0),(13,10,'RETURN',-1,1000,0)])
        c.executemany('INSERT INTO payments VALUES(?,?,?)',[(1,1,6000),(2,1,4000),(3,2,5000),(4,2,5000),(5,3,0),(6,5,20000),(7,9,10000),(8,10,-1000)])
        c.executemany('INSERT INTO staff VALUES(?,?,?)',[(1,'张伟','东店'),(2,'张伟','西店'),(3,'李明','北店'),(4,'无工时合成员工','东店')])
        c.executemany('INSERT INTO worklogs VALUES(?,?,?,?)',[(1,1,1,'1.5'),(2,1,1,'0.5'),(3,2,2,'2.0'),(4,2,3,None),(5,3,5,'0'),(6,3,7,'3.25'),(7,1,9,'1.0')])
        c.executemany('INSERT INTO monthly VALUES(?,?,?)',[
            ('2024-12','东区',8000),('2025-01','东区',10000),('2025-02','东区',10000),('2025-04','东区',0),
            ('2024-12','西区',0),('2025-01','西区',2000),('2025-02','西区',None),('2025-03','西区',3000),
            ('2025-01','北区',0),('2025-02','北区',-1000),('2025-03','北区',0)])
        c.executemany('INSERT INTO budgets VALUES(?,?)',[('东区',40000),('西区',0),('北区',None)])
        c.executemany('INSERT INTO measurements VALUES(?,?,?,?,?)',[
            (1,'2025-01-01','energy','1.25','kWh'),(2,'2025-01-02','energy','750','Wh'),
            (3,'2025-01-03','energy','2.50','kWh'),(4,'2025-01-04','energy',None,'kWh'),
            (5,'2025-01-01','money_exact','9007199254740993.01','元'),(6,'2025-01-02','money_exact','0.09','元')])
        for n in range(1,241):
            region=('东区','西区','北区')[(n-1)%3]
            description=(f'这是明确合成的第{n:03d}条登记原始文本，所属对象为独立编号{n:03d}；资料仅用于恢复历史行和范围的工程测试，不是真实企业或个人数据。'
                         '该记录保留编号、地区、日期和金额分值，任何转换展示件都不构成第二个独立来源。')
            c.execute('INSERT INTO long_records VALUES(?,?,?,?,?,?)',(n,f'合成对象{n:03d}',f'2025-{1+(n-1)//28:02d}-{1+(n-1)%28:02d}',region,n*37,description))
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
    return path
