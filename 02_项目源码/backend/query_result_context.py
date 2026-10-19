"""Bounded identities for actual SQL output cells, never original source cells.

Mechanical checks are necessary but insufficient: callers must independently
review each claim's entity, period, metric, unit and query scope. This module
neither executes SQL nor repairs model-provided values/IDs.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from .query_time_annotations import time_annotations

VERSION = 'query-result-context-v1'
NUMBER = re.compile(r'(?<![A-Za-z0-9_.])[+\-−]?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?%?(?![A-Za-z0-9_.])')
NUMERIC = re.compile(r'[+\-−]?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?%?')
MAX_BINDINGS = 32
QUERY_BINDING_INSTRUCTIONS = '''SQL查询结果使用query_bindings，不得编造原表cell_id或table_bindings。每条绑定示例：{"evidence_id":"E3","context_id":"从query_result_context复制","cell_id":"从该context复制","row_index":0,"column_index":2,"column_label":"真实查询输出列名","value":"该格真实JSON值","entity_cells":["同一结果行的对象/时期格ID"],"unit":"仅在有真实单位依据时填写","unit_column":"同一原表直接单位字段的查询输出列名","statement":"正文中原样出现的完整事实子句"}。row_index/column_index为0基。多行结果须选取能在结果中唯一确定该行的实体格组合；单位等重复标签不足以区分行。单行结果可用空entity_cells。正文必须含真实格值和各实体格完整值，不能改负号、虚构单位、把查询输出定位说成原件单元格。字段名可作自然语言表述，但指标、对象、时期、单位和查询范围必须另经逐条query_relations独立复核。没有单位声明时省略unit/unit_column；正文若数值紧接kWh等拉丁单位，必须在该绑定声明unit，并匹配已溯源column_units或unit_column真实单位格，不能只把单位写在正文，不能用SQL常量造单位。每个statement必须在正文中唯一精确出现；两行对比应分别写全各自的日期、对象、数值和单位，不用同一时段等代词替代entity_cells中的真实日期。修正statement时同时修正文。仅明确时间列支持完整年月/年月日的等精度日历表示，不能省年、省日或推断时区。结果截断不能支持完整总体结论。'''

_CALENDAR_COLUMNS = frozenset({'period', 'date', 'month', 'year_month', 'yearmonth',
    'calendar_month', 'report_month', 'report_date', '日期', '时期', '时段', '年月',
    '月份', '统计月份', '统计日期', '统计期', '报告期', '期间'})
_UNIT_COLUMNS = re.compile(r'(?:^|_)unit(?:$|_)|单位', re.I)
_CHINESE_CALENDAR_NUMBER = r'[零〇一二两三四五六七八九十廿卅]{1,4}'
_ADDED_DAY_PRECISION = re.compile(
    r'\s*(?:(?:\d{1,2}|' + _CHINESE_CALENDAR_NUMBER + r')(?:日|号)'
    r'|初[一二三四五六七八九十]{1,3})')
_ADDED_TIME_PRECISION = re.compile(
    r'\s*(?:'
    r'(?:(?:每日|当天)\s*)?(?:(?:上午|下午|凌晨|中午|晚上|早晨)\s*)?'
    r'(?:\d{1,2}|' + _CHINESE_CALENDAR_NUMBER + r')(?:时|点)'
    r'|T|\d{1,2}:|UTC|GMT|[+\-]\d{2}:?\d{2})', re.I)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _display(value):
    if value is None: return 'NULL'
    if value is True: return 'true'
    if value is False: return 'false'
    return str(value)


def _same(left, right):
    if type(left) is type(right): return left == right
    if isinstance(left, bool) or isinstance(right, bool) or left is None or right is None: return False
    try:
        a, b = Decimal(str(left)), Decimal(str(right))
        return a.is_finite() and b.is_finite() and a == b and a.is_signed() == b.is_signed()
    except (ValueError, InvalidOperation): return False


def _identity(evidence):
    if not isinstance(evidence, dict): raise ValueError('查询依据必须是对象')
    for key in ('source_id', 'sha256', 'parser_version'):
        if not isinstance(evidence.get(key), str) or not evidence[key]: raise ValueError('缺少来源版本：'+key)
    if evidence.get('stale') is True: raise ValueError('查询依据来源已过期')
    query, result = evidence.get('query'), evidence.get('query_result')
    if not isinstance(query, dict) or not isinstance(query.get('sql'), str) or not query['sql'].strip():
        raise ValueError('缺少实际执行SQL')
    if not isinstance(query.get('parameters'), list) or len(query['parameters'])>100:
        raise ValueError('缺少有限的实际SQL参数列表')
    if any(not isinstance(v,(str,int,float,bool,type(None))) or isinstance(v,float) and not math.isfinite(v) for v in query['parameters']):
        raise ValueError('SQL参数必须是有限JSON标量')
    if not isinstance(result, dict) or type(result.get('truncated')) is not bool: raise ValueError('缺少查询结果/截断状态')
    columns, rows = result.get('columns'), result.get('rows')
    if not isinstance(columns, list) or not columns or len(columns)>256 or not all(isinstance(c,str) and c for c in columns):
        raise ValueError('查询输出列无效')
    if not isinstance(rows,list) or len(rows)>200 or not all(isinstance(r,list) and len(r)==len(columns) for r in rows):
        raise ValueError('查询输出行无效或超过200行')
    scalar = (str,int,float,bool,type(None))
    if any(not isinstance(v,scalar) or isinstance(v,float) and not math.isfinite(v) for row in rows for v in row):
        raise ValueError('查询值必须是有限JSON标量')
    import sqlglot
    try: canonical = sqlglot.parse_one(query['sql'],read='sqlite').sql(dialect='sqlite',pretty=False)
    except Exception as exc: raise ValueError('实际SQL不能规范化') from exc
    source = {k: evidence.get(k) for k in ('source_id','sha256','parser_version','semantic_revision')}
    query_hash = _hash({'source':source,'sql':canonical,'parameters':query['parameters']})
    context_id = _hash({'version':VERSION,'query_fingerprint':query_hash,'columns':columns,'rows':rows,'truncated':result['truncated'],
        'direct_columns':evidence.get('direct_columns') or {},'column_units':evidence.get('column_units') or {}})
    return source, query_hash, context_id, columns, rows, result['truncated']


def build_query_result_context(evidence, *, max_chars=6000, max_rows=20):
    """Project complete output rows. Omitted rows never yield bindable cell IDs."""
    if type(max_chars) is not int or max_chars<256 or type(max_rows) is not int or max_rows<1:
        raise ValueError('查询结果上下文预算无效')
    try: source, query_hash, context_id, columns, rows, truncated = _identity(evidence)
    except ValueError as exc:
        return {'schema_version':VERSION,'kind':'query_result','usable':False,'errors':[str(exc)],'rows':[],
                'localization_scope':'query_output_not_original_cell','projection_truncated':False}
    out = {'schema_version':VERSION,'kind':'query_result','usable':True,'context_id':context_id,
        'context_alias':'QC'+context_id[:12],
        'query_fingerprint':query_hash,'source':source,'localization_scope':'query_output_not_original_cell',
        'columns':[{'column_index':i,'label':label}for i,label in enumerate(columns)],'rows':[],
        'row_count':len(rows),'query_result_truncated':truncated,'projection_truncated':False,'omitted_rows':0,
        'notice':'仅实际执行SQL的输出格；原查询范围见同一evidence.query，非原表单元格或聚合贡献行。'}
    # A row and all of its labels remain complete. Never trim a value to fit.
    if len(_json(out))>max_chars:
        return {**out,'usable':False,'columns':[],'errors':['完整查询列信息超过上下文预算'],
                'projection_truncated':True,'omitted_rows':len(rows)}
    for r,row in enumerate(rows[:min(max_rows,200)]):
        projected={'row_index':r,'cells':[{'cell_id':f'Q{context_id[:12]}:R{r+1}C{c+1}',
            'column_index':c,'column_label':columns[c],'value':value}for c,value in enumerate(row)]}
        candidate={**out,'rows':[*out['rows'],projected],'omitted_rows':len(rows)-r-1,
                   'projection_truncated':r+1<len(rows)}
        if len(_json(candidate))>max_chars: break
        out['rows'].append(projected)
    out['omitted_rows']=len(rows)-len(out['rows']);out['projection_truncated']=bool(out['omitted_rows'])
    if rows and not out['rows']:
        out['usable']=False;out['errors']=['完整查询结果行超过上下文预算，未提供可绑定格']
    return out


def direct_join_projection(catalog, execution):
    """Reuse declared DDL for ONE nonmultiplying inner join; no extra query.

    This proves physical output-column origins, never join business meaning.
    Unknown/aggregate/self/multiple joins remain unsupported. Unit columns with
    mixed or missing row values are deliberately not eligible in this narrow
    extension. Existing single-table inference and validators are unchanged.
    """
    import sqlglot
    from sqlglot import exp
    try: tree = sqlglot.parse_one(execution.get('sql'), read='sqlite')
    except (sqlglot.errors.ParseError, TypeError): return {}, None
    if (not isinstance(tree, exp.Select) or len(list(tree.find_all(exp.Select))) != 1
            or tree.args.get('group') or tree.args.get('distinct') or tree.args.get('with_')
            or tree.args.get('limit') or tree.args.get('offset') or execution.get('truncated') is not False
            or any(isinstance(n, (exp.AggFunc, exp.Window)) for n in tree.walk())):
        return {}, None
    joins = tree.args.get('joins') or []
    first = tree.args.get('from_')
    if len(joins) != 1 or first is None or not isinstance(first.this, exp.Table): return {}, None
    join = joins[0]; left, right = first.this, join.this
    if (not isinstance(right, exp.Table) or join.args.get('side')
            or str(join.args.get('kind') or '').upper() not in ('', 'INNER')
            or join.args.get('method') or join.args.get('using')
            or left.name.casefold() == right.name.casefold()
            or left.alias_or_name.casefold() == right.alias_or_name.casefold()
            or any(t.db or t.catalog for t in (left, right))): return {}, None
    tables = {t.get('name', '').casefold(): t for t in catalog.get('tables', []) if isinstance(t, dict)}
    actual = [tables.get(t.name.casefold()) for t in (left, right)]
    if any(not t or t.get('constraint_origin') != 'ddl' for t in actual): return {}, None
    aliases = {t.alias_or_name.casefold(): a for t, a in zip((left, right), actual)}
    schema = {alias: {c['name'].casefold(): c['name'] for c in t.get('columns', [])
                      if isinstance(c, dict) and isinstance(c.get('name'), str)} for alias, t in aliases.items()}
    def resolve(node):
        if not isinstance(node, exp.Column) or node.is_star: return None
        hits = [(alias, cols[node.name.casefold()]) for alias, cols in schema.items()
                if node.name.casefold() in cols and (not node.table or node.table.casefold() == alias)]
        return hits[0] if len(hits) == 1 else None
    on = join.args.get('on')
    if not isinstance(on, exp.EQ): return {}, None
    pairs = [resolve(on.this), resolve(on.expression)]
    la, ra = left.alias_or_name.casefold(), right.alias_or_name.casefold()
    if not all(pairs) or {p[0] for p in pairs} != {la, ra}: return {}, None
    right_key = next(p[1] for p in pairs if p[0] == ra)
    unique = [actual[1].get('primary_key') or [], *(actual[1].get('unique_keys') or [])]
    if not any(len(k) == 1 and str(k[0]).casefold() == right_key.casefold() for k in unique): return {}, None
    columns, rows = execution.get('columns'), execution.get('rows')
    if (not isinstance(columns, list) or not isinstance(rows, list) or not rows
            or len(columns) != len(tree.expressions) or len(columns) != len(set(columns))
            or len(rows) > 200 or any(not isinstance(r, list) or len(r) != len(columns) for r in rows)):
        return {}, None
    resolved = [resolve(p.this if isinstance(p, exp.Alias) else p) for p in tree.expressions]
    base_positions = [i for i, p in enumerate(resolved) if p and p[0] == la]
    # Also check the complete returned base projection, conservatively rejecting
    # duplicates (including collation-driven fanout or indistinguishable rows).
    # LIMIT/truncation above cannot hide duplicates from this check.
    if not base_positions: return {}, None
    base_rows = [_json([r[i] for i in base_positions]) for r in rows]
    if len(set(base_rows)) != len(base_rows): return {}, None
    result = {}; rejected_units = []
    for index, (output, projection) in enumerate(zip(columns, tree.expressions)):
        node = projection.this if isinstance(projection, exp.Alias) else projection
        origin = resolve(node)
        if origin is None: continue  # expressions/literals never acquire lineage
        alias, physical = origin
        if _UNIT_COLUMNS.search(physical):
            values = [r[index] for r in rows]
            if (not all(isinstance(v, str) and v and len(v) <= 40 for v in values)
                    or len(set(values)) != 1):
                rejected_units.append(output); continue
        result[output] = aliases[alias]['name'] + '.' + physical
    if not result: return {}, None
    return result, {'method': 'single_many_to_one_inner_join_direct_columns_v1',
        'scope': 'physical_output_only_not_business_relation',
        'source_id': catalog.get('id'), 'sha256': catalog.get('sha256'),
        'left_table': actual[0]['name'], 'right_table': actual[1]['name'],
        'join_columns': [aliases[p[0]]['name']+'.'+p[1] for p in pairs],
        'right_unique_key': [right_key], 'constraint_origin': 'ddl',
        'complete_result_base_projection_unique': True,
        'rejected_mixed_or_missing_unit_outputs': rejected_units,
        'row_count': len(rows), 'query_result_truncated': execution.get('truncated'),
        'semantic_review_required': True}


def _spans(text, value, unit=None):
    literal=_display(value)
    if not literal: return []
    # Latin identifiers and numeric tokens must match in full (A01 != A010).
    if NUMERIC.fullmatch(literal):
        # Match standalone numeric tokens symmetrically. In particular, a
        # selected year 2025 must not mask the prefix of a separate date
        # 2025-01-01 before that full output-cell value is checked below.
        end_boundary=r'(?![A-Za-z0-9_.+\-−])'
        if isinstance(unit,str) and unit:
            end_boundary=r'(?='+re.escape(unit)+r'(?![A-Za-z0-9_.])|[^A-Za-z0-9_.+\-−]|$)'
        pattern=r'(?<![A-Za-z0-9_.+\-−])'+re.escape(literal)+end_boundary
    else:
        pattern=(r'(?<![A-Za-z0-9_.])' if literal[0].isascii() and literal[0].isalnum() else '')+re.escape(literal)
        pattern+=(r'(?![A-Za-z0-9_.])' if literal[-1].isascii() and literal[-1].isalnum() else '')
    return [(m.start(),m.end())for m in re.finditer(pattern,text)]


def _mask(text, spans):
    chars=list(text)
    for start,end in spans:chars[start:end]=' '*(end-start)
    return ''.join(chars)


def _calendar_variants(value):
    """Only complete legal calendar dates/months; no inferred precision/timezone."""
    if not isinstance(value, str): return []
    match = re.fullmatch(r'(\d{4})-(\d{2})(?:-(\d{2}))?', value)
    if not match:
        match = re.fullmatch(r'(\d{4})年(\d{1,2})月(?:(\d{1,2})日)?', value)
    if not match: return []
    year, month = int(match[1]), int(match[2])
    day = int(match[3]) if match[3] is not None else None
    try: date(year, month, day if day is not None else 1)
    except ValueError: return []
    variants = {f'{year:04d}-{month:02d}' + (f'-{day:02d}' if day is not None else '')}
    for month_text in {str(month), f'{month:02d}'}:
        if day is None: variants.add(f'{year:04d}年{month_text}月')
        else:
            for day_text in {str(day), f'{day:02d}'}:
                variants.add(f'{year:04d}年{month_text}月{day_text}日')
    return sorted(variants, key=lambda s: (-len(s), s))


def _calendar_eligible(cell, evidence):
    column = cell['column_label']
    direct = (evidence.get('direct_columns') or {}).get(column)
    return not (not isinstance(direct, str) or '.' not in direct
            or column.lower() not in _CALENDAR_COLUMNS
            or direct.rsplit('.', 1)[-1].strip('"`[]').lower() not in _CALENDAR_COLUMNS)


def _calendar_spans(text, cell, evidence):
    """Use controller-resolved original time fields, never an arbitrary alias."""
    if not _calendar_eligible(cell,evidence): return []
    spans = []
    for variant in _calendar_variants(cell['value']):
        # A month is not a partial match inside a complete day; a day is not
        # a prefix of a timestamp. Do not silently adopt extra precision.
        suffix = (r'(?![0-9A-Za-z_./+\-]|\d{1,2}日)' if variant.endswith('月')
                  else r'(?![A-Za-z_./+\-])' if variant.endswith('日')
                  else r'(?![0-9A-Za-z_./+\-])')
        pattern = r'(?<![0-9A-Za-z_./+\-])' + re.escape(variant) + suffix
        for match in re.finditer(pattern, text):
            tail = text[match.end():]
            month_precision = bool(re.fullmatch(r'\d{4}(?:-\d{2}|年\d{1,2}月)', str(cell['value'])))
            if (_ADDED_TIME_PRECISION.match(tail)
                    or month_precision and _ADDED_DAY_PRECISION.match(tail)):
                continue
            span = (match.start(), match.end())
            if span not in spans: spans.append(span)
    return sorted(spans)


def _entity_spans(text, cell, evidence):
    if _calendar_eligible(cell,evidence) and _calendar_variants(cell['value']):
        return _calendar_spans(text,cell,evidence)
    return _spans(text,cell['value'])


def _short(value):
    return repr(_display(value)[:100])


def _unit_declaration_hint(statement, cell, evidence, row_index):
    """Explain a rejected attached unit; never infer/add a binding or accept it."""
    direct = evidence.get('direct_columns') or {}
    value_source = direct.get(cell['column_label'])
    if not isinstance(value_source, str): return ''
    columns = evidence['query_result']['columns']
    for index, column in enumerate(columns):
        if not _UNIT_COLUMNS.search(column): continue
        unit_source = direct.get(column)
        unit = evidence['query_result']['rows'][row_index][index]
        if (not isinstance(unit_source, str) or unit_source.rsplit('.', 1)[0] != value_source.rsplit('.', 1)[0]
                or not isinstance(unit, str) or not unit or len(unit) > 40): continue
        if _spans(statement, cell['value'], unit):
            return ('正文数值紧接单位但本绑定未声明unit；请显式提供unit=' + _short(unit)
                    + '、unit_column=' + _short(column) + '，它们须继续通过真实同表字段核验')
    return ''


def check_query_bindings(text, bindings, evidence_records):
    """Validate identities and literal row bindings; semantic review is mandatory.

    Unbound ordinary text is returned unchanged. No allowed-number bag is built:
    numeric_text removes only exact spans within successfully bound statements.
    """
    if not isinstance(text,str):return {'required':True,'valid':False,'bindings':[],'errors':['事实正文必须是字符串'],'numeric_text':'','context_annotations':[]}
    records=[e for e in (evidence_records or []) if isinstance(e,dict) and e.get('query_result') is not None]
    required=bool(bindings) or bool(records and NUMBER.search(text))
    if not required:return {'required':False,'valid':True,'bindings':[],'errors':[],'numeric_text':text,'context_annotations':[]}
    errors=[];normalized=[];annotations=[];mask_spans=[]
    if not isinstance(bindings,list) or not bindings or len(bindings)>MAX_BINDINGS:
        return {'required':True,'valid':False,'bindings':[],'errors':['查询事实须提供1至32项query_bindings'],'numeric_text':text,'context_annotations':[]}
    for index,binding in enumerate(bindings):
        prefix=f'第{index+1}项查询绑定：'
        if not isinstance(binding,dict):errors.append(prefix+'必须是对象');continue
        if not all(isinstance(binding.get(k),str) and binding[k] for k in ('evidence_id','context_id','cell_id')):
            errors.append(prefix+'evidence_id/context_id/cell_id必须是非空字符串');continue
        matches=[e for e in records if e.get('id')==binding.get('evidence_id')]
        if len(matches)!=1:errors.append(prefix+'evidence_id未唯一指向本事实引用的查询依据');continue
        evidence=matches[0]
        context=build_query_result_context(evidence)
        stored=evidence.get('query_result_context')
        # Caller may supply a smaller bounded projection, never a more permissive one.
        if isinstance(stored,dict):
            if stored.get('context_id')!=context.get('context_id') or stored.get('usable') is not True:
                errors.append(prefix+'所提供的结果上下文已变化或不可用');continue
            allowed={c.get('cell_id')for row in stored.get('rows',[])for c in row.get('cells',[])}
        else:
            allowed={c['cell_id']for row in context.get('rows',[])for c in row['cells']}
        if not context.get('usable') or binding.get('context_id')!=context.get('context_id'):
            errors.append(prefix+'context_id与当前实际查询/来源版本/结果不一致');continue
        lookup={c['cell_id']:(row['row_index'],c)for row in context['rows']for c in row['cells']if c['cell_id'] in allowed}
        entry=lookup.get(binding.get('cell_id'))
        if entry is None:errors.append(prefix+'cell_id不在本次提供的查询结果格中');continue
        row_index,cell=entry;column_index=cell['column_index']
        if (type(binding.get('row_index')) is not int or type(binding.get('column_index')) is not int
                or binding['row_index']!=row_index or binding['column_index']!=column_index
                or binding.get('column_label')!=cell['column_label'] or not _same(binding.get('value'),cell['value'])):
            errors.append(prefix+'行/列/列名/真实格值不匹配');continue
        if cell['value'] is None:errors.append(prefix+'NULL不能绑定成已知事实数值');continue
        statement=binding.get('statement')
        if not isinstance(statement,str) or not statement or text.count(statement)!=1:
            errors.append(prefix+'statement须是正文中唯一精确子句（当前出现次数='+str(text.count(statement) if isinstance(statement,str) and statement else 0)
                          +'）；请从正文复制完整子句，修改statement时同步修改正文，不自行补主语或日期');continue
        entities=binding.get('entity_cells',[])
        if not isinstance(entities,list) or len(entities)>16 or not all(isinstance(v,str) and v for v in entities) or len(entities)!=len(set(entities)):
            errors.append(prefix+'entity_cells必须是不重复的有限格ID列表');continue
        entity_values=[];local_spans=[];entity_columns=[];bad=False
        for entity_id in entities:
            entity=lookup.get(entity_id)
            if entity is None or entity[0]!=row_index or entity_id==cell['cell_id'] or entity[1]['value'] in (None,''):
                errors.append(prefix+'实体格须是同一结果行的其他真实非空格');bad=True;break
            ecell=entity[1];spans=_entity_spans(statement,ecell,evidence)
            if not spans:
                errors.append(prefix+'statement缺少完整实体格值：'+ecell['cell_id']+'，列'+_short(ecell['column_label'])+'，真实值'+_short(ecell['value'])
                              +'；请在正文和本子句同时写全此值，不能用同一时段等代词替代');bad=True;break
            local_spans.extend(spans);entity_columns.append(ecell['column_index'])
            entity_values.append(copy.deepcopy(ecell))
        if bad:continue
        full_rows=evidence['query_result']['rows']
        if len(full_rows)>1:
            matches_count=sum(all(_same(row[c],full_rows[row_index][c]) for c in entity_columns)for row in full_rows)
            if not entities or matches_count!=1:
                errors.append(prefix+'多行结果须有在完整结果中唯一确定本行的实体格组合');continue
        remaining=_mask(statement,local_spans)
        value_spans=(_calendar_spans(remaining,cell,evidence)
                     if _calendar_eligible(cell,evidence) and _calendar_variants(cell['value'])
                     else _spans(remaining,cell['value'],binding.get('unit')))
        if not value_spans:
            hint = _unit_declaration_hint(remaining,cell,evidence,row_index) if binding.get('unit') is None else ''
            errors.append(prefix+(hint or ('statement缺少原格完整值（含符号）：'+cell['cell_id']+'，列'+_short(cell['column_label'])+'，真实值'+_short(cell['value']))));continue
        # Repeated numeric values are separate factual occurrences, not a licence
        # to reuse a genuine cell for an extra value with a different meaning.
        if NUMERIC.fullmatch(_display(cell['value'])) and len(value_spans)!=1:
            errors.append(prefix+'同一数值在子句重复，须拆分事实分别绑定');continue
        if NUMERIC.fullmatch(_display(cell['value'])) and not _display(cell['value']).endswith('%'):
            if any(statement[end:end+1]=='%' for _,end in value_spans) and binding.get('unit')!='%':
                errors.append(prefix+'不能把普通结果值改成百分数');continue
        local_spans.extend(value_spans)
        if binding.get('unit') is not None:
            unit=binding['unit'];units=[]
            mapped=(evidence.get('column_units')or{}).get(cell['column_label'])or{}
            if isinstance(mapped,dict) and mapped.get('unit') and mapped.get('source_columns'):
                units.append(mapped['unit'])
            unit_column=binding.get('unit_column')
            if unit_column is not None:
                columns=evidence['query_result']['columns'];direct=evidence.get('direct_columns') or {}
                value_source=direct.get(cell['column_label']);unit_source=direct.get(unit_column) if isinstance(unit_column,str) else None
                if (not isinstance(unit_column,str) or columns.count(unit_column)!=1 or not isinstance(value_source,str)
                    or not isinstance(unit_source,str) or value_source.rsplit('.',1)[0]!=unit_source.rsplit('.',1)[0]):
                    errors.append(prefix+'unit_column和值列必须来自同一真实原表的直接字段，不能用常量或伪造别名');continue
                units.append(full_rows[row_index][columns.index(unit_column)])
            if not isinstance(unit,str) or not unit or unit not in units or unit not in statement:
                errors.append(prefix+'unit没有本结果行/列的真实单位依据或未出现在子句');continue
        # Column labels may contain source years/ordinals; only exact occurrences
        # in this bound statement are treated as labels, never global numbers.
        local_spans.extend(_spans(statement,cell['column_label']))
        filter_annotations=time_annotations(statement,evidence)
        local_spans.extend((a['statement_start'],a['statement_end']) for a in filter_annotations)
        residual=_mask(statement,local_spans)
        if NUMBER.search(residual):
            errors.append(prefix+'子句还有未由本格/实体格支持的数值，须拆分或补真实绑定');continue
        offset=text.index(statement)
        mask_spans.extend((offset+a,offset+b)for a,b in local_spans)
        item={**copy.deepcopy(binding),'value':copy.deepcopy(cell['value']),'entity_values':entity_values,
            'source':copy.deepcopy(context['source']),'query_fingerprint':context['query_fingerprint'],
            'localization_scope':'query_output_not_original_cell','query_result_truncated':context['query_result_truncated'],
            'semantic_review_required':True}
        date_annotations=[]
        for ecell in [cell,*entity_values]:
            for start,end in _calendar_spans(statement,ecell,evidence):
                if statement[start:end] != _display(ecell['value']):
                    date_annotations.append({'kind':'equivalent_calendar_display','cell_id':ecell['cell_id'],
                        'column_label':ecell['column_label'],'canonical_value':copy.deepcopy(ecell['value']),
                        'display_text':statement[start:end],'statement_start':start,'statement_end':end,
                        'claim_start':offset+start,'claim_end':offset+end,'precision':'day' if len(_display(ecell['value']).split('-'))==3 or str(ecell['value']).endswith('日') else 'month'})
        filter_annotations=[{**a,'claim_start':offset+a['statement_start'],'claim_end':offset+a['statement_end'],
                             'query_fingerprint':context['query_fingerprint']} for a in filter_annotations]
        if date_annotations or filter_annotations: item['context_annotations']=[*date_annotations,*filter_annotations]
        annotations.extend(filter_annotations)
        normalized.append(item)
        annotations.append({'type':'query_output_binding','evidence_id':evidence['id'],'context_id':context['context_id'],
            'cell_id':cell['cell_id'],'statement_start':offset,'statement_end':offset+len(statement),
            'localization_scope':'query_output_not_original_cell'})
    valid=not errors and len(normalized)==len(bindings)
    return {'required':required,'valid':valid,'bindings':normalized if valid else [],'errors':errors,
        'numeric_text':_mask(text,mask_spans) if valid else text,'context_annotations':annotations if valid else []}
