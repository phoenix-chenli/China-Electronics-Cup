"""Local intent previews and persisted project source bindings.

Previews never call a model or modify a source. The execution layer still freezes
connection revisions and exact document versions before it enqueues a task.
"""
from __future__ import annotations

import json
import re
from fastapi import HTTPException
from . import store as s

SCOPES = ('sales', 'database', 'knowledge', 'hybrid')
LABELS = {'sales': '销售示例', 'database': '数据库', 'knowledge': '文档', 'hybrid': '数据库与文档'}


def initialize():
    with s.conn() as db:
        db.execute('CREATE TABLE IF NOT EXISTS ef_project_sources(project_id TEXT PRIMARY KEY,body TEXT NOT NULL)')


def bind(project_id, profile):
    initialize()
    with s.conn() as db:
        if not db.execute('SELECT 1 FROM ef_projects WHERE id=?', (project_id,)).fetchone():
            raise HTTPException(404, '项目不存在')
        db.execute('INSERT OR REPLACE INTO ef_project_sources VALUES (?,?)',
                   (project_id, json.dumps(profile, ensure_ascii=False)))


def profile(project_id=None):
    initialize()
    with s.conn() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='ef_projects'").fetchone()
        project = db.execute('SELECT id,name FROM ef_projects WHERE id=?', (project_id or 'default',)).fetchone() if exists else None
        if project_id and not project:
            raise HTTPException(404, '项目不存在')
        row = db.execute('SELECT body FROM ef_project_sources WHERE project_id=?', (project_id or 'default',)).fetchone()
    body = json.loads(row[0]) if row else {}
    ids = body.get('document_ids')
    docs = s.doc_list()
    if ids is not None:
        docs = [d for d in docs if d['id'] in ids]
    return {**body, 'project_id': project['id'] if project else None,
            'project_name': project['name'] if project else '本机资料',
            'source_label': body.get('source_label', '销售示例与本机文档'),
            'document_count': len(docs), 'documents': [{'id': d['id'], 'name': d['name'], 'format': d['format']} for d in docs],
            'starter_questions': body.get('starter_questions', [])}


def project_docs(project_id=None, archived=False):
    docs = s.doc_list(archived)
    if not project_id:
        return docs
    ids = profile(project_id).get('document_ids')
    return docs if ids is None else [d for d in docs if d['id'] in ids]


def choose(question, context, configured='auto', override=None):
    """Return a transparent recommendation, with explicit choice taking priority."""
    if override is not None and override not in SCOPES:
        raise ValueError('提问范围不正确')
    text = str(question or '').strip()
    public = bool(context.get('managed_public'))
    numeric = bool(re.search(r'多少|几[个人笔次]|总[额数量]|合计|占比|比例|比重|排名|最高|最低|比较|相差|差值|平均|计算|增长|增速|GDP|增加值|生产总值|浓度', text, re.I))
    document = bool(re.search(r'规定|政策|办法|保修|质保|手册|文档|公报|口径|含义|定义|说明|解释|依据|条款|原文|脚注|注释|为什么', text))
    calculation = bool(re.search(r'一致|核对|是否符合|按.*规则|根据.*(规定|规则)|公报.*(比例|比重)|计算.*(公布|解释)', text))
    raw_sql = bool(re.search(r'仅.*(SQL|数据库)|只.*(SQL|数据库)|执行.*SELECT|\bSELECT\s', text, re.I))
    sales = bool(re.search(r'销售|订单|客户|退货|毛利|客单价|产品', text))
    if raw_sql:
        recommended, reason = 'database', '问题明确要求只查询数据库。'
    elif public and numeric:
        recommended, reason = 'hybrid', '统计数值需要数据库计算，并用原始公报核对单位、口径和出处。'
    elif numeric and (document or calculation):
        recommended, reason = 'hybrid', '问题同时涉及数值查询和文档规则。'
    elif document or not numeric and not sales:
        recommended, reason = 'knowledge', '问题主要需要文档内容与原文依据。'
    elif sales and not context.get('database_connection_id'):
        recommended, reason = 'sales', '问题涉及本机销售示例指标。'
    else:
        recommended, reason = 'database', '问题主要需要结构化数据查询。'
    selected = override or (configured if configured in SCOPES else recommended)
    if override:
        reason = '本轮采用你明确选择的范围。' + reason
    elif configured in SCOPES and configured != recommended:
        reason = '当前使用已固定的范围；可以在提问前切换。' + reason
    if public and selected == 'sales':
        raise ValueError('公开统计项目没有销售示例数据，请选择数据库、文档或跨源范围')
    return {'query_scope': selected, 'recommended_scope': recommended, 'selected_scope': selected,
            'reason': reason, 'source_label': context['source_label'], 'document_count': context['document_count'],
            'project_id': context.get('project_id'), 'project_name': context.get('project_name'),
            'scope_options': [{'id': x, 'label': LABELS[x]} for x in SCOPES if not public or x != 'sales']}


def resolve_context(question=None, project_id=None, previous=None, document_context=None,
                    requested_scope=None, scope_override=None):
    if requested_scope and scope_override in {'sales', 'database'}:
        raise ValueError('已选文档范围与数据库单独查询冲突，请选择文档或跨源范围')
    from . import agent
    cfg = agent.saved_config()
    previous = previous or {}
    bound = profile(project_id)
    configured = bound.get('default_scope', cfg.get('query_scope', 'sales'))
    inherited = bool(previous) and not scope_override and not requested_scope and not document_context
    routing = choose(question, bound, configured, scope_override)
    cid = bound.get('database_connection_id', cfg.get('database_connection_id'))
    scope = routing['query_scope']
    rag = requested_scope
    if inherited:
        from . import engine
        metric = previous.get('metric')
        legacy_scope = 'database' if metric == 'database' else 'knowledge' if metric == 'document' else 'sales' if metric in engine.METRICS else scope
        scope = previous.get('query_scope', legacy_scope)
        cid = previous.get('database_connection_id')
        routing.update(selected_scope=scope, query_scope=scope, inherited=True,
                       reason='追问沿用所选结果的固定资料范围与来源版本。')
    if document_context:
        scope = 'knowledge'
        routing.update(selected_scope=scope, query_scope=scope, reason='本轮仅依据当前页或选中文字。')
    elif requested_scope:
        scope = 'hybrid' if scope == 'hybrid' else 'knowledge'
        routing.update(selected_scope=scope, query_scope=scope, reason='本轮采用你选定的文档清单。')
    elif not inherited and scope in {'knowledge', 'hybrid'} and bound.get('document_ids') is not None:
        rag = {'scope_type': 'corpus', 'document_ids': bound['document_ids']}
        if not bound['document_ids']:
            raise ValueError('当前项目没有可用文档，请先导入资料或选择数据库范围')
    if bound.get('managed_public') and scope == 'sales':
        raise ValueError('公开统计项目没有销售示例数据，请选择数据库、文档或跨源范围')
    routing['database_connection_id'] = cid if scope in {'database', 'hybrid'} else None
    return {'query_scope': scope, 'database_connection_id': routing['database_connection_id'],
            'source_vocabulary':bound.get('source_vocabulary') if cid==bound.get('database_connection_id') else None,
            'requested_rag_scope': rag, 'routing': routing}


def preview(question, project_id=None, scope_override=None):
    resolved = resolve_context(question=question, project_id=project_id, scope_override=scope_override)
    return {**resolved['routing'], 'database_connection_id': resolved['database_connection_id'],
            'routing': resolved['routing']}


def query_profile(project_id):
    from . import agent
    bound = profile(project_id)
    return {**agent.config(), **bound, 'profile': bound,
            'scope_options': [{'id': x, 'label': LABELS[x]} for x in SCOPES if not bound.get('managed_public') or x != 'sales'],
            'default_scope': bound.get('default_scope', agent.saved_config().get('query_scope', 'sales')),
            'query_scope': bound.get('default_scope', agent.saved_config().get('query_scope', 'sales')),
            'database_connection_id': bound.get('database_connection_id', agent.saved_config().get('database_connection_id'))}


def project_tables(project_id=None):
    bound = profile(project_id) if project_id else {}
    cid = bound.get('database_connection_id')
    if not cid:
        return s.tables()
    from . import connections, database_query
    snap = database_query.inspect(cid)
    config = connections.ready(cid, connections.DB_KINDS)
    result = []
    with connections.database(config) as db:
        for table in snap['tables']:
            name = table['name']
            quoted = connections.identifier(config, table.get('schema', 'main'), name)
            count = connections.rows(db, f'SELECT COUNT(*) AS n FROM {quoted}', limit=1)['rows'][0]['n']
            result.append({**table, 'row_count': count, 'sample': [], 'relations': table.get('foreign_keys', []),
                           'source': bound.get('source_label'), 'removable': False, 'connection_id': cid})
    return result
