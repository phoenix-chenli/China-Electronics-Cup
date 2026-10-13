"""Install the reviewed public demo without changing the synthetic regression DB."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from . import store as s, connections, query_routing

PROJECT_ID = 'public-statistics'
SOURCE_DIR = s.PROJECT_ROOT / '02_数据资料' / '公开统计分析'
REVIEW_FILE = s.PROJECT_ROOT / '03_测试验收' / '验收证据' / '公开统计独立资料核验-20261011.json'

SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE public_regions(region_id TEXT PRIMARY KEY,region_name TEXT NOT NULL UNIQUE);
CREATE TABLE public_indicators(metric_id TEXT PRIMARY KEY,metric_name TEXT NOT NULL,unit TEXT NOT NULL,definition TEXT NOT NULL);
CREATE TABLE public_sources(source_id TEXT PRIMARY KEY,title TEXT NOT NULL,institution TEXT NOT NULL,official_url TEXT NOT NULL,
 document_year INTEGER NOT NULL,published_at TEXT,original_sha256 TEXT NOT NULL,page_count INTEGER NOT NULL,source_note TEXT);
CREATE TABLE public_source_anchors(anchor_id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES public_sources(source_id),
 source_pdf_page INTEGER NOT NULL,source_quote TEXT NOT NULL,source_sha256 TEXT NOT NULL);
CREATE TABLE public_observations(region_id TEXT NOT NULL REFERENCES public_regions(region_id),year INTEGER NOT NULL,
 metric_id TEXT NOT NULL REFERENCES public_indicators(metric_id),value_decimal TEXT NOT NULL,value REAL NOT NULL,
 unit TEXT NOT NULL,population_type TEXT,price_basis TEXT,statistical_scope TEXT,preliminary INTEGER NOT NULL,
 source_id TEXT NOT NULL REFERENCES public_sources(source_id),anchor_id TEXT NOT NULL REFERENCES public_source_anchors(anchor_id),
 review_status TEXT NOT NULL,source_note TEXT,
 PRIMARY KEY(region_id,year,metric_id));
CREATE INDEX public_observations_metric_year ON public_observations(metric_id,year);
'''


def build_database(path, curated, catalog):
    """Controlled import. App query connections subsequently use mode=ro."""
    path = Path(path)
    temporary = path.with_suffix('.preparing.db')
    if temporary.exists(): temporary.unlink()
    db = sqlite3.connect(temporary)
    try:
        db.executescript(SCHEMA)
        db.executemany('INSERT INTO public_regions VALUES (?,?)', [(r['region_id'], r['region_name']) for r in curated['regions']])
        db.executemany('INSERT INTO public_indicators VALUES (?,?,?,?)', [(r['metric_id'], r['metric_name'], r['unit'], r['definition']) for r in curated['indicators']])
        for source in catalog['sources']:
            db.execute('INSERT INTO public_sources VALUES (?,?,?,?,?,?,?,?,?)',
                tuple(source.get(k) for k in ['source_id','title','institution','official_url','document_year','published_at','original_sha256','page_count','metadata_warning']))
        anchors = set()
        for row in curated['observations']:
            aid = row['source_id'] + ':p' + str(row['source_pdf_page'])
            if aid not in anchors:
                sha = next(x['original_sha256'] for x in catalog['sources'] if x['source_id'] == row['source_id'])
                db.execute('INSERT INTO public_source_anchors VALUES (?,?,?,?,?)',
                    (aid, row['source_id'], row['source_pdf_page'], row['source_quote'], sha))
                anchors.add(aid)
            for additional in row.get('additional_source_quotes', []):
                extra_aid=row['source_id']+':p'+str(additional['source_pdf_page'])
                if extra_aid not in anchors:
                    sha=next(x['original_sha256'] for x in catalog['sources'] if x['source_id']==row['source_id'])
                    db.execute('INSERT INTO public_source_anchors VALUES (?,?,?,?,?)',
                               (extra_aid,row['source_id'],additional['source_pdf_page'],additional['source_quote'],sha))
                    anchors.add(extra_aid)
            db.execute('INSERT INTO public_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (row['region_id'], row['year'], row['metric_id'], row['value_decimal'], float(row['value_decimal']),
                 row['unit'], row.get('population_type'), row.get('price_basis'), row.get('statistical_scope'), int(row['preliminary']),
                 row['source_id'], aid, 'independent_agent_visual_review', row.get('source_note')))
        db.commit()
        if db.execute('PRAGMA foreign_key_check').fetchall(): raise ValueError('公开统计来源关联检查未通过')
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise ValueError('公开统计数据库完整性检查未通过')
    finally: db.close()
    os.replace(temporary, path)


def install():
    data_path = SOURCE_DIR / '指标核验.json'
    manifest_path = SOURCE_DIR / '来源清单.json'
    if not data_path.exists() or not manifest_path.exists() or not REVIEW_FILE.exists():
        return {'installed': False, 'reason': '公开指标或独立资料核验尚未准备完成'}
    data = data_path.read_bytes()
    curated, catalog = json.loads(data), json.loads(manifest_path.read_bytes())
    review = json.loads(REVIEW_FILE.read_bytes())
    digest = hashlib.sha256(data).hexdigest()
    if review.get('indicators_sha256') != digest or review.get('status') != 'verified':
        return {'installed': False, 'reason': '资料核验版本与指标版本不一致'}
    document_sources = (catalog['sources'] + catalog.get('derived_documents', [])
                        + catalog.get('supplemental_documents', []))
    for source in document_sources:
        expected = source.get('file_sha256', source.get('original_sha256'))
        if s.digest(SOURCE_DIR / source['file_path']) != expected:
            raise ValueError('公开资料原件摘要不一致，未导入')
    query_routing.initialize()
    with s.conn() as db:
        old = db.execute('SELECT body FROM ef_project_sources WHERE project_id=?', (PROJECT_ID,)).fetchone()
        old = json.loads(old[0]) if old else {}
    db_path = s.DATA / 'public-statistics.db'
    if not db_path.exists() or old.get('indicators_sha256') != digest:
        build_database(db_path, curated, catalog)
    configs = [x for x in connections.listing() if x['kind'] == 'sqlite' and x.get('path') == str(db_path.resolve())]
    if configs:
        config = configs[0]
    else:
        config = connections.create(connections.Config(name='公开统计公报 只读', kind='sqlite', path=str(db_path.resolve())))
        tested = connections.test(config['id'])
        if not tested['last_test']['ok']: raise ValueError('公开统计数据库只读连接测试未通过')
        config = connections.enabled(config['id'], connections.Enabled(enabled=True))
    # Existing user-disabled sources remain disabled; startup never undoes them.
    folder = s.DOCS / PROJECT_ID
    folder.mkdir(exist_ok=True)
    ids = []
    primary_ids = []
    for source in document_sources:
        original = SOURCE_DIR / source['file_path']
        target = folder / original.name
        if not target.exists() or s.digest(target) != s.digest(original): shutil.copy2(original, target)
        with s.conn() as db:
            existing = db.execute('SELECT id,status FROM documents WHERE path=?', (str(target.relative_to(s.DATA)),)).fetchone()
        if existing and existing['status'] == 'archived':
            did = existing['id']
        else:
            did = s.index_document(target)['id']
        ids.append(did)
        if source in catalog['sources'] + catalog.get('supplemental_documents', []):
            primary_ids.append(did)
        provenance = {k: v for k, v in source.items() if k not in {'file_path', 'landing_url'}}
        provenance['file_sha256'] = s.digest(original)
        with s.conn() as db:
            quality = json.loads(db.execute('SELECT quality_json FROM documents WHERE id=?', (did,)).fetchone()[0] or '{}')
            quality['source_provenance'] = provenance
            db.execute('UPDATE documents SET quality_json=? WHERE id=?', (json.dumps(quality, ensure_ascii=False), did))
    stamp = s.now()
    with s.conn() as db:
        db.execute('INSERT OR IGNORE INTO ef_projects VALUES (?,?,?,?,?)',
                   (PROJECT_ID, '公开统计分析', 'sales-demo', stamp, stamp))
    query_routing.bind(PROJECT_ID, {'default_scope': 'auto', 'managed_public': True,
        'database_connection_id': config['id'], 'document_ids': ids, 'primary_document_ids': primary_ids,
        'source_vocabulary':{'regions':[{'region_id':r['region_id'], 'region_name':r['region_name']} for r in curated['regions']],
            'indicators':[{k:i[k] for k in ('metric_id','metric_name','unit')} for i in curated['indicators']],
            'indicators_sha256':digest,'boundary':'只含当前公开资料的指标名称、单位和地区名称，详细口径须查原文；不含问答样例或指标观测数值。'},
        'source_label': '三地官方统计、产业政策、碳市场与开源技术原件', 'indicators_sha256': digest,
        'starter_questions': ['2024年新津民营经济增加值占GDP的比例是多少？计算结果与公报公布的比例是否一致？',
            '比较2024年淮南、新津、石景山的GDP和第三产业比重，并注明单位和出处。',
            '三地人口能直接比较吗？请核对常住与户籍口径。',
            '新津2024年GDP现价增幅与公布的实际增速为什么不同？',
            '石景山2024年人形机器人方案的医疗与养老任务分别由谁牵头？这些任务是否已经完成？']})
    if not old:
        # Keep synthetic source fixtures separate from the public presentation.
        legacy = [d['id'] for d in s.doc_list() if d['id'] not in ids]
        query_routing.bind('default', {'default_scope': 'auto', 'document_ids': legacy,
                                      'source_label': '合成销售回归数据与示例文档'})
    return {'installed': True, 'project_id': PROJECT_ID, 'observations': len(curated['observations']), 'document_count': len(ids)}
