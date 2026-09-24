import sqlite3
from .store import BUSINESS_DB, DOCS

def seed_business() -> None:
    if BUSINESS_DB.exists():
        try:
            con = sqlite3.connect(BUSINESS_DB)
            tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
            con.close()
            if {"orders", "products", "projects"}.issubset(tables):
                return
        except Exception:
            pass
    con = sqlite3.connect(BUSINESS_DB)
    con.executescript("""
    DROP TABLE IF EXISTS orders;
    DROP TABLE IF EXISTS products;
    DROP TABLE IF EXISTS customers;
    DROP TABLE IF EXISTS projects;
    DROP TABLE IF EXISTS targets;
    DROP TABLE IF EXISTS business_rules;
    CREATE TABLE products(product_id TEXT PRIMARY KEY, product_name TEXT, category TEXT, unit TEXT);
    CREATE TABLE customers(customer_id TEXT PRIMARY KEY, customer_name TEXT, region TEXT, industry TEXT);
    CREATE TABLE projects(project_id TEXT PRIMARY KEY, project_name TEXT, customer_id TEXT, region TEXT, owner TEXT, status TEXT, project_revenue REAL);
    CREATE TABLE orders(order_id TEXT PRIMARY KEY, order_date TEXT, customer_id TEXT, product_id TEXT, project_id TEXT, region TEXT, quantity INTEGER, sales_amount REAL, returned_amount REAL, currency TEXT);
    CREATE TABLE targets(target_id TEXT PRIMARY KEY, target_year INTEGER, region TEXT, product_id TEXT, target_amount REAL, source_note TEXT);
    CREATE TABLE business_rules(rule_id TEXT PRIMARY KEY, rule_name TEXT, effective_from TEXT, effective_to TEXT, threshold_amount REAL, bonus_rate REAL, source_document TEXT, formula TEXT);
    """)
    products = [
        ("P001", "云安全平台", "安全", "元"), ("P002", "智能存储", "基础设施", "元"), ("P003", "边缘计算节点", "计算", "元"), ("P004", "数据中台服务", "数据服务", "元"), ("P005", "统一运维套件", "运维", "元"),
    ]
    customers = [
        ("C001", "东海制造集团", "华东", "制造"), ("C002", "北辰政务云", "华北", "政务"), ("C003", "南岭能源", "华南", "能源"), ("C004", "蜀都交通", "西南", "交通"), ("C005", "沪上金融科技", "华东", "金融"), ("C006", "江南医疗", "华东", "医疗"),
    ]
    projects = [
        ("PR001", "东海零信任升级", "C001", "华东", "林默", "已交付", 780000.0), ("PR002", "北辰政务云二期", "C002", "华北", "周宁", "进行中", 620000.0), ("PR003", "南岭数据治理", "C003", "华南", "沈言", "已交付", 510000.0), ("PR004", "蜀都边缘节点", "C004", "西南", "顾晨", "已交付", 430000.0), ("PR005", "沪上风控平台", "C005", "华东", "林默", "已交付", 920000.0), ("PR006", "江南医疗数据中台", "C006", "华东", "赵璇", "进行中", 680000.0),
    ]
    orders = [
        ("O001", "2024-01-15", "C001", "P001", "PR001", "华东", 3, 360000, 0, "CNY"), ("O002", "2024-02-08", "C005", "P001", "PR005", "华东", 2, 280000, 20000, "CNY"),
        ("O003", "2024-03-18", "C006", "P004", "PR006", "华东", 1, 300000, 0, "CNY"), ("O004", "2024-04-09", "C001", "P002", "PR001", "华东", 4, 240000, 0, "CNY"),
        ("O005", "2024-05-21", "C005", "P004", "PR005", "华东", 2, 420000, 0, "CNY"), ("O006", "2024-06-03", "C006", "P001", "PR006", "华东", 1, 160000, 0, "CNY"),
        ("O007", "2024-07-11", "C002", "P003", "PR002", "华北", 3, 330000, 0, "CNY"), ("O008", "2024-08-14", "C002", "P004", "PR002", "华北", 1, 180000, 0, "CNY"),
        ("O009", "2024-09-02", "C003", "P002", "PR003", "华南", 2, 190000, 0, "CNY"), ("O010", "2024-10-16", "C003", "P004", "PR003", "华南", 2, 390000, 10000, "CNY"),
        ("O011", "2024-11-05", "C004", "P003", "PR004", "西南", 2, 220000, 0, "CNY"), ("O012", "2024-12-20", "C004", "P005", "PR004", "西南", 1, 90000, 0, "CNY"),
        ("O013", "2025-01-17", "C001", "P001", "PR001", "华东", 1, 145000, 0, "CNY"), ("O014", "2025-02-26", "C005", "P004", "PR005", "华东", 1, 260000, 0, "CNY"),
        ("O015", "2025-03-10", "C006", "P002", "PR006", "华东", 2, 180000, 0, "CNY"), ("O016", "2025-04-22", "C002", "P003", "PR002", "华北", 1, 120000, 0, "CNY"),
        ("O017", "2025-05-19", "C003", "P004", "PR003", "华南", 1, 210000, 0, "CNY"), ("O018", "2025-06-08", "C004", "P005", "PR004", "西南", 2, 150000, 0, "CNY"),
    ]
    targets = [("T001", 2024, "华东", "P001", 550000, "年度经营目标表·华东"), ("T002", 2024, "华东", "P004", 650000, "年度经营目标表·华东"), ("T003", 2024, "华北", "P003", 420000, "年度经营目标表·华北"), ("T004", 2024, "华南", "P004", 350000, "年度经营目标表·华南")]
    rules = [("R2024", "区域产品激励规则", "2024-01-01", "2024-12-31", 800000, 0.025, "激励规则_2024.md", "奖金 = max(0, 净销售额 - 800000) × 2.5%"), ("R2025", "区域产品激励规则", "2025-01-01", None, 1000000, 0.03, "激励规则_2025.md", "奖金 = max(0, 净销售额 - 1000000) × 3%")]
    con.executemany("INSERT INTO products VALUES (?,?,?,?)", products)
    con.executemany("INSERT INTO customers VALUES (?,?,?,?)", customers)
    con.executemany("INSERT INTO projects VALUES (?,?,?,?,?,?,?)", projects)
    con.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)", orders)
    con.executemany("INSERT INTO targets VALUES (?,?,?,?,?,?)", targets)
    con.executemany("INSERT INTO business_rules VALUES (?,?,?,?,?,?,?,?)", rules)
    con.commit(); con.close()


def write_seed_docs() -> None:
    docs = {
        "激励规则_2024.md": """# 2024区域产品激励规则\n\n## 生效范围\n本规则于2024年1月1日至2024年12月31日生效，适用于销售区域为华东、华北、华南、西南的产品订单。\n\n## 计算口径\n净销售额 = 含税销售额 - 已确认退货金额。奖金仅对超过800000元的部分计算，奖金 = max(0, 净销售额 - 800000) × 2.5%。\n\n## 复盘经验\n云安全平台在制造和金融客户中采用“先评估、后分阶段上线”的方法，项目复盘记录显示该方法帮助客户降低了迁移风险。此条是经验总结，不构成因果证明。\n""",
        "激励规则_2025.md": """# 2025区域产品激励规则\n\n## 生效范围\n本规则自2025年1月1日起生效，适用于所有标准产品订单。\n\n## 计算口径\n净销售额 = 含税销售额 - 已确认退货金额。奖金仅对超过1000000元的部分计算，奖金 = max(0, 净销售额 - 1000000) × 3%。\n\n## 版本提示\n如果查询历史交易，应按交易发生日期选择规则；按当前规则重算必须标记为情景模拟。\n""",
        "项目复盘_零信任.md": """# 东海零信任升级项目复盘\n\n## 成功经验\n项目组先进行资产盘点和风险分级，再按业务域分阶段切换；每一阶段都保留回滚窗口。\n\n## 证据边界\n本文只记录项目复盘观察，不证明单一措施与收入增长之间的因果关系。\n""",
        "指标口径字典.md": """# 销售指标口径字典\n\n净销售额：订单含税销售额扣除已确认退货金额，按订单明细聚合，单位为元。\n销售冠军：在给定时间和区域内，按净销售额降序排名第一的产品。\n增长率： (本期净销售额 - 对比期净销售额) / 对比期净销售额；必须指定两个时间范围。\n""",
        "图表_2024目标说明.md": """# 2024目标图表说明\n\n[IMAGE: monthly_target_chart.png]\n\n图中目标值来自年度经营目标表，单位为元。图像证据需要与结构化目标表交叉核验；视觉读取只作为定位线索，精确计算优先使用表格单元格。\n""",
    }
    for name, content in docs.items():
        p = DOCS / name
        if not p.exists(): p.write_text(content, encoding="utf-8")
    # Rebuild only the known broken bundled chart; preserve arbitrary user files.
    # index_document retains the old SHA-addressed original before indexing v2.
    import hashlib
    img = DOCS / "monthly_target_chart.png"
    legacy = "f32185ff38fbe3e54beca5fb77102b3856fc542f3e261057d97612ab7b8d937e"
    if not img.exists() or hashlib.sha256(img.read_bytes()).hexdigest() == legacy:
        from pathlib import Path
        from PIL import Image, ImageDraw, ImageFont
        from .store import VERSIONS
        if img.exists():
            original = VERSIONS / (legacy + ".png")
            if not original.exists(): original.write_bytes(img.read_bytes())
        import os
        candidates = ([Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / f for f in ("msyh.ttc", "simsun.ttc")] if os.name == "nt" else [Path("/System/Library/Fonts/Supplemental/Songti.ttc")])
        font_path = next((p for p in candidates if p.exists()), None)
        if font_path is None: return  # Never generate missing-glyph artwork.
        with sqlite3.connect(BUSINESS_DB) as con:
            rows = con.execute("SELECT t.region,p.product_name,t.target_amount FROM targets t JOIN products p ON p.product_id=t.product_id WHERE t.target_year=2024 ORDER BY t.target_id").fetchall()
        if not rows: return
        font = ImageFont.truetype(str(font_path), 25)
        title_font = ImageFont.truetype(str(font_path), 35)
        im = Image.new("RGB", (1200, 210+110*len(rows)), "#f5f8fd"); d = ImageDraw.Draw(im)
        d.text((50, 30), "2024 年区域产品目标", font=title_font, fill="#172c50")
        d.text((50, 82), "合成示例数据 · 来源：年度目标表 · 单位：元", font=font, fill="#536078")
        maximum = max(float(row[2]) for row in rows)
        for i, (region, product, amount) in enumerate(rows):
            y = 155+i*110
            d.text((50,y+8), region+" · "+product, font=font, fill="#243958")
            width = round(float(amount)/maximum*560) if maximum else 0
            d.rounded_rectangle((360,y,360+max(1,width),y+48),radius=5,fill="#328bff")
            d.text((935,y+8), f"{amount:,.0f}", font=font, fill="#172c50")
        im.save(img)
