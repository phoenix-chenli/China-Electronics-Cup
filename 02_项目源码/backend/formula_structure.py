"""Source-preserving formula structures; extraction is never semantic verification.

OMML follows Office Open XML Math (ECMA-376 §22.1). The display tree and the
restricted, non-executable arithmetic AST are deliberately separate. PDF/OCR
strings are candidates, not a substitute for the missing two-dimensional tree.
"""
from __future__ import annotations

import ast
import hashlib
import re
from xml.etree import ElementTree as ET

VERSION = 'zhiheng-formula-1'
M = 'http://schemas.openxmlformats.org/officeDocument/2006/math'
MAX_FORMULAS = 500
MAX_NODES = 512
MAX_DEPTH = 32
MAX_XML = 32000
MAX_TEXT = 8000


class StructureError(ValueError):
    pass


def _local(node):
    return node.tag.rsplit('}', 1)[-1]


def _prop(node, container, name, default):
    value = node.find(f'{{{M}}}{container}/{{{M}}}{name}')
    return value.get(f'{{{M}}}val', default) if value is not None else default


def _row(items):
    return items[0] if len(items) == 1 else {'type': 'row', 'children': items}


def _parse(element):
    count, unsupported = 0, []

    def walk(node, depth=0):
        nonlocal count
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise StructureError('formula_tree_limit')
        tag = _local(node)
        def children(parent=node):
            return [walk(c, depth + 1) for c in parent
                    if c.tag.startswith('{' + M + '}') and not _local(c).endswith('Pr')]
        def arg(name, optional=False):
            found = node.find(f'{{{M}}}{name}')
            if found is None:
                if optional:
                    return None
                raise StructureError('missing_' + name)
            return walk(found, depth + 1)
        if tag == 't':
            return {'type': 'text', 'text': node.text or ''}
        if tag in ('oMath', 'oMathPara', 'r', 'e', 'num', 'den', 'sub', 'sup', 'deg', 'lim'):
            return _row(children())
        if tag == 'f':
            return {'type': 'fraction', 'numerator': arg('num'), 'denominator': arg('den'),
                    'layout': _prop(node, 'fPr', 'type', 'bar')}
        if tag in ('sSub', 'sSup', 'sSubSup', 'sPre'):
            return {'type': 'script', 'base': arg('e'), 'subscript': arg('sub', tag == 'sSup'),
                    'superscript': arg('sup', tag == 'sSub'), 'position': 'pre' if tag == 'sPre' else 'post'}
        if tag == 'rad':
            hidden = _prop(node, 'radPr', 'degHide', '0') in ('1', 'true', 'on')
            return {'type': 'root', 'radicand': arg('e'), 'degree': arg('deg', True), 'degree_hidden': hidden}
        if tag == 'd':
            value = {'type': 'delimiter', 'open': _prop(node, 'dPr', 'begChr', '('),
                    'close': _prop(node, 'dPr', 'endChr', ')'), 'separator': _prop(node, 'dPr', 'sepChr', '|'),
                    'children': [walk(c, depth + 1) for c in node.findall(f'{{{M}}}e')]}
            if value['open'] == '{' and value['close'] == '' and len(value['children']) == 1:
                matrix = value['children'][0]
                if matrix['type'] == 'matrix' and all(len(r) == 2 for r in matrix['rows']):
                    return {'type': 'cases', 'branches': [{'value': r[0], 'condition': r[1]} for r in matrix['rows']],
                            'semantic_status': 'conditions_require_review'}
            return value
        if tag == 'nary':
            return {'type': 'nary', 'operator': _prop(node, 'naryPr', 'chr', '∫'), 'body': arg('e'),
                    'lower': arg('sub', True), 'upper': arg('sup', True),
                    'lower_hidden': _prop(node, 'naryPr', 'subHide', '0') in ('1', 'true', 'on'),
                    'upper_hidden': _prop(node, 'naryPr', 'supHide', '0') in ('1', 'true', 'on')}
        if tag == 'm':
            rows = [[walk(e, depth + 1) for e in row.findall(f'{{{M}}}e')]
                    for row in node.findall(f'{{{M}}}mr')]
            if not rows or any(not r or len(r) != len(rows[0]) for r in rows):
                raise StructureError('irregular_matrix')
            return {'type': 'matrix', 'rows': rows}
        if tag == 'eqArr':
            return {'type': 'equation_array', 'rows': [walk(e, depth + 1) for e in node.findall(f'{{{M}}}e')]}
        # Decorations and unknown functions must not silently become operands.
        unsupported.append(tag)
        return {'type': 'unsupported', 'omml_tag': tag, 'children': children()}

    return walk(element), sorted(set(unsupported))


def text_of(tree):
    if not tree:
        return ''
    kind = tree['type']
    if kind == 'text':
        return tree['text']
    if kind in ('row', 'unsupported'):
        return ''.join(text_of(c) for c in tree['children'])
    if kind == 'fraction':
        return f"({text_of(tree['numerator'])})/({text_of(tree['denominator'])})"
    if kind == 'script':
        base = text_of(tree['base'])
        return base + ('_' + text_of(tree['subscript']) if tree['subscript'] else '') + ('^(' + text_of(tree['superscript']) + ')' if tree['superscript'] else '')
    if kind == 'root':
        return 'root(' + text_of(tree['radicand']) + ')'
    if kind == 'delimiter':
        return tree['open'] + tree['separator'].join(text_of(c) for c in tree['children']) + tree['close']
    return ''


def _latex_text(text):
    translations = {'\\': r'\backslash ', '{': r'\{', '}': r'\}', '%': r'\%', '#': r'\#',
                    '&': r'\&', '_': r'\_', '$': r'\$', '^': r'\^{}', '~': r'\~{}',
                    '×': r'\times ', '÷': r'\div ', '≤': r'\le ', '≥': r'\ge ', '≠': r'\ne ', '∑': r'\sum '}
    return ''.join(translations.get(c, c) for c in text)


def latex(tree):
    kind = tree['type']
    f = latex
    if kind == 'text':
        return _latex_text(tree['text'])
    if kind == 'row':
        return ''.join(f(c) for c in tree['children'])
    if kind == 'fraction':
        if tree['layout'] == 'noBar':
            return r'\genfrac{}{}{0pt}{}{' + f(tree['numerator']) + '}{' + f(tree['denominator']) + '}'
        return r'\frac{' + f(tree['numerator']) + '}{' + f(tree['denominator']) + '}'
    if kind == 'script':
        scripts = ('_{' + f(tree['subscript']) + '}' if tree['subscript'] else '') + ('^{' + f(tree['superscript']) + '}' if tree['superscript'] else '')
        return ('{}' + scripts + '{' + f(tree['base']) + '}') if tree['position'] == 'pre' else '{' + f(tree['base']) + '}' + scripts
    if kind == 'root':
        degree = '' if tree['degree_hidden'] or not text_of(tree['degree']) else '[' + f(tree['degree']) + ']'
        return r'\sqrt' + degree + '{' + f(tree['radicand']) + '}'
    if kind == 'delimiter':
        return r'\left' + (_latex_text(tree['open']) or '.') + tree['separator'].join(f(c) for c in tree['children']) + r'\right' + (_latex_text(tree['close']) or '.')
    if kind == 'nary':
        op = {'∑': r'\sum', '∏': r'\prod', '∫': r'\int'}.get(tree['operator'], _latex_text(tree['operator']))
        return op + ('_{' + f(tree['lower']) + '}' if tree['lower'] and not tree['lower_hidden'] else '') + ('^{' + f(tree['upper']) + '}' if tree['upper'] and not tree['upper_hidden'] else '') + ' ' + f(tree['body'])
    if kind == 'matrix':
        return r'\begin{matrix}' + r'\\'.join('&'.join(f(c) for c in row) for row in tree['rows']) + r'\end{matrix}'
    if kind == 'equation_array':
        return r'\begin{gathered}' + r'\\'.join(f(c) for c in tree['rows']) + r'\end{gathered}'
    if kind == 'cases':
        return r'\begin{cases}' + r'\\'.join(f(b['value']) + '&' + f(b['condition']) for b in tree['branches']) + r'\end{cases}'
    return r'\text{[unsupported OMML]}'


def mathml(tree):
    def make(tag, children=(), text=None, **attrs):
        e = ET.Element(tag, attrs)
        e.text = text
        e.extend(children)
        return e
    def walk(n):
        k = n['type']
        if k == 'text':
            return make('mtext', text=n['text'])
        if k == 'row':
            return make('mrow', [walk(c) for c in n['children']])
        if k == 'fraction':
            return make('mfrac', [walk(n['numerator']), walk(n['denominator'])], **({'linethickness': '0'} if n['layout'] == 'noBar' else {}))
        if k == 'script':
            if n['position'] == 'pre':
                return make('mmultiscripts', [walk(n['base']), make('mprescripts'), walk(n['subscript']) if n['subscript'] else make('none'), walk(n['superscript']) if n['superscript'] else make('none')])
            args = [walk(n['base'])] + [walk(n[v]) for v in ('subscript', 'superscript') if n[v]]
            return make('msubsup' if n['subscript'] and n['superscript'] else 'msub' if n['subscript'] else 'msup', args)
        if k == 'root':
            return make('msqrt', [walk(n['radicand'])]) if n['degree_hidden'] or not text_of(n['degree']) else make('mroot', [walk(n['radicand']), walk(n['degree'])])
        if k == 'delimiter':
            args = [make('mo', text=n['open'])]
            for i, c in enumerate(n['children']):
                if i:
                    args.append(make('mo', text=n['separator']))
                args.append(walk(c))
            return make('mrow', args + [make('mo', text=n['close'])])
        if k == 'nary':
            return make('mrow', [make('munderover', [make('mo', text=n['operator']), walk(n['lower']) if n['lower'] and not n['lower_hidden'] else make('mrow'), walk(n['upper']) if n['upper'] and not n['upper_hidden'] else make('mrow')]), walk(n['body'])])
        if k in ('matrix', 'equation_array'):
            rows = n['rows'] if k == 'matrix' else [[c] for c in n['rows']]
            return make('mtable', [make('mtr', [make('mtd', [walk(c)]) for c in row]) for row in rows])
        if k == 'cases':
            return make('mrow', [make('mo', text='{'), make('mtable', [make('mtr', [make('mtd', [walk(b[v])]) for v in ('value', 'condition')]) for b in n['branches']])])
        return make('merror', [make('mtext', text='Unsupported OMML: ' + n.get('omml_tag', '?'))])
    return ET.tostring(make('math', [walk(tree)], xmlns='http://www.w3.org/1998/Math/MathML'), encoding='unicode')


def _arithmetic(tree):
    """Translate a strict subset, never eval, never infer multiplication or cases."""
    atoms = {}
    def expr(n):
        k = n['type']
        if k == 'text':
            if 'OMMLATOM' in n['text']:
                raise StructureError('reserved_symbol')
            if not re.fullmatch(r'[\w\s.+*/^()−×÷-]*', n['text'], re.UNICODE):
                raise StructureError('ambiguous_operator')
            return n['text'].replace('−', '-').replace('×', '*').replace('÷', '/').replace('^', '**')
        if k == 'row':
            return ''.join(expr(c) for c in n['children'])
        if k == 'fraction':
            if n['layout'] not in ('bar', 'skw', 'lin'):
                raise StructureError('fraction_without_division_bar')
            return '(' + expr(n['numerator']) + ')/(' + expr(n['denominator']) + ')'
        if k == 'script':
            if n['position'] != 'post':
                raise StructureError('prescript_not_arithmetic')
            base = expr(n['base'])
            if n['subscript'] is not None:
                sub = text_of(n['subscript'])
                if not re.fullmatch(r'[^\W\d]\w*', base, re.UNICODE) or not re.fullmatch(r'\w+', sub, re.UNICODE):
                    raise StructureError('ambiguous_index')
                key = f'OMMLATOM{len(atoms)}'
                atoms[key] = {'type': 'symbol', 'name': base, 'subscript': sub}
                base = key
            return '(' + base + ')**(' + expr(n['superscript']) + ')' if n['superscript'] is not None else base
        if k == 'delimiter' and len(n['children']) == 1 and (n['open'], n['close']) in (('(', ')'), ('[', ']')):
            return '(' + expr(n['children'][0]) + ')'
        if k == 'root':
            degree = '2' if n['degree_hidden'] or not text_of(n['degree']) else text_of(n['degree'])
            if not re.fullmatch('[2-9]', degree):
                raise StructureError('root_degree_not_supported')
            key = f'OMMLATOM{len(atoms)}'
            atoms[key] = {'type': 'root', 'degree': int(degree), 'radicand': _arithmetic(n['radicand'])}
            return key
        if k == 'nary' and n['operator'] == '∑' and not n['lower_hidden'] and not n['upper_hidden']:
            lower = text_of(n['lower'])
            match = re.fullmatch(r'\s*([A-Za-zΑ-ω])\s*=\s*([-+]?\d{1,6})\s*', lower)
            if not match or n['upper'] is None:
                raise StructureError('sum_bounds_not_explicit')
            upper = _arithmetic(n['upper'])
            if upper['type'] not in ('number', 'symbol') or upper['type'] == 'number' and not re.fullmatch(r'\d{1,6}', upper['value']):
                raise StructureError('sum_bounds_not_supported')
            key = f'OMMLATOM{len(atoms)}'
            atoms[key] = {'type': 'finite_sum', 'index': match[1], 'lower': {'type': 'number', 'value': match[2]},
                          'upper': upper, 'body': _arithmetic(n['body']), 'execution_term_limit': 10000}
            return key
        raise StructureError('structure_not_computable')
    expression = expr(tree).strip()
    if not expression or len(expression) > 2000:
        raise StructureError('empty_or_long_expression')
    try:
        parsed = ast.parse(expression, mode='eval')
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise StructureError('ambiguous_arithmetic') from exc
    if sum(1 for _ in ast.walk(parsed)) > MAX_NODES:
        raise StructureError('arithmetic_limit')
    def convert(n):
        if isinstance(n, ast.Name):
            if n.id.startswith('OMMLATOM') and n.id not in atoms:
                raise StructureError('reserved_symbol')
            return atoms.get(n.id, {'type': 'symbol', 'name': n.id})
        if isinstance(n, ast.Constant) and type(n.value) in (int, float):
            raw = ast.get_source_segment(expression, n)
            if not re.fullmatch(r'\d+(?:\.\d*)?|\.\d+', raw or '') or len(raw) > 80:
                raise StructureError('number_not_supported')
            return {'type': 'number', 'value': raw}
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
            return {'type': 'unary', 'operator': '+' if isinstance(n.op, ast.UAdd) else '-', 'operand': convert(n.operand)}
        if isinstance(n, ast.BinOp) and type(n.op) in (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow):
            return {'type': 'binary', 'operator': {ast.Add: '+', ast.Sub: '-', ast.Mult: '*', ast.Div: '/', ast.Pow: '^'}[type(n.op)], 'left': convert(n.left), 'right': convert(n.right)}
        raise StructureError('arithmetic_not_allowed')
    return convert(parsed.body)


def computation_candidate(tree):
    # Equality at the outer row is structural; inequalities and case conditions
    # remain display-only rather than being coerced into an executable formula.
    parts = []
    for item in tree.get('children', []) if tree['type'] == 'row' else [tree]:
        if item['type'] == 'text' and '=' in item['text']:
            pieces = item['text'].split('=')
            for index, piece in enumerate(pieces):
                if index:
                    parts.append(None)
                if piece:
                    parts.append({'type': 'text', 'text': piece})
        else:
            parts.append(item)
    splits = [i for i, p in enumerate(parts) if p is None]
    if len(splits) == 1 and 0 < splits[0] < len(parts) - 1:
        point = splits[0]
        return {'type': 'equation', 'left': _arithmetic(_row(parts[:point])), 'right': _arithmetic(_row(parts[point + 1:]))}
    if splits:
        raise StructureError('ambiguous_equation')
    return _arithmetic(tree)


def validate_tree(tree):
    """Validate model-proposed display trees without executing or repairing them."""
    count = 0
    def walk(node, depth=0):
        nonlocal count
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH or not isinstance(node, dict):
            raise StructureError('formula_tree_limit_or_shape')
        kind = node.get('type')
        fields = {
            'text': {'text'}, 'row': {'children'}, 'fraction': {'numerator', 'denominator', 'layout'},
            'script': {'base', 'subscript', 'superscript', 'position'},
            'root': {'radicand', 'degree', 'degree_hidden'}, 'delimiter': {'open', 'close', 'separator', 'children'},
            'nary': {'operator', 'body', 'lower', 'upper', 'lower_hidden', 'upper_hidden'},
            'matrix': {'rows'}, 'equation_array': {'rows'}, 'cases': {'branches', 'semantic_status'},
        }
        if not isinstance(kind, str) or kind not in fields or set(node) - {'type'} != fields[kind]:
            raise StructureError('unknown_or_missing_tree_fields')
        result = {'type': kind}
        def child(value, optional=False):
            return None if optional and value is None else walk(value, depth + 1)
        def children(values, maximum=64):
            if not isinstance(values, list) or not 1 <= len(values) <= maximum:
                raise StructureError('formula_children_limit')
            return [child(v) for v in values]
        if kind == 'text':
            value = node['text']
            if not isinstance(value, str) or len(value) > 500 or any(ord(c) < 32 and c not in '\n\t' for c in value):
                raise StructureError('invalid_formula_text')
            result['text'] = value
        elif kind == 'row':
            result['children'] = children(node['children'])
        elif kind == 'fraction':
            if node['layout'] not in ('bar', 'skw', 'lin', 'noBar'):
                raise StructureError('unknown_fraction_layout')
            result.update(numerator=child(node['numerator']), denominator=child(node['denominator']), layout=node['layout'])
        elif kind == 'script':
            if node['position'] not in ('post', 'pre') or node['subscript'] is None and node['superscript'] is None:
                raise StructureError('invalid_script')
            result.update(base=child(node['base']), subscript=child(node['subscript'], True), superscript=child(node['superscript'], True), position=node['position'])
        elif kind == 'root':
            if type(node['degree_hidden']) is not bool:
                raise StructureError('invalid_root_visibility')
            result.update(radicand=child(node['radicand']), degree=child(node['degree'], True), degree_hidden=node['degree_hidden'])
        elif kind == 'delimiter':
            if node['open'] not in ('(', '[', '{', '|', '') or node['close'] not in (')', ']', '}', '|', '') or node['separator'] not in ('|', ',', ';', ''):
                raise StructureError('invalid_delimiter')
            result.update(open=node['open'], close=node['close'], separator=node['separator'], children=children(node['children']))
        elif kind == 'nary':
            if node['operator'] not in ('∑', '∏', '∫') or any(type(node[k]) is not bool for k in ('lower_hidden', 'upper_hidden')):
                raise StructureError('invalid_nary_operator')
            result.update(operator=node['operator'], body=child(node['body']), lower=child(node['lower'], True), upper=child(node['upper'], True),
                          lower_hidden=node['lower_hidden'], upper_hidden=node['upper_hidden'])
        elif kind == 'matrix':
            if not isinstance(node['rows'], list) or not 1 <= len(node['rows']) <= 16:
                raise StructureError('matrix_limit')
            result['rows'] = [children(row, 8) for row in node['rows']]
            if len({len(row) for row in result['rows']}) != 1:
                raise StructureError('irregular_matrix')
        elif kind == 'equation_array':
            result['rows'] = children(node['rows'], 16)
        elif kind == 'cases':
            branches = node['branches']
            if node['semantic_status'] != 'conditions_require_review' or not isinstance(branches, list) or not 1 <= len(branches) <= 16:
                raise StructureError('invalid_cases')
            result.update(branches=[], semantic_status='conditions_require_review')
            for branch in branches:
                if not isinstance(branch, dict) or set(branch) != {'value', 'condition'}:
                    raise StructureError('invalid_case_branch')
                result['branches'].append({k: child(v) for k, v in branch.items()})
        return result
    return walk(tree)


def from_omml(element, *, identifier, location):
    # Bound traversal before ElementTree's recursive serializer is entered.
    pending, visited = [(element, 0)], 0
    while pending:
        node, depth = pending.pop()
        visited += 1
        if depth > MAX_DEPTH or visited > MAX_NODES * 2:
            return {'id': identifier, 'schema_version': VERSION, 'origin': 'docx_omml',
                    'source_position': dict(location), 'structure_status': 'unknown', 'verified': False,
                    'requires_review': True, 'execution_eligible': False, 'tree': None, 'computation_ast': None,
                    'computation_status': 'unavailable', 'xml_truncated': True, 'issues': ['formula_tree_limit']}
        pending.extend((child, depth + 1) for child in node)
    raw = ET.tostring(element, encoding='unicode')
    record = {'id': identifier, 'schema_version': VERSION, 'origin': 'docx_omml',
              'source_position': dict(location), 'structure_status': 'unknown', 'verified': False,
              'requires_review': True, 'execution_eligible': False, 'tree': None, 'computation_ast': None,
              'computation_status': 'unavailable',
              'xml_sha256': hashlib.sha256(raw.encode()).hexdigest(), 'issues': []}
    if len(raw) > MAX_XML:
        record.update(xml_excerpt=raw[:1000], xml_truncated=True, issues=['formula_xml_limit'])
        return record
    record.update(original_xml=raw, xml_truncated=False)
    try:
        tree, unsupported = _parse(element)
        if sum(len(n.text or '') for n in element.iter() if _local(n) == 't') > MAX_TEXT:
            raise StructureError('formula_text_limit')
        record.update(tree=tree, latex=latex(tree), mathml=mathml(tree),
                      structure_status='partial' if unsupported else 'parsed', unsupported_tags=unsupported)
        if unsupported:
            record['issues'].append('unsupported_omml_structure')
        else:
            try:
                record['computation_ast'] = computation_candidate(tree)
            except StructureError as exc:
                record['issues'].append(str(exc))
    except (StructureError, RecursionError) as exc:
        record['issues'].append(str(exc) or 'formula_depth_limit')
    record['computation_status'] = 'unverified_candidate' if record['computation_ast'] else 'unavailable'
    return record


def from_text(text, *, identifier, location, origin):
    return {'id': identifier, 'schema_version': VERSION, 'origin': origin, 'text': text[:MAX_TEXT],
            'source_position': dict(location), 'structure_status': 'unknown', 'tree': None,
            'computation_ast': None, 'computation_status': 'unavailable', 'verified': False,
            'requires_review': True, 'execution_eligible': False, 'truncated': len(text) > MAX_TEXT,
            'issues': ['two_dimensional_formula_structure_not_verified']}


_UNIT = r'(?:%|‰|亿元|万元|元|kWh|MWh|Wh|km²|m²|cm²|mg/L|kg|km|cm|mm|m|t|人|件|次|小时|分钟|秒|天)'
_PARAMETER = re.compile(r'(?<![\w])(?P<symbol>[A-Za-zΑ-ω][A-Za-z0-9_Α-ω]{0,30})\s*[=＝]\s*(?P<value>[-+−]?(?:\d+(?:\.\d+)?|\.\d+))(?P<gap>\s*)(?P<unit>' + _UNIT + r'(?:\s*[/*]\s*' + _UNIT + r'){0,3})?(?![\w./*])')


def parameter_candidates(formula, pages):
    """Explicit nearby assignments only; no default units or numeric correction."""
    position = formula.get('source_position', {})
    current = next((p for p in pages if p.get('number') == position.get('page')), None)
    if not current:
        return []
    start, end = position.get('start', 0), position.get('end', 0)
    result = []
    for block in current.get('blocks', []):
        if block.get('text', '').startswith('[公式结构待核对]') or abs(block.get('start', 0) - start) > 1600:
            continue
        for match in _PARAMETER.finditer(block.get('text', '')):
            unit = match.group('unit')
            a = block['start'] + match.start()
            b = block['start'] + match.end()
            quote = current['text'][a:b]
            result.append({'symbol': match.group('symbol'), 'value_text': match.group('value'), 'unit_text': unit,
                           'binding_status': 'candidate_requires_review', 'unit_status': 'explicit' if unit else 'unknown',
                           'evidence': {'page': current['number'], 'page_unit': position.get('page_unit'), 'start': a, 'end': b, 'quote': quote},
                           'period': None, 'scope': None, 'rounding': None})
            if len(result) >= 32:
                break
        if len(result) >= 32:
            break
    for candidate in result:
        variants = {(c['value_text'], c['unit_text']) for c in result if c['symbol'] == candidate['symbol']}
        candidate['conflict'] = len(variants) > 1
    return result
