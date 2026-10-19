-- 从所附 SQLite 原件只读导出的表结构；不包含业务数据。
-- 关系仅以数据库实际声明为准，不等于模型核实了业务关系。

CREATE TABLE aux_00_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_01_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_02_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_03_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_04_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_05_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_06_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_07_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_08_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_09_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_10_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_11_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_12_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_13_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_14_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_15_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_16_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_17_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_18_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_19_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_20_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_21_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_22_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_23_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_24_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_25_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_26_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_27_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_28_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE aux_29_archive(id INTEGER PRIMARY KEY,join_id INTEGER,label TEXT,amount_cents INTEGER);

CREATE TABLE budgets(region TEXT PRIMARY KEY,target_cents INTEGER);

CREATE TABLE customers(id INTEGER PRIMARY KEY,name TEXT NOT NULL,region TEXT NOT NULL);

CREATE TABLE lines(id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL REFERENCES orders(id),sku TEXT NOT NULL,qty INTEGER NOT NULL,price_cents INTEGER NOT NULL,discount_bp INTEGER NOT NULL);

CREATE TABLE long_records(id INTEGER PRIMARY KEY,label TEXT NOT NULL,recorded_at TEXT NOT NULL,region TEXT NOT NULL,amount_cents INTEGER NOT NULL,description TEXT NOT NULL);

CREATE TABLE measurements(id INTEGER PRIMARY KEY,day TEXT NOT NULL,series TEXT NOT NULL,value TEXT,unit TEXT NOT NULL);

CREATE TABLE monthly(month TEXT NOT NULL,region TEXT NOT NULL,amount_cents INTEGER,PRIMARY KEY(month,region));

CREATE TABLE orders(id INTEGER PRIMARY KEY,customer_id INTEGER NOT NULL REFERENCES customers(id),day TEXT NOT NULL,status TEXT NOT NULL,total_cents INTEGER,tag TEXT);

CREATE TABLE payments(id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL REFERENCES orders(id),paid_cents INTEGER NOT NULL);

CREATE TABLE staff(id INTEGER PRIMARY KEY,name TEXT NOT NULL,region TEXT NOT NULL);

CREATE TABLE worklogs(id INTEGER PRIMARY KEY,staff_id INTEGER NOT NULL REFERENCES staff(id),order_id INTEGER NOT NULL REFERENCES orders(id),hours TEXT);
