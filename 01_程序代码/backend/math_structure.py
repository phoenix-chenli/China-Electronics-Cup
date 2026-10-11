"""Lossless supported Office Math structures, with explicit unsupported nodes."""
M = '{http://schemas.openxmlformats.org/officeDocument/2006/math}'


def latex(node, depth=0):
    if depth > 32:
        raise ValueError('公式层级超过限制')
    name = node.tag.removeprefix(M)
    def part(key):
        child = node.find(M+key)
        if child is None:
            raise ValueError('公式缺少 '+key)
        return latex(child, depth+1)
    if name.endswith('Pr'):
        return ''
    if name == 't':
        text = node.text or ''
        for before, after in [ ('\\', r'\backslash '), ('{', r'\{'), ('}', r'\}'), ('%', r'\%'), ('&', r'\&'), ('_', r'\_'), ('#', r'\#') ]:
            text = text.replace(before, after)
        return text
    if name == 'f':
        numerator,denominator=part('num'),part('den')
        if not numerator.strip() or not denominator.strip():raise ValueError('分子或分母为空')
        return r'\frac{'+numerator+'}{'+denominator+'}'
    if name == 'sSub':
        return '{'+part('e')+'}_{'+part('sub')+'}'
    if name == 'sSup':
        return '{'+part('e')+'}^{'+part('sup')+'}'
    if name == 'sSubSup':
        return '{'+part('e')+'}_{'+part('sub')+'}^{'+part('sup')+'}'
    def hidden(prop, key):
        flag=node.find(M+prop+'/'+M+key)
        return flag is not None and flag.get(M+'val','1') in {'1','true','on'}
    if name == 'rad':
        degree = node.find(M+'deg')
        return r'\sqrt'+('['+latex(degree, depth+1)+']' if degree is not None and list(degree) and not hidden('radPr','degHide') else '')+'{'+part('e')+'}'
    if name == 'nary':
        prop = node.find(M+'naryPr/'+M+'chr')
        symbol = prop.get(M+'val', '∫') if prop is not None else '∫'
        op = {'∑': r'\sum', '∏': r'\prod', '∫': r'\int', '⋃': r'\bigcup', '⋂': r'\bigcap'}.get(symbol)
        if op is None:
            raise ValueError('不支持的多项运算符')
        for tag, prefix in [('sub', '_'), ('sup', '^')]:
            term = node.find(M+tag)
            if term is not None and list(term) and not hidden('naryPr',tag+'Hide'):
                op += prefix+'{'+latex(term, depth+1)+'}'
        return op+' '+part('e')
    if name in {'oMath', 'oMathPara', 'r', 'num', 'den', 'e', 'sub', 'sup', 'deg'}:
        return ''.join(latex(child, depth+1) for child in node if child.tag.startswith(M))
    raise ValueError('不支持的公式节点 '+name)
