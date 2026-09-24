-- 干净合成示例业务库；金额单位为元。
PRAGMA foreign_keys=ON;

CREATE TABLE business_rules(rule_id TEXT PRIMARY KEY, rule_name TEXT, effective_from TEXT, effective_to TEXT, threshold_amount REAL, bonus_rate REAL, source_document TEXT, formula TEXT);

CREATE TABLE customers(customer_id TEXT PRIMARY KEY, customer_name TEXT, region TEXT, industry TEXT);

CREATE TABLE orders(order_id TEXT PRIMARY KEY, order_date TEXT, customer_id TEXT, product_id TEXT, project_id TEXT, region TEXT, quantity INTEGER, sales_amount REAL, returned_amount REAL, currency TEXT);

CREATE TABLE products(product_id TEXT PRIMARY KEY, product_name TEXT, category TEXT, unit TEXT);

CREATE TABLE projects(project_id TEXT PRIMARY KEY, project_name TEXT, customer_id TEXT, region TEXT, owner TEXT, status TEXT, project_revenue REAL);

CREATE TABLE targets(target_id TEXT PRIMARY KEY, target_year INTEGER, region TEXT, product_id TEXT, target_amount REAL, source_note TEXT);
