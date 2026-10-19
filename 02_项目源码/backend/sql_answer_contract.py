"""Bounded SQL answer-scope facts for a separate, explicit semantic review.

The candidate contract is untrusted. AST scopes and declared schema are facts
about this query/source, not proof that the query answers the user's question.
"""
from __future__ import annotations

import json
import sqlglot
from sqlglot import exp

REVIEW_FIELDS = ('source_basis', 'population_grain', 'quantifier_scope', 'missing_values', 'output_contract')
MAX_CHARS = 24000

PLAN_INSTRUCTIONS = '''规划时明确 answer_contract（候选，不是用户确认）：
{source_basis:[{table:实际表名,role:"entity_population|stored_metric|event|bridge",reason:选择依据}],
population:{table:实体母表,include_no_related_rows:true|false|null},result_grain:[{table,column}],
quantifiers:[{entity_key:{table,column},mode:"exists|none|all|per_record",related_tables:[实际表名],predicate:条件}],
null_policy:保留/补值/筛除的用户依据,output_columns:[用户要求输出的列含单位]}。
表名相似、数据重合不证明明细重算与已有汇总是相同口径；优先用户指定的实际资料层级。
量词必须放在其实体粒度：某实体存在一条没有关联记录的明细，不等于该实体所有明细都无关联记录。
用户要求全部实体时必须保留无子记录实体；未授权不得过滤NULL行或把未知补0。
最终投影遵守用户要求的列；内部排序/窗口名次可以不输出。列别名不得增加用户/SQL未支持的“只、全部、从未”等限定。
金额/比率显示精度与SQL计算精度分别处理，使用满足用户要求的显示格式，不能丢弃原值。'''

REVIEW_INSTRUCTIONS = '''answer_scope 中 candidate 是未确认的规划解释，sql_scopes 是实际SQL结构，source_tables/declared_relationships仅来自本次真实schema。
对照原始问题、上文和完整catalog候选，必须逐项独立核对并返回 scope_review 的五个布尔字段：
source_basis：所用事实表/已有汇总口径是否正确，不能因候选可执行就默认源选对。
population_grain：结果粒度/分组层级/实体母集是否对应问题，检查零子记录实体、全年分组与逐月分组、逐行与汇总。
quantifier_scope：存在/完全没有/所有的作用域及关联键是否正确。尤其输出上层实体而NOT EXISTS仅关联下层单条记录时，须对照问题判断，不能靠DISTINCT弥补量词。
missing_values：不得增加未要求的IS NOT NULL过滤或COALESCE。SUM忽略NULL但全NULL才NULL；这不等于把每个未知原值补0，也不意味着同组只要一个NULL就必须整组NULL。原始值、已知值合计与未知项明确区分。
output_contract：输出列、列别名限定词、单位和实际展示精度是否正确。数值精度和固定小数显示不同；检查display_rows，不要求使用某个特定SQL函数。DECIMAL文本本身已满足两位显示时，不因缺PRINTF/ROUND否决。预览不是全表证明，仍检查SQL及display_contract。
scope_review只含这五项true/false。任何一项未知或不支持填false并说明具体问题，不可用整体supported代替。
不根据风险提示自动判错；风险仅指出需要核对的位置，不改变真实数据，不将模型建议修正视为事实。'''


def _text(value, limit=400):
    return value.strip()[:limit] if isinstance(value, str) else ''


def candidate_contract(raw, catalog):
    tables = {t['name']: {c['name'] for c in t.get('columns', [])} for t in catalog.get('tables', [])}
    out = {'status': 'candidate_requires_independent_review', 'provided': isinstance(raw, dict), 'issues': [], 'truncated': False}
    if not isinstance(raw, dict):
        return out
    def bounded_list(value, limit, field):
        if not isinstance(value, list):
            out['issues'].append('invalid_' + field); return []
        if len(value) > limit:
            out['truncated'] = True
            out['issues'].append('candidate_contract_truncated')
        return value[:limit]
    def text(value, limit=400):
        if isinstance(value, str) and len(value.strip()) > limit:
            out['truncated'] = True
            out['issues'].append('candidate_contract_truncated')
        return _text(value, limit)
    def ref(value):
        if (not isinstance(value, dict) or not isinstance(value.get('table'), str) or value['table'] not in tables
                or not isinstance(value.get('column'), str) or value['column'] not in tables[value['table']]):
            out['issues'].append('unknown_schema_reference'); return None
        return {'table': value['table'], 'column': value['column']}
    basis = raw.get('source_basis', [])
    out['source_basis'] = []
    for value in bounded_list(basis, 16, 'source_basis'):
        if isinstance(value, dict) and isinstance(value.get('table'), str) and value['table'] in tables:
            out['source_basis'].append({'table': value['table'], 'role': text(value.get('role'), 40), 'reason': text(value.get('reason'))})
        else:out['issues'].append('unknown_source_basis')
    population = raw.get('population')
    if isinstance(population, dict) and isinstance(population.get('table'), str) and population['table'] in tables:
        flag = population.get('include_no_related_rows')
        out['population'] = {'table': population['table'], 'include_no_related_rows': flag if type(flag) is bool else None}
    elif population is not None:
        out['issues'].append('unknown_population')
    grain = raw.get('result_grain', [])
    out['result_grain'] = [r for value in bounded_list(grain, 16, 'result_grain') if (r := ref(value))]
    out['quantifiers'] = []
    values = raw.get('quantifiers', [])
    for value in bounded_list(values, 8, 'quantifiers'):
        if not isinstance(value, dict):
            out['issues'].append('invalid_quantifier'); continue
        key = ref(value.get('entity_key'))
        related = bounded_list(value.get('related_tables', []), 16, 'related_tables')
        if not key:continue
        if value.get('mode') not in ('exists', 'none', 'all', 'per_record'):
            out['issues'].append('invalid_quantifier_mode'); continue
        if any(not isinstance(t, str) or t not in tables for t in related):
            out['issues'].append('unknown_related_table');continue
        out['quantifiers'].append({'entity_key': key, 'mode': value['mode'], 'related_tables': related, 'predicate': text(value.get('predicate'))})
    out['null_policy'] = text(raw.get('null_policy'))
    columns = raw.get('output_columns', [])
    out['output_columns'] = [text(v, 120) for v in bounded_list(columns, 32, 'output_columns') if isinstance(v, str)]
    out['issues'] = list(dict.fromkeys(out['issues']))
    return out


def build_answer_scope(sql, plan, catalog, source):
    tables = {t['name']: t for t in catalog.get('tables', [])}
    out = {'version': 'sql-answer-scope-1', 'source_sha256': source.get('sha256'),
           'candidate': candidate_contract(plan.get('answer_contract'), catalog),
           'sql_scopes': [], 'source_tables': [], 'declared_relationships': [], 'truncated': False,
           'boundary': 'AST/DDL facts only; business scope and candidate claims require independent review.'}
    tree = sqlglot.parse_one(sql, read='sqlite')  # Executor has already accepted this SQL.
    selects = list(tree.find_all(exp.Select)); indices = {id(s): i for i, s in enumerate(selects)}
    used = {t.name for t in tree.find_all(exp.Table) if t.name in tables}
    for name in sorted(tables, key=lambda n: (n not in used, n))[:64]:
        table = tables[name]
        out['source_tables'].append({'table': name, 'used': name in used, 'primary_key': table.get('primary_key', []),
                                    'columns': [c['name'] for c in table.get('columns', [])[:40]],
                                    'columns_truncated': len(table.get('columns', [])) > 40})
    out['truncated'] = len(tables) > 64 or len(selects) > 16 or any(t['columns_truncated'] for t in out['source_tables'])
    out['declared_relationships'] = [{k: r.get(k) for k in ('table', 'columns', 'referenced_table', 'referenced_columns', 'origin', 'data_integrity_verified')}
                                     for r in catalog.get('relationships', [])[:64]]
    out['truncated'] |= len(catalog.get('relationships', [])) > 64
    alias_maps = {}
    for select in selects:
        direct = [select.args['from_'].this] if select.args.get('from_') else []
        direct += [j.this for j in select.args.get('joins', [])]
        alias_maps[id(select)] = {t.alias_or_name: t.name for t in direct if isinstance(t, exp.Table)}
    def resolved(column, select):
        owner = select
        while owner is not None:
            aliases = alias_maps.get(id(owner), {})
            if column.table and column.table in aliases:
                name = aliases[column.table]
                return {'scope': indices[id(owner)], 'table': name, 'column': column.name,
                        'basis': 'physical_column' if name in tables else 'derived_column_unresolved'}
            if not column.table:
                # An unqualified field in a multi-relation scope stays unknown.
                names = [n for n in aliases.values() if n in tables and column.name in {c['name'] for c in tables[n].get('columns', [])}]
                if len(names) == 1:return {'scope': indices[id(owner)], 'table': names[0], 'column': column.name, 'basis': 'physical_column'}
                if aliases:return {'scope': indices[id(owner)], 'column': column.name, 'basis': 'unqualified_or_derived_unresolved'}
            owner = owner.find_ancestor(exp.Select)
        return {'column': column.name, 'basis': 'unresolved'}
    for select in selects[:16]:
        index = indices[id(select)]; parent = select.find_ancestor(exp.Select)
        projections = [p.sql(dialect='sqlite') for p in select.expressions]
        groups = [p.sql(dialect='sqlite') for p in (select.args.get('group').expressions if select.args.get('group') else [])]
        record = {'scope': index, 'parent_scope': indices.get(id(parent)), 'relations': alias_maps[id(select)],
                  'projections': [p[:500] for p in projections[:32]],
                  'group_by': [p[:500] for p in groups[:16]],
                  'distinct': bool(select.args.get('distinct')), 'quantifiers': [], 'null_filters': [],
                  'truncated': (len(projections) > 32 or len(groups) > 16
                                or any(len(p) > 500 for p in projections[:32] + groups[:16]))}
        for exists in select.find_all(exp.Exists):
            if exists.find_ancestor(exp.Select) is not select:continue
            inner = exists.this if isinstance(exists.this, exp.Select) else exists.find(exp.Select)
            if inner is None:continue
            refs = []
            for col in inner.find_all(exp.Column):
                if col.find_ancestor(exp.Select) is not inner:continue
                value = resolved(col, inner)
                if value.get('scope') not in (None, indices[id(inner)]) and value not in refs:refs.append(value)
            record['quantifiers'].append({'operator': 'NOT EXISTS' if isinstance(exists.parent, exp.Not) else 'EXISTS',
                                          'subquery_scope': indices[id(inner)], 'outer_correlations': refs[:16],
                                          'truncated': len(refs) > 16})
            record['truncated'] |= len(refs) > 16
        where = select.args.get('where')
        if where:
            for node in where.find_all(exp.Is):
                if node.find_ancestor(exp.Select) is select and isinstance(node.expression, exp.Null):
                    predicate = (node.parent if isinstance(node.parent, exp.Not) else node).sql(dialect='sqlite')
                    record['null_filters'].append({'predicate': predicate[:300]})
                    record['truncated'] |= len(predicate) > 300
        record['truncated'] |= len(record['quantifiers']) > 16 or len(record['null_filters']) > 16
        record['quantifiers'] = record['quantifiers'][:16];record['null_filters'] = record['null_filters'][:16]
        out['truncated'] |= record['truncated']
        out['sql_scopes'].append(record)
    if len(json.dumps(out, ensure_ascii=False)) > MAX_CHARS:
        out['truncated'] = True
        out['source_tables'] = out['source_tables'][:16]
        out['declared_relationships'] = out['declared_relationships'][:16]
        out['sql_scopes'] = out['sql_scopes'][:4]
        if len(json.dumps(out, ensure_ascii=False)) > MAX_CHARS:
            out['sql_scopes'] = [];out['boundary'] += ' Scope budget exceeded; no complete structural review supplied.'
        for section in ('source_tables', 'declared_relationships'):
            while out[section] and len(json.dumps(out, ensure_ascii=False)) > MAX_CHARS:
                out[section].pop()
        if len(json.dumps(out, ensure_ascii=False)) > MAX_CHARS:
            out['candidate'] = {'status': 'candidate_requires_independent_review', 'provided': True,
                                'issues': ['answer_scope_budget_exceeded']}
    return out


def scope_review_passed(check):
    review = check.get('scope_review') if isinstance(check, dict) else None
    return isinstance(review, dict) and all(review.get(key) is True for key in REVIEW_FIELDS)
