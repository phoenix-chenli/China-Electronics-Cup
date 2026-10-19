"""Bounded Decimal arithmetic with source-bound parameters and explicit units.

This module never evaluates Python code. Model-supplied values and SQL aliases
are not evidence. A formula's *business meaning* still needs semantic review;
these checks establish the parameter provenance, arithmetic and dimensions.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from decimal import Decimal, DecimalException, ROUND_HALF_UP, localcontext
import re
from typing import Callable


class FormulaError(ValueError):
    """A calculation cannot be established from the supplied evidence."""


@dataclass(frozen=True)
class Unit:
    factor: Decimal
    dimensions: tuple[tuple[str, int], ...]


def _unit(factor, **dimensions):
    return Unit(Decimal(str(factor)), tuple(sorted((k, v) for k, v in dimensions.items() if v)))


def _ratio(numerator, denominator):
    with localcontext() as context:
        context.prec = 40
        context.rounding = ROUND_HALF_UP
        return Decimal(numerator) / Decimal(denominator)


# Deliberately finite. Unknown names do NOT become dimensionless quantities.
_UNITS = {
    '1': _unit(1), '%': _unit('.01'), '‰': _unit('.001'),
    '分': _unit('.01', CNY=1), '元': _unit(1, CNY=1), '万元': _unit(10000, CNY=1), '亿元': _unit(100000000, CNY=1),
    '美元': _unit(1, USD=1), '欧元': _unit(1, EUR=1),
    'm': _unit(1, length=1), 'cm': _unit('.01', length=1), 'km': _unit(1000, length=1),
    'm²': _unit(1, length=2), 'cm²': _unit('.0001', length=2), 'km²': _unit(1000000, length=2),
    '亩': _unit(_ratio(2000, 3), length=2), '公顷': _unit(10000, length=2),
    # Joule/second base uses exact integer conversion factors. A kWh base
    # required the recurring Decimal 1/3600 for power and introduced a tail
    # such as 6.000...001 into an otherwise exact (16+8) kWh / 4 h result.
    'Wh': _unit(3600, energy=1), 'kWh': _unit(3600000, energy=1), 'MWh': _unit(3600000000, energy=1),
    'W': _unit(1,energy=1,time=-1),
    'kW': _unit(1000,energy=1,time=-1),
    'MW': _unit(1000000,energy=1,time=-1),
    'Pa': _unit(1, mass=1, length=-1, time=-2),
    'kPa': _unit(1000, mass=1, length=-1, time=-2),
    'MPa': _unit(1000000, mass=1, length=-1, time=-2),
    'kg': _unit(1, mass=1), 't': _unit(1000, mass=1),
    '秒': _unit(1, time=1), '分钟': _unit(60, time=1), '小时': _unit(3600, time=1), '天': _unit(86400, time=1),
    '人': _unit(1, person=1), '件': _unit(1, item=1), '次': _unit(1, occurrence=1),
    '个': _unit(1, count=1), '单': _unit(1, order=1),
}
_ALIASES = {'': '1', '无量纲': '1', '百分比': '%', '百分数': '%', '％': '%', '人民币元': '元', '人民币分': '分', '帕':'Pa', '千帕':'kPa', '兆帕':'MPa', 'yuan': '元', 'percent': '%', 'CNY': '元', 'RMB': '元', 'USD': '美元', 'EUR': '欧元',
            '米': 'm', '厘米': 'cm', '千米': 'km', '公里': 'km', '平方米': 'm²', 'm2': 'm²', 'm^2': 'm²',
            '平方厘米': 'cm²', 'cm2': 'cm²', '平方千米': 'km²', 'km2': 'km²',
            '千瓦时': 'kWh', '度': 'kWh', '千瓦·时': 'kWh', '瓦时': 'Wh', '兆瓦时': 'MWh',
            '瓦':'W','千瓦':'kW','兆瓦':'MW',
            '千克': 'kg', '公斤': 'kg', '吨': 't', '秒钟': '秒', 'h': '小时', 's': '秒'}
_NUMBER = re.compile(r'(?<![\d.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\d.])')
MAX_EXPRESSION = 1000
MAX_NODES = 100
MAX_PARAMETERS = 20


def supported_units():
    """Public capability list; slash and '*' compose at most four listed units."""
    return {'units': list(_UNITS), 'aliases': dict(_ALIASES),
            'compound': '支持白名单单位以 * 或 / 组合（最多四项），例如 元/kWh、kWh/m²。',
            'limits': '不自动换汇；月/年不是固定时长；未知单位拒绝计算；亩换算使用40位Decimal精度。'}


def _combine(a, b, sign=1):
    dims = dict(a)
    for key, exponent in b:
        dims[key] = dims.get(key, 0) + sign * exponent
    return tuple(sorted((k, v) for k, v in dims.items() if v))


def parse_unit(name):
    if not isinstance(name, str) or len(name) > 60:
        raise FormulaError('单位必须是明确的短文本')
    name = _ALIASES.get(name.strip(), name.strip())
    if name in _UNITS:
        return name, _UNITS[name]
    pieces = re.split(r'([*/])', name)
    if len(pieces) > 7 or len(pieces) % 2 == 0:
        raise FormulaError('单位不在支持范围内：' + name)
    factor, dims, op, normalized = Decimal(1), (), '*', []
    for index, piece in enumerate(pieces):
        if index % 2:
            op = piece
            normalized.append(piece)
            continue
        atom = _ALIASES.get(piece.strip(), piece.strip())
        if atom not in _UNITS:
            raise FormulaError('单位不在支持范围内：' + name)
        current = _UNITS[atom]
        factor = factor * current.factor if op == '*' else factor / current.factor
        dims = _combine(dims, current.dimensions, 1 if op == '*' else -1)
        normalized.append(atom)
    return ''.join(normalized), Unit(factor, dims)


def _decimal(value):
    if isinstance(value, bool) or value is None or isinstance(value, (dict, list)):
        raise FormulaError('参数不是明确的数值，不能把空值或文本补为零')
    raw = str(value).strip().replace(',', '')
    if len(raw) > 100 or not re.fullmatch(r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?', raw):
        raise FormulaError('参数不是有限十进制数值')
    number = Decimal(raw)
    if not number.is_finite() or number and abs(number.adjusted()) > 60:
        raise FormulaError('数值超出可复算范围')
    return number


def _mentions_unit(text, unit_name):
    # Validate complete known unit spellings; 元 must not be inferred from 万元.
    if unit_name == '分' and re.search(r'(?:金额|货币|人民币)[^。；;\n]{0,30}(?:单位(?:为|是|[:：])\s*分|以分为单位)(?=$|[，,。；;：:\s])', text):
        return True
    spellings = {unit_name} | {alias for alias, canonical in _ALIASES.items() if canonical == unit_name and alias}
    tokens = sorted(set(_UNITS) | set(_ALIASES), key=len, reverse=True)
    tokens = [t for t in tokens if t and t != '1']
    patterns = [('(?<![\u4e00-\u9fffA-Za-z])'+re.escape(t)+'(?![\u4e00-\u9fffA-Za-z])') if t in ('单','度','分') else
                ('(?<![A-Za-z])' + re.escape(t) + '(?![A-Za-z])') if re.search('[A-Za-z]', t) else re.escape(t) for t in tokens]
    found = re.findall('|'.join(patterns), text)
    if '/' in unit_name or '*' in unit_name:
        return any(re.sub(r'\s+', '', spelling) in re.sub(r'\s+', '', text) for spelling in spellings)
    return any(token in spellings for token in found)


def _proof_unit(binding, evidence, record, name, inline_text=''):
    if name == '1':
        # A dimensionless declaration is still checked for business meaning by
        # the semantic reviewer. A visible dimensional suffix is not erased.
        if inline_text.strip() and inline_text.strip() not in ('1', '无量纲'):
            raise FormulaError('原文带有单位，不能按无量纲计算')
        return []
    if inline_text and any(_mentions_unit(inline_text, candidate) for candidate in _UNITS if candidate != '1') and not _mentions_unit(inline_text, name):
        raise FormulaError('声明单位与数值紧邻原文单位不一致')
    proof_id = binding.get('unit_evidence_id')
    proofs = []
    if proof_id:
        proof = evidence.get(proof_id)
        if not proof or proof.get('kind') != 'text' or not _mentions_unit(str(proof.get('quote', '')), name):
            raise FormulaError(f'单位依据 {proof_id} 未包含明确的 {name} 单位说明；请引用含单位的原文证据，或省略错误的unit_evidence_id使用可追溯的字段单位')
        proofs.append(proof_id)
    elif inline_text and _mentions_unit(inline_text, name):
        pass
    elif record.get('kind') == 'table':
        # Only original schema column names are eligible, never SELECT aliases.
        originals = record.get('original_columns', [])
        column = binding.get('column')
        unit_column = binding.get('unit_column')
        if unit_column is not None:
            result = record.get('query_result') or {}
            columns, rows = result.get('columns', []), result.get('rows', [])
            row = binding.get('row')
            direct = record.get('direct_columns') or {}
            if (not isinstance(unit_column, str) or columns.count(unit_column) != 1
                    or not isinstance(direct.get(unit_column),str) or not isinstance(direct.get(column),str)
                    or direct[unit_column].rsplit('.',1)[0] != direct[column].rsplit('.',1)[0]
                    or isinstance(row, bool) or not isinstance(row, int) or not 0 <= row < len(rows)):
                raise FormulaError('单位列必须是同一结果行的真实直接原字段，不能使用常量、聚合或伪造别名')
            value = rows[row][columns.index(unit_column)]
            if not isinstance(value, str) or parse_unit(value)[0] != name:
                raise FormulaError('同一结果行的单位格与声明单位不一致')
            binding['unit_proof'] = {'row': row, 'column': unit_column, 'value': value,
                                     'source_column': direct[unit_column], 'value_source_column':direct[column], 'scope': 'same_executed_result_row'}
            return proofs
        lineage = (record.get('column_units') or {}).get(column)
        if lineage:
            if lineage['unit'] != name:
                raise FormulaError(f'查询结果字段 {column} 的可追溯单位为 {lineage["unit"]}，不能改标为 {name}')
            return proofs
        if column not in originals or not _mentions_unit(str(column), name):
            raise FormulaError('SQL数值的单位需要原文单位说明或含单位的原始字段名')
    else:
        raise FormulaError('数值的单位没有明确原文依据')
    return proofs


def infer_sql_output_units(source, sql, output_columns):
    """Conservative unit-preserving SQL projection lineage; unknown stays unknown.

    Supports direct real columns, aliases, SUM/AVG/MIN/MAX, DECIMAL_SUM/AVG,
    ROUND/CAST, and addition/subtraction with exactly the same original units.
    Nested SELECTs, unions, products, ratios and guessed names are not inferred.
    """
    import sqlglot
    from sqlglot import exp
    try:
        tree = sqlglot.parse_one(sql, read='sqlite')
    except (sqlglot.errors.ParseError, TypeError):
        return {}
    if not isinstance(tree, exp.Select) or len(list(tree.find_all(exp.Select))) != 1:
        return {}
    schema = {t['name'].casefold(): {c['name'].casefold(): c['name'] for c in t.get('columns', [])} for t in source.get('tables', [])}
    tables = {}
    for table in tree.find_all(exp.Table):
        if table.name.casefold() in schema:
            tables[table.alias_or_name.casefold()] = table.name.casefold()
    def column_unit(node):
        matches = [(table, schema[table][node.name.casefold()]) for alias, table in tables.items()
                   if (not node.table or node.table.casefold() == alias) and node.name.casefold() in schema[table]]
        if len(matches) != 1:
            return None
        table, column = matches[0]
        units = [unit for unit in _UNITS if unit != '1' and _mentions_unit(column, unit)]
        if len(units) != 1:
            return None
        return units[0], [table+'.'+column]
    def trace(node):
        if isinstance(node, exp.Column):
            return column_unit(node)
        if isinstance(node, (exp.Alias, exp.Paren, exp.Cast, exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Round)):
            return trace(node.this)
        if isinstance(node, exp.Anonymous) and node.name.upper() in ('DECIMAL_SUM', 'DECIMAL_AVG') and len(node.expressions) == 1:
            return trace(node.expressions[0])
        if isinstance(node, (exp.Add, exp.Sub)):
            left, right = trace(node.this), trace(node.expression)
            if left and right and left[0] == right[0]:
                return left[0], list(dict.fromkeys(left[1]+right[1]))
        return None
    projections = tree.expressions
    if len(projections) != len(output_columns):
        return {}
    result = {}
    for name, projection in zip(output_columns, projections):
        if output_columns.count(name) != 1:
            continue
        lineage = trace(projection)
        if lineage:
            result[name] = {'unit': lineage[0], 'source_columns': lineage[1], 'method': 'supported_sql_expression'}
    return result


def infer_sql_direct_columns(source, sql, output_columns):
    """Direct non-aggregate physical projection only, for same-row unit proof."""
    import sqlglot
    from sqlglot import exp
    try: tree=sqlglot.parse_one(sql,read='sqlite')
    except (sqlglot.errors.ParseError,TypeError): return {}
    if (not isinstance(tree,exp.Select) or len(list(tree.find_all(exp.Select)))!=1
        or tree.args.get('group') or tree.args.get('distinct') or tree.args.get('joins')
        or any(isinstance(n,(exp.AggFunc,exp.Window)) for n in tree.walk())): return {}
    tables=list(tree.find_all(exp.Table))
    if len(tables)!=1:return {}
    table=tables[0]
    actual=next((t for t in source.get('tables',[]) if t['name'].casefold()==table.name.casefold()),None)
    if not actual:return {}
    columns={c['name'].casefold():c['name'] for c in actual.get('columns',[])}
    projections=tree.expressions
    if len(projections)==1 and isinstance(projections[0],exp.Star):
        return {c:actual['name']+'.'+c for c in output_columns if c.casefold() in columns}
    if len(projections)!=len(output_columns):return {}
    result={}
    for output,node in zip(output_columns,projections):
        if isinstance(node,exp.Alias):node=node.this
        if isinstance(node,exp.Column) and node.name.casefold() in columns and output_columns.count(output)==1:
            if not node.table or node.table.casefold()==table.alias_or_name.casefold():
                result[output]=actual['name']+'.'+columns[node.name.casefold()]
    return result


def _resolve_binding(name, binding, evidence, prior, user_context=None):
    if not isinstance(binding, dict) or 'value' in binding:
        raise FormulaError('参数只能绑定证据位置，不能接收模型填写的数值')
    if 'user_input_id' in binding:
        from .user_scenarios import resolve_input
        try: raw, proof = resolve_input(binding, user_context)
        except ValueError as exc: raise FormulaError(str(exc)) from exc
        unit_name, unit = parse_unit(proof['unit'])
        value = _decimal(raw)
        return value * unit.factor, unit.dimensions, {'name':name, 'value':str(value), 'unit':unit_name,
            'evidence_ids':[], 'binding':dict(binding), 'user_input':proof,
            'provenance':'user_question', 'is_hypothetical':proof.get('is_hypothetical') is True, 'is_document_evidence':False}
    unit_name, unit = parse_unit(binding.get('unit', '1'))
    if 'calculation_id' in binding:
        calculation = prior.get(binding['calculation_id'])
        if not calculation:
            raise FormulaError('依赖的计算不存在')
        actual_name, actual = parse_unit(calculation['unit'])
        if actual_name != unit_name:
            raise FormulaError('先前计算必须按其实际单位绑定；换算由输出单位声明')
        value = _decimal(calculation['value'])
        return value * actual.factor, actual.dimensions, {'name': name, 'value': str(value), 'unit': unit_name,
            'evidence_ids': list(calculation['evidence_ids']), 'binding': dict(binding),
            'is_hypothetical':bool(calculation.get('is_hypothetical'))}
    identifier = binding.get('evidence_id')
    record = evidence.get(identifier)
    if not record:
        raise FormulaError('参数引用了不存在的证据')
    if record.get('kind') in ('chart_observation','formula_parameter'):
        # These are separately reviewed transcriptions, never native text/SQL.
        # Only controller-owned evidence supplies the number and actual unit.
        if record['kind']=='chart_observation':
            item=record.get('visual_item') or {}
            if (record.get('provenance')!='separate_visual_read_and_review' or item.get('review',{}).get('supported') is not True
                or item.get('type')!='printed_value' or binding.get('visual_item_id')!=item.get('id')):
                raise FormulaError('只有绑定原项ID且已独立核验的印刷图值可计算；定性关系、参考线和精度限制不可当实测参数')
            position=item
        else:
            item=record.get('parameter') or {}
            if (record.get('provenance')!='separate_formula_visual_read_and_review' or record.get('formula_verified') is not True
                or item.get('verified') is not True or binding.get('formula_id')!=record.get('formula_id')
                or binding.get('parameter_id')!=record.get('parameter_id')
                or item.get('symbol') in record.get('formula_parameter_conflicts',[])):
                raise FormulaError('公式参数未核验、标识不符或存在冲突')
            if binding.get('symbol',name)!=item.get('symbol'):
                raise FormulaError('计算参数与原公式符号不一致；别名须显式给出原symbol')
            position=item.get('source_position') or {}
        if any(position.get(key)!=record.get(key) for key in ('source_id','sha256','parser_version','page')):
            raise FormulaError('视觉参数的来源版本或原页不一致')
        raw=str(item.get('value','')).replace('−','-').replace('％','%')
        suffix='%' if raw.endswith('%') else '‰' if raw.endswith('‰') else ''
        actual_unit=item.get('unit') or suffix
        unit_normalization = None
        if not actual_unit and record['kind'] == 'formula_parameter':
            from .formula_unit_evidence import explicit_dimensionless_parameter
            unit_normalization = explicit_dimensionless_parameter(record, evidence)
            if unit_normalization is not None:
                actual_unit = unit_normalization['normalized_unit']
        if not actual_unit:
            raise FormulaError('视觉参数缺少原件明确单位，不能默认无量纲')
        actual_name,actual=parse_unit(actual_unit)
        if actual_name!=unit_name or suffix and actual_name!=suffix:
            raise FormulaError('视觉参数声明单位与原件已核验单位不一致')
        value=_decimal(raw[:-len(suffix)] if suffix else raw)
        return value*actual.factor,actual.dimensions,{'name':name,'value':str(value),'unit':unit_name,
            'evidence_ids':list(dict.fromkeys([identifier] + ([unit_normalization['evidence_id']] if unit_normalization else []))),
            **({'unit_normalization': unit_normalization} if unit_normalization else {}),
            'binding':dict(binding),'provenance':record['provenance'],
            'source_position':{k:record.get(k) for k in ('source_id','sha256','parser_version','page')}}
    proof_text = ''
    if record.get('kind') == 'text':
        quote = binding.get('quote')
        full = str(record.get('quote', ''))
        if not isinstance(quote, str) or not quote or quote not in full or len(quote) > 160:
            raise FormulaError('参数必须是原文中的精确数值片段')
        offset = binding.get('start')
        if offset is None:
            if full.count(quote) != 1:
                raise FormulaError('数值片段在原文出现多次，请给出明确起始位置')
            offset = full.index(quote)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or full[offset:offset+len(quote)] != quote:
            raise FormulaError('数值片段位置与原文不一致')
        numbers = list(_NUMBER.finditer(quote))
        if len(numbers) != 1:
            raise FormulaError('每个参数片段必须恰好包含一个数值')
        match = numbers[0]
        # Reject selecting "5" out of "15" or "1.5".
        begin, end = offset + match.start(), offset + match.end()
        if begin and (full[begin-1].isdigit() or full[begin-1] in '.,') or end < len(full) and (full[end].isdigit() or full[end] in '.,'):
            raise FormulaError('参数截取不是完整数值')
        value = _decimal(match.group())
        proof_text = quote[match.end():].strip()
        if not proof_text:
            proof_text = re.match(r'^[^，。；;\s\n]{0,18}', full[end:].lstrip(' \t')).group()
            # Only an immediately following known unit counts, not later prose.
            if not any(proof_text.startswith(x) for x in sorted(set(_UNITS)|set(_ALIASES),key=len,reverse=True) if x and x != '1'):
                proof_text = ''
        binding = {**binding, 'start': offset, 'end': offset+len(quote)}
    elif record.get('kind') == 'table':
        result = record.get('query_result')
        if not isinstance(result, dict):
            raise FormulaError('表格参数必须绑定已执行查询的真实结果')
        if result.get('selection_required_for_scalar'):
            raise FormulaError('该查询是未筛选的多行候选，不能直接把第某行作为公式参数；请先用from_step的真实对象标识查对应映射，再用from_evidence的规则编号过滤参数表')
        row, column = binding.get('row'), binding.get('column')
        columns, rows = result.get('columns', []), result.get('rows', [])
        location_hint = f'证据 {identifier} 是SQL查询结果；请用row（0起始）和column绑定，可用列为 {columns[:20]}，共有 {len(rows)} 行'
        if row is None and column is None and 'quote' in binding:
            quote = binding['quote']
            # Compatibility is exact full-cell string equality, not numerical
            # closeness, substring search, or an inferred entity match.
            matches = [(ri, columns[ci]) for ri, values in enumerate(rows) for ci, cell in enumerate(values)
                       if ci < len(columns) and cell is not None and isinstance(quote, str) and str(cell) == quote]
            if len(matches) != 1 or columns.count(matches[0][1]) != 1:
                raise FormulaError(f'{location_hint}；quote必须唯一匹配一个完整单元格，当前匹配 {len(matches)} 格')
            row, column = matches[0]
            binding = {**binding, 'row': row, 'column': column, 'resolved_from': 'unique_exact_cell_quote'}
        if isinstance(row, bool) or not isinstance(row, int) or row < 0 or row >= len(rows) or column not in columns or columns.count(column) != 1:
            raise FormulaError(location_hint)
        value = _decimal(rows[row][columns.index(column)])
    else:
        raise FormulaError('这种证据不能作为计算参数')
    binding = dict(binding)  # Add unit proof to the resolved audit, not the model proposal.
    ids = [identifier] + list(record.get('supporting_evidence_ids', []))
    try:
        ids += _proof_unit(binding, evidence, record, unit_name, proof_text)
    except FormulaError as exc:
        raise FormulaError(f'参数 {name}（证据 {identifier}）：{exc}') from exc
    return value * unit.factor, unit.dimensions, {'name': name, 'value': str(value), 'unit': unit_name,
        'evidence_ids': list(dict.fromkeys(ids)), 'binding': binding}


def _canonical_number(value):
    with localcontext() as context:
        context.prec=100
        return str(_decimal(value).normalize())


def _check_visual_rule(tree,bindings,record,expression):
    """Compare supported arithmetic structure, never eval a transcription AST."""
    from . import formula_structure
    formula=record.get('formula') or {}
    if (record.get('provenance')!='separate_formula_visual_read_and_review'
        or formula.get('verified') is not True or formula.get('review',{}).get('supported') is not True
        or any((formula.get('source_position') or {}).get(k)!=record.get(k) for k in ('source_id','sha256','parser_version','page'))):
        raise FormulaError('公式结构未核验或来源版本不一致')
    try:
        formula_structure.validate_tree(formula['tree'])
        actual=formula_structure.computation_candidate(formula['tree'])
    except (formula_structure.StructureError,ValueError,KeyError,TypeError) as exc:
        raise FormulaError('该视觉公式结构不支持确定性复算') from exc
    if not isinstance(actual,dict):
        raise FormulaError('该视觉公式仅支持展示，尚无可执行的受限计算结构')
    if actual!=formula.get('computation_ast'):
        raise FormulaError('公式显示结构与受限计算结构不一致')
    if actual.get('type')=='equation':
        if actual['left'].get('type')!='symbol':raise FormulaError('该公式需要求解关系式，当前工具不支持')
        actual=actual['right']
    symbols={name:binding.get('symbol',name) for name,binding in bindings.items() if isinstance(binding,dict)}
    if any(not isinstance(s,str) or not s or len(s)>64 for s in symbols.values()):raise FormulaError('原公式符号映射无效')
    if len(set(symbols.values()))!=len(symbols):raise FormulaError('同一原公式符号不能映射多个参数')
    constants=set()
    def original(node):
        kind=node['type']
        if kind=='symbol':return ('symbol',node['name']+('_'+node['subscript'] if node.get('subscript') else ''))
        if kind=='number':
            number=_canonical_number(node['value']);constants.add(number);return ('number',number)
        if kind=='binary':return (node['operator'].replace('^','**'),original(node['left']),original(node['right']))
        if kind=='unary':return ('unary'+node['operator'],original(node['operand']))
        if kind=='root' and node['degree']==2:return ('sqrt',original(node['radicand']))
        raise FormulaError('该公式包含当前复算器不支持的求和、根次或条件结构')
    def submitted(node):
        if isinstance(node,ast.Name):return ('symbol',symbols.get(node.id,node.id))
        if isinstance(node,ast.Constant) and type(node.value) in (int,float):return ('number',_canonical_number(ast.get_source_segment(expression,node)))
        if isinstance(node,ast.UnaryOp) and type(node.op) in (ast.UAdd,ast.USub):return ('unary'+('+' if isinstance(node.op,ast.UAdd) else '-'),submitted(node.operand))
        if isinstance(node,ast.BinOp) and type(node.op) in (ast.Add,ast.Sub,ast.Mult,ast.Div,ast.Pow):
            return ({ast.Add:'+',ast.Sub:'-',ast.Mult:'*',ast.Div:'/',ast.Pow:'**'}[type(node.op)],submitted(node.left),submitted(node.right))
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='sqrt' and len(node.args)==1 and not node.keywords:
            return ('sqrt',submitted(node.args[0]))
        raise FormulaError('表达式不是受支持的原公式结构')
    if original(actual)!=submitted(tree.body):
        raise FormulaError('提交表达式与独立核验的原公式结构不一致，不能改分母、符号或凭常识换公式')
    return constants


def _evaluate_resolved(expression,tree,resolved,output_unit,precision,cancelled,visual_constants=()):
    with localcontext() as context:
        context.prec=40
        context.rounding=ROUND_HALF_UP
        used = set()
        def visit(node):
            if cancelled():
                raise InterruptedError('计算已取消')
            if isinstance(node, ast.Name):
                if node.id not in resolved:
                    raise FormulaError('表达式引用了未绑定参数：' + node.id)
                used.add(node.id)
                return resolved[node.id][:2]
            if (isinstance(node, ast.Constant) and type(node.value) in (int,float)
                and (type(node.value) is int and node.value in (0,1,2,100)
                     or _canonical_number(ast.get_source_segment(expression,node)) in visual_constants)):
                # Only mathematical structure constants. Business rates, fees,
                # coefficients etc. must be separate evidence-bound parameters.
                return _decimal(ast.get_source_segment(expression,node)), ()
            if isinstance(node, ast.Constant) and type(node.value) in (int,float):
                literal = ast.get_source_segment(expression,node)
                raise FormulaError(f'数值常量 {literal} 缺少来源绑定；业务系数须作为独立参数绑定原文精确数值，'
                                   '或由 formula_read 独立核验的公式结构授权。read_image 仅查看原页，不等于公式结构已核验')
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                value, dims = visit(node.operand)
                return (-value if isinstance(node.op, ast.USub) else value), dims
            if isinstance(node, ast.BinOp):
                left, ld = visit(node.left)
                right, rd = visit(node.right)
                if isinstance(node.op, (ast.Add, ast.Sub)):
                    if ld != rd:
                        raise FormulaError('加减运算的量纲不同')
                    return (left + right if isinstance(node.op, ast.Add) else left - right), ld
                if isinstance(node.op, ast.Mult):
                    return left * right, _combine(ld, rd)
                if isinstance(node.op, ast.Div):
                    if not right:
                        raise FormulaError('分母为零，不能得出计算结果')
                    return left / right, _combine(ld, rd, -1)
                if isinstance(node.op, ast.Pow):
                    if rd or right != right.to_integral_value() or abs(right) > 6:
                        raise FormulaError('幂指数必须是-6到6的无量纲整数')
                    if not left and right <= 0:
                        raise FormulaError('零的零次幂或负幂没有可用结果')
                    return left ** int(right), tuple((k, p*int(right)) for k, p in ld if p*int(right))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
                values = [visit(n) for n in node.args]
                if node.func.id == 'sqrt' and len(values) == 1:
                    value, dims = values[0]
                    if value < 0 or any(p % 2 for _, p in dims):
                        raise FormulaError('平方根需要非负数值和偶数量纲指数')
                    return value.sqrt(), tuple((k, p//2) for k, p in dims if p//2)
                if node.func.id == 'abs' and len(values) == 1:
                    return abs(values[0][0]), values[0][1]
                if node.func.id in ('min', 'max') and 2 <= len(values) <= 8:
                    if any(d != values[0][1] for _, d in values):
                        raise FormulaError('比较运算的量纲不同')
                    return (min if node.func.id == 'min' else max)(v for v, _ in values), values[0][1]
            raise FormulaError('表达式包含未允许的语法；仅支持算术、sqrt、abs、min、max')
        try:
            value, dims = visit(tree.body)
            if used != set(resolved):
                raise FormulaError('存在未参与公式的参数')
            name, output = parse_unit(output_unit)
            if dims != output.dimensions:
                raise FormulaError('计算结果与目标单位的量纲不一致')
            value = value / output.factor
            _decimal(value)
            display = format(value.quantize(Decimal(1).scaleb(-precision)), f'.{precision}f')
        except DecimalException as exc:
            raise FormulaError('计算超出可复算的数值范围') from exc
    return value,name,display


def evaluate_arithmetic(expression,parameters,output_unit='1',*,precision=2,cancelled=lambda:False,structure_constants=()):
    """Pure bounded arithmetic. This function makes no source/fact assertions.

    Callers must label hypothetical/user-supplied parameters as assumptions.
    Never use this entry point to bypass calculate_formula evidence bindings.
    """
    if not isinstance(expression,str) or not expression.strip() or len(expression)>MAX_EXPRESSION:
        raise FormulaError('表达式为空或超出长度限制')
    if not isinstance(parameters,dict) or not parameters or len(parameters)>MAX_PARAMETERS:
        raise FormulaError('算术参数格式或数量无效')
    if type(precision) is not int or not 0<=precision<=10:
        raise FormulaError('展示小数位必须为0到10')
    try:tree=ast.parse(expression,mode='eval')
    except (SyntaxError,RecursionError) as exc:raise FormulaError('表达式不是支持的算术语法') from exc
    if len(list(ast.walk(tree)))>MAX_NODES:raise FormulaError('表达式过于复杂')
    resolved={}
    for key,item in parameters.items():
        if not isinstance(key,str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,31}',key) or key in ('sqrt','abs','min','max') or not isinstance(item,dict):
            raise FormulaError('算术参数须使用合法名称、明确数值和单位')
        name,unit=parse_unit(item.get('unit'))
        with localcontext() as context:
            context.prec=40
            context.rounding=ROUND_HALF_UP
            resolved[key]=(_decimal(item.get('value'))*unit.factor,unit.dimensions)
    if not isinstance(structure_constants,(list,tuple,set)) or len(structure_constants)>MAX_NODES:
        raise FormulaError('结构常量格式无效')
    constants={_canonical_number(v) for v in structure_constants}
    value,name,display=_evaluate_resolved(expression,tree,resolved,output_unit,precision,cancelled,constants)
    return {'value':str(value),'display_value':display,'unit':name,'precision':precision,'origin':'arithmetic_only'}


def calculate_formula(expression, bindings, evidence, output_unit='1', *, precision=2,
                      rule_evidence_ids=None, prior_calculations=None, user_formula=None, operation=None, user_context=None,
                      cancelled: Callable[[], bool] = lambda: False):
    """Evaluate only source-bound arithmetic; returns JSON-serializable audit data.

    Text binding: {evidence_id, quote, unit, start?}; SQL binding:
    {evidence_id,row,column,unit,unit_evidence_id?}; previous result binding:
    {calculation_id,unit}. Evidence is supplied by trusted executed tools, not
    accepted from model payloads. `user_formula` must be the actual user question.
    """
    if cancelled():
        raise InterruptedError('计算已取消')
    if not isinstance(expression, str) or not expression.strip() or len(expression) > MAX_EXPRESSION:
        raise FormulaError('表达式为空或超出长度限制')
    if not isinstance(bindings, dict) or not bindings or len(bindings) > MAX_PARAMETERS:
        raise FormulaError('计算必须包含有限的证据参数')
    if isinstance(precision, bool) or not isinstance(precision, int) or not 0 <= precision <= 10:
        raise FormulaError('展示小数位必须为0到10')
    if rule_evidence_ids is not None and (not isinstance(rule_evidence_ids, list) or any(not isinstance(i, str) for i in rule_evidence_ids)):
        raise FormulaError('计算规则引用格式无效')
    rule_ids = list(dict.fromkeys(rule_evidence_ids or []))
    if any(not isinstance(i, str) or i not in evidence or evidence[i].get('kind') not in ('text','formula_structure') or not evidence[i].get('quote') for i in rule_ids):
        raise FormulaError('计算规则必须引用真实原文')
    normalized_expression = re.sub(r'\s+', '', expression)
    explicit_user_rule = isinstance(user_formula, str) and re.search(r'(?<![A-Za-z0-9_])' + re.escape(normalized_expression) + r'(?![A-Za-z0-9_])', re.sub(r'\s+', '', user_formula)) is not None
    from . import user_scenarios
    if user_context is None:
        user_context = user_scenarios.build_context(user_formula or '')
    explicit_user_rule = explicit_user_rule or user_scenarios.literal_arithmetic_authorized(expression, bindings, user_context)
    if not rule_ids and not explicit_user_rule and operation is None:
        raise FormulaError('缺少计算规则原文或用户明确给出的表达式')
    try:
        tree = ast.parse(expression, mode='eval')
    except (SyntaxError, RecursionError) as exc:
        raise FormulaError('表达式不是支持的算术语法') from exc
    if len(list(ast.walk(tree))) > MAX_NODES:
        raise FormulaError('表达式过于复杂')
    visual_constants=set()
    for rule_id in rule_ids:
        record=evidence[rule_id]
        if record.get('kind')=='formula_structure':
            visual_constants.update(_check_visual_rule(tree,bindings,record,expression))
    if any(not isinstance(name, str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,31}', name) or name in ('sqrt', 'abs', 'min', 'max') for name in bindings):
        raise FormulaError('参数名须为短英文标识符，不能覆盖函数名')
    with localcontext() as context:
        context.prec = 40
        context.rounding = ROUND_HALF_UP
        resolved = {name: _resolve_binding(name, binding, evidence, prior_calculations or {}, user_context) for name, binding in bindings.items()}
        parameters = [resolved[name][2] for name in bindings]
        user_inputs = [p['user_input'] for p in parameters if p.get('user_input')]
        if user_inputs:
            if any(p.get('parameter_kind') in {'quantity', 'request_quantity'} for p in user_inputs):
                if operation is not None or (not rule_ids and not explicit_user_rule) or any(p.get('parameter_kind') not in {'quantity', 'request_quantity'} for p in user_inputs):
                    raise FormulaError('用户给定量只能代入有原文依据的公式，不能混用受限百分比操作或无来源公式')
            elif not isinstance(operation,dict) or operation.get('kind') != 'percentage_adjust':
                raise FormulaError('用户百分比输入只可用于明确授权的受限百分比调整')
        from .conversion_rules import validate_direct_conversion
        try:
            validate_direct_conversion(expression, parameters, output_unit,
                                       [evidence[key]['quote'] for key in rule_ids], user_formula or '')
        except ValueError as exc:
            raise FormulaError(str(exc)) from exc
        operation_audit = None
        if operation is not None:
            from .business_operations import authorize
            try:
                if isinstance(operation,dict) and operation.get('kind') in user_scenarios.KINDS:
                    operation_audit = user_scenarios.authorize(operation, user_formula or '', tree, bindings, resolved, parse_unit(output_unit)[1].dimensions, user_context)
                else:
                    operation_audit = authorize(operation, user_formula, tree, bindings, resolved, parse_unit(output_unit)[1].dimensions)
            except ValueError as exc:
                raise FormulaError(str(exc)) from exc
        # Binding, rule/AST and unit proof above always run, including hits.
        # Only the pure arithmetic tuple is memoized, never a semantic verdict.
        from .deterministic_cache import mechanical_call
        numeric, cache_audit = mechanical_call('formula_arithmetic', {
            'policy': 'bounded-decimal40-half-up-v1', 'expression': expression,
            'bindings': bindings, 'evidence': evidence, 'prior': prior_calculations or {},
            'rules': rule_ids, 'user_formula': user_formula, 'user_context':user_context, 'operation': operation_audit, 'output_unit': output_unit,
            'precision': precision, 'visual_constants': sorted(visual_constants),
            'resolved': {key: [str(value[0]), value[1]] for key,value in resolved.items()},
            'limits': [MAX_EXPRESSION, MAX_NODES, MAX_PARAMETERS],
            'units': {key: [str(value.factor), value.dimensions] for key,value in _UNITS.items()}},
            lambda: _evaluate_resolved(expression,tree,resolved,output_unit,precision,cancelled,visual_constants),
            cancelled=cancelled)
        value,name,display=numeric
    evidence_ids = list(dict.fromkeys(rule_ids + [i for p in parameters for i in p['evidence_ids']]))
    unit_handling = {
        'evaluation_basis':'parameters_normalized_before_expression_then_converted_to_output',
        'parameter_base_values':[{'parameter':p['name'],'original_value':p['value'],'original_unit':p['unit'],
            'base_value':str(resolved[p['name']][0]),'unit_factor':str(parse_unit(p['unit'])[1].factor),
            'dimensions':list(resolved[p['name']][1])} for p in parameters],
        'output_unit':name,'output_unit_factor':str(parse_unit(name)[1].factor),
        'pure_unit_conversion':isinstance(tree.body,ast.Name),
        'semantic_status':'not_established_by_arithmetic',
        'notice':'参数先按单位归一化，表达式在基准单位上运算，再换算到output_unit；纯换算只写单个变量，不重复乘除换算系数。'}
    if len(parameters)==1 and parse_unit(parameters[0]['unit'])[1].factor != parse_unit(name)[1].factor and not isinstance(tree.body,ast.Name):
        unit_handling['review_warning']='源单位与输出单位不同，表达式还包含运算；须独立核对是否把单位换算系数重复乘除。不得只因机械执行成功就认可业务结果。'
    return {'expression': expression, 'parameters': parameters, 'value': str(value), 'unit': name,
            **({'cache': {**cache_audit, 'scope': 'pure_arithmetic_only; bindings_and_units_rechecked'}} if cache_audit['status'] != 'bypass' else {}),
            'visual_rule_structure_checked':bool(visual_constants or any(evidence[i].get('kind')=='formula_structure' for i in rule_ids)),
            'visual_rule_constants':sorted(visual_constants),
            'display_value': display, 'precision': precision, 'evidence_ids': evidence_ids, 'unit_handling':unit_handling,
            'rule_evidence_ids': rule_ids, 'rule_origin': 'user_scenario' if operation_audit and operation_audit.get('kind') in user_scenarios.KINDS else 'user_operation' if operation_audit else 'source' if rule_ids else 'user_question',
            'is_hypothetical': bool(operation_audit and operation_audit.get('is_hypothetical') or any(p.get('is_hypothetical') for p in parameters)),
            **({'operation_authorization': operation_audit} if operation_audit else {}),
            'checks': ['参数从精确原文、真实查询结果或已核对原话span的显式用户输入读取' if user_inputs else '参数从精确原文或真实查询结果读取', '单位按有限白名单校验并换算',
                       '使用40位Decimal上下文计算，展示采用ROUND_HALF_UP', '算术语法与资源上限检查通过'],
            'numeric_policy': '40位有效数字；平方根和除法为有限精度计算；展示位数不代表来源精度。'}
