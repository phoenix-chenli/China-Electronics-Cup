-- 从所附 SQLite 原件只读导出的表结构；不包含业务数据。
-- 关系仅以数据库实际声明为准，不等于模型核实了业务关系。

CREATE TABLE inspections(station_id TEXT,period TEXT,checks INTEGER,rechecks INTEGER,unit TEXT);

CREATE TABLE stations(station_id TEXT PRIMARY KEY,name TEXT,bank TEXT);
