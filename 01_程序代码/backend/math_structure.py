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
    def prop(group,key,default):
        item=node.find(M+group+'/'+M+key)
        return item.get(M+'val',default) if item is not None else default
    if name == 'd':
        left,right=prop('dPr','begChr','('),prop('dPr','endChr',')')
        delimiters={'(':'(',')':')','[':'[',']':']','{':r'\{','}':r'\}','|':r'\vert','‖':r'\Vert','⟨':r'\langle','⟩':r'\rangle','⌊':r'\lfloor','⌋':r'\rfloor','⌈':r'\lceil','⌉':r'\rceil','':'.'}
        if left not in delimiters or right not in delimiters:raise ValueError('不支持的公式括号')
        expressions=node.findall(M+'e')
        if not expressions:raise ValueError('括号公式缺少内容')
        separator=prop('dPr','sepChr','|')
        if separator not in {'|',',',';',''}:raise ValueError('不支持的公式分隔符')
        return r'\left'+delimiters[left]+separator.join(latex(e,depth+1) for e in expressions)+r'\right'+delimiters[right]
    if name == 'func':
        function_name=part('fName');argument=part('e')
        if not function_name.strip() or not argument.strip():raise ValueError('函数名称或参数为空')
        return r'\operatorname{'+function_name+'} '+argument
    if name in {'limLow','limUpp'}:
        return '{'+part('e')+'}'+('_{' if name=='limLow' else '^{')+part('lim')+'}'
    if name == 'bar':
        position=prop('barPr','pos','top')
        if position not in {'top','bot'}:raise ValueError('不支持的上下横线位置')
        return (r'\overline' if position=='top' else r'\underline')+'{'+part('e')+'}'
    if name == 'acc':
        symbol=prop('accPr','chr','̂')
        command={'̂':r'\hat','̅':r'\overline','⃗':r'\vec','̃':r'\tilde','̇':r'\dot','̈':r'\ddot'}.get(symbol)
        if command is None:raise ValueError('不支持的公式重音')
        return command+'{'+part('e')+'}'
    if name == 'm':
        rows=node.findall(M+'mr');widths={len(row.findall(M+'e')) for row in rows}
        if not rows or len(widths)!=1 or 0 in widths:raise ValueError('矩阵行列不完整')
        return r'\begin{matrix}'+r'\\'.join('&'.join(latex(e,depth+1) for e in row.findall(M+'e')) for row in rows)+r'\end{matrix}'
    if name == 'eqArr':
        if not node.findall(M+'e'):raise ValueError('多行公式为空')
        return r'\begin{gathered}'+r'\\'.join(latex(e,depth+1) for e in node.findall(M+'e'))+r'\end{gathered}'
    if name == 'f':
        fraction_type=prop('fPr','type','bar')
        if fraction_type not in {'bar','lin','skw','noBar'}:raise ValueError('不支持的分数类型')

        numerator,denominator=part('num'),part('den')
        if not numerator.strip() or not denominator.strip():raise ValueError('分子或分母为空')
        if fraction_type=='noBar':return r'\genfrac{}{}{0pt}{}{'+numerator+'}{'+denominator+'}'
        if fraction_type in {'lin','skw'}:return '{'+numerator+'}/{'+denominator+'}'
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
    if name in {'oMath', 'oMathPara', 'r', 'num', 'den', 'e', 'sub', 'sup', 'deg','fName','lim'}:
        return ''.join(latex(child, depth+1) for child in node if child.tag.startswith(M))
    raise ValueError('不支持的公式节点 '+name)
