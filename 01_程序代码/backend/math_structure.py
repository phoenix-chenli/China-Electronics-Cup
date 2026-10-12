"""Lossless supported Office Math structures, with explicit unsupported nodes."""
M = '{http://schemas.openxmlformats.org/officeDocument/2006/math}'


def latex(node, depth=0):
    if depth==0:audit(node)
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


def audit(root):
    """Every supported operand must be visited; no unknown child is flattened."""
    import hashlib
    from collections import Counter
    from xml.etree import ElementTree as ET
    expressions={'r','f','sSub','sSup','sSubSup','rad','nary','d','func','limLow','limUpp','bar','acc','m','eqArr'}
    containers={'oMath','oMathPara','num','den','e','sub','sup','deg','fName','lim'}
    operands={'f':{'num','den'},'sSub':{'e','sub'},'sSup':{'e','sup'},'sSubSup':{'e','sub','sup'},
              'rad':{'deg','e'},'nary':{'sub','sup','e'},'d':{'e'},'func':{'fName','e'},
              'limLow':{'e','lim'},'limUpp':{'e','lim'},'bar':{'e'},'acc':{'e'},'m':{'mr'},'mr':{'e'},'eqArr':{'e'},'r':{'t'}}
    counts=Counter();stack=[(root,0)];total=0
    while stack:
        node,depth=stack.pop();total+=1
        if depth>32 or total>4096:raise ValueError('公式结构超过层级或节点限制')
        stack.extend((child,depth+1) for child in node)
    stack=[root]
    while stack:
        node=stack.pop()
        if not node.tag.startswith(M):raise ValueError('公式正文含未知命名空间')
        name=node.tag[len(M):];counts[name]+=1
        if name not in expressions|containers|{'mr','t'}:raise ValueError('不支持的公式节点 '+name)
        if name=='t':
            if list(node):raise ValueError('公式文字节点含嵌套结构')
            continue
        permitted={'oMath'} if name=='oMathPara' else expressions if name in containers else operands.get(name,set())
        seen=set()
        repeatable=permitted if name in containers else {'d':{'e'},'m':{'mr'},'mr':{'e'},'eqArr':{'e'},'r':{'t'}}.get(name,set())
        for child in node:
            cname=child.tag.removeprefix(M)
            if cname in seen and cname not in repeatable:raise ValueError('公式 '+name+' 有重复操作数 '+cname)
            seen.add(cname)
            if child.tag==M+name+'Pr' or (name=='r' and child.tag=='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rPr'):
                if any(desc.tag==M+'t' for desc in child.iter()):raise ValueError('公式样式属性含正文，不能忽略')
                continue
            if not child.tag.startswith(M) or cname not in permitted:
                raise ValueError('公式 '+name+' 含未处理子节点 '+cname)
            stack.append(child)
    return {'validation':'passed','node_count':total,'structure_counts':dict(sorted(counts.items())),
            'source_xml_sha256':hashlib.sha256(ET.tostring(root,encoding='utf-8')).hexdigest()}
