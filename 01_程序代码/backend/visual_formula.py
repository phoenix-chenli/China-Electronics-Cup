"""Bounded visual math candidates; valid structure never certifies accuracy."""
import hashlib
import copy
import json
import re


def geometry_hints(raw):
    """Observed ink positions only: no inferred character or certified math."""
    try:
        import io
        import cv2
        import numpy as np
        from PIL import Image
    except ImportError:
        return {'available':False,'reason':'local_component_dependency_unavailable'}
    with Image.open(io.BytesIO(raw)) as image:gray=np.asarray(image.convert('L'))
    height,width=gray.shape
    _,_,stats,_=cv2.connectedComponentsWithStats((gray<160).astype('uint8'),8)
    components=[tuple(int(v) for v in s[:4]) for s in stats[1:] if s[4]>=5 and s[3]>=3]
    if not components:return {'available':True,'pairs':[]}
    dominant=max(components,key=lambda b:b[3]);x,y,w,h=dominant
    lower=[list(b) for b in components if b[0]+b[2]/2>=x-.2*w and b[0]+b[2]/2<=x+1.2*w
           and b[1]>=y+h-2 and b[1]+b[3]<=min(height,y+2*h)]
    # Join dots to stems only with substantial x overlap; nearby letters stay apart.
    grouped=[]
    for box in sorted(lower,key=lambda b:(b[0],b[1])):
        for prior in grouped:
            overlap=max(0,min(box[0]+box[2],prior[0]+prior[2])-max(box[0],prior[0]))
            gap=max(box[1],prior[1])-min(box[1]+box[3],prior[1]+prior[3])
            if overlap>=.6*min(box[2],prior[2]) and gap<max(box[3],prior[3]):
                left=min(box[0],prior[0]);top=min(box[1],prior[1]);right=max(box[0]+box[2],prior[0]+prior[2]);bottom=max(box[1]+box[3],prior[1]+prior[3])
                prior[:]=[left,top,right-left,bottom-top];break
        else:grouped.append(box)
    grouped.sort(key=lambda b:b[0]);pairs=[]
    def normalized(b):return [round(b[0]/width,4),round(b[1]/height,4),round((b[0]+b[2])/width,4),round((b[1]+b[3])/height,4)]
    for base,small in zip(grouped,grouped[1:]):
        if small[3]<.8*base[3] and small[0]-(base[0]+base[2])<base[3]:
            raised=small[1]<base[1]+.15*base[3] and small[1]+small[3]<base[1]+.85*base[3]
            lowered=small[1]>base[1]+.45*base[3]
            if raised or lowered:pairs.append({'left_ink_box':normalized(base),'small_right_ink_box':normalized(small),
                                               'observed_position':'raised' if raised else 'lowered'})
    return {'available':True,'coordinate_space':'normalized','dominant_ink_box':normalized(dominant),
            'pairs':pairs[:6],'scope':'connected ink positions below tallest component; not character identity or proof'}


def audit_geometry(tree,hints):
    """A layout contradiction creates uncertainty; never auto-fixes a symbol."""
    result=copy.deepcopy(tree);operators=[]
    def walk(node,fn):
        if isinstance(node,dict):
            fn(node)
            for value in list(node.values()):walk(value,fn)
        elif isinstance(node,list):
            for value in node:walk(value,fn)
    walk(result,lambda node:operators.append(node) if node.get('kind')=='large_operator' else None)
    pairs=hints.get('pairs',[]);conflicts=[]
    if len(operators)==1 and len(pairs)==1:
        scripts=[]
        walk(operators[0].get('lower'),lambda node:scripts.append(node) if node.get('kind')=='scripts' else None)
        if len(scripts)==1:
            node=scripts[0];position=pairs[0]['observed_position']
            mismatch=position=='raised' and node.get('sub') is not None and node.get('sup') is None
            mismatch|=position=='lowered' and node.get('sup') is not None and node.get('sub') is None
            if mismatch:
                conflicts.append({'scope':'single_script_in_single_operator_lower_bound','candidate':copy.deepcopy(node),
                                  'observed_ink_pair':pairs[0],'action':'unknown_not_auto_corrected'})
                node.clear();node.update(kind='unknown',reason='局部像素的上移/下移位置与模型上下标结构冲突，字符和位置关联须对照原图核验')
    return result,{'conflicts':conflicts,'scope':'layout contradiction screening; not recognized character proof'}

VERSION = 'visual-math-tree-v1'
SYMBOLS = {'α': r'\alpha', 'β': r'\beta', 'γ': r'\gamma', 'δ': r'\delta',
           'θ': r'\theta', 'λ': r'\lambda', 'μ': r'\mu', 'σ': r'\sigma',
           'π': r'\pi', '∞': r'\infty', '≤': r'\le', '≥': r'\ge',
           '≠': r'\ne', '×': r'\times', '÷': r'\div', '−': '-',
           '∑': r'\sum', '∏': r'\prod', '∫': r'\int'}
SYMBOLS.update(dict(zip('εζηκνξρτφχψωΓΔΘΛΞΠΣΦΨΩ',
    (r'\epsilon',r'\zeta',r'\eta',r'\kappa',r'\nu',r'\xi',r'\rho',r'\tau',r'\phi',r'\chi',r'\psi',r'\omega',
     r'\Gamma',r'\Delta',r'\Theta',r'\Lambda',r'\Xi',r'\Pi',r'\Sigma',r'\Phi',r'\Psi',r'\Omega'))))
SYMBOLS.update({'±':r'\pm','∓':r'\mp','·':r'\cdot','⋅':r'\cdot','≈':r'\approx','∂':r'\partial','∇':r'\nabla',
                '∈':r'\in','∉':r'\notin','→':r'\to','∀':r'\forall','∃':r'\exists'})


def schema():
    return {'coordinate_space':'normalized_1000','box': [100,100,900,900], 'tree': {'kind':'fraction',
        'numerator':{'kind':'token','value':'a'},'denominator':{'kind':'token','value':'b'}}}


def instructions():
    return ('For formulas only, set coordinate_space="normalized_1000" and box=[x0,y0,x1,y1], '
            'with numbers in 0..1000 relative to image width/height. Never pixel coordinates or a string. '
            'Other page objects still use normalized 0..1. Use actual image coordinates, not example coordinates. '
            'The tree example only demonstrates types. Allowed node objects and exact keys: '
            'token(kind,value); row(kind,children); fraction(kind,numerator,denominator); '
            'scripts(kind,base,sub,sup); root(kind,body,index); large_operator(kind,symbol,lower,upper,body); '
            'matrix(kind,rows); unknown(kind,reason). kind is the node name. '
            'children is an array of nodes, rows is a rectangular array of node arrays. '
            'sub,sup,index,lower,upper are nodes or null. Use null only when visibly absent; '
            'use unknown nodes for unreadable or cropped content. A fraction denominator can NEVER be null; '
            'a missing denominator must be {"kind":"unknown","reason":"cropped or unreadable denominator"}. symbol is ∑,∏,or ∫. '
            'All operands are nodes; token.value contains printed characters, never LaTeX commands. '
            'For every scripts node, inspect the small character relative to its OWN base character baseline: '
            'above is sup, below is sub. A large operator lower bound may itself contain a superscript; '
            'do not change that superscript to sub merely because the entire bound is below the operator. '
            'Do not normalize unusual printed notation to familiar mathematical notation. '
            'Preserve printed parentheses as token nodes in rows. Equation numbers are not part of the expression. '
            'provenance must be an object, never an array.')


def parse(tree):
    count = 0; unknown = []; kinds = {}
    def visit(node, depth=0, path='root'):
        nonlocal count
        if node is None:
            node={'kind':'unknown','reason':'模型未提供必要公式节点；不能据此恢复缺失内容'}
        count += 1
        if count > 256 or depth > 20 or not isinstance(node, dict):
            raise ValueError('公式结构过深、超限或节点类型无效')
        kind = node.get('kind')
        if not isinstance(kind,str):raise ValueError('公式节点类型无效')
        kinds[kind] = kinds.get(kind, 0)+1
        fields = {'token': {'value'}, 'row': {'children'}, 'fraction': {'numerator','denominator'},
                  'scripts': {'base','sub','sup'}, 'root': {'body','index'},
                  'large_operator': {'symbol','lower','upper','body'}, 'matrix': {'rows'},
                  'unknown': {'reason'}}
        if kind not in fields or set(node) != fields[kind] | {'kind'}:
            raise ValueError('公式节点缺少字段、含未知字段或不支持的结构')
        def child(key):return visit(node[key],depth+1,path+'.'+key)
        def optional(key):return '' if node[key] is None else child(key)
        if kind == 'unknown':
            reason = node['reason']
            if not isinstance(reason,str) or not reason.strip() or len(reason)>200:raise ValueError('不可读公式节点需注明原因')
            unknown.append({'path':path,'reason':reason})
            return r'\text{[unreadable]}'
        if kind == 'token':
            value=node['value']
            if not isinstance(value,str) or not value.strip() or len(value)>80 or re.search(r'[\\{}\x00-\x1f]',value):
                raise ValueError('公式符号必须是可见原字符，不接受 LaTeX 命令')
            if any(not(ch.isalnum() or ch in ' +-*/=().,;:|<>![]%_' or ch in SYMBOLS) for ch in value):
                raise ValueError('公式含未支持符号，请对照原图复核')
            # Literal underscore/percent cannot silently turn into TeX structure.
            return ''.join(SYMBOLS[ch]+' ' if ch in SYMBOLS and SYMBOLS[ch].startswith('\\') else SYMBOLS.get(ch,'\\'+ch if ch in '_%' else ch) for ch in value).rstrip()
        if kind == 'row':
            if not isinstance(node['children'],list) or not 1<=len(node['children'])<=64:raise ValueError('公式行为空或超限')
            return ' '.join(visit(n,depth+1,path+'.children.'+str(i)) for i,n in enumerate(node['children']))
        if kind == 'fraction':return r'\frac{'+child('numerator')+'}{'+child('denominator')+'}'
        if kind == 'scripts':
            base=child('base');sub=optional('sub');sup=optional('sup')
            if not sub and not sup:raise ValueError('上下标结构未提供上下标')
            return '{'+base+'}'+('_{'+sub+'}' if sub else '')+('^{'+sup+'}' if sup else '')
        if kind == 'root':
            body=child('body');index=optional('index')
            return r'\sqrt'+('['+index+']' if index else '')+'{'+body+'}'
        if kind == 'large_operator':
            if node['symbol'] not in {'∑','∏','∫'}:raise ValueError('大型运算符类型无效')
            lower=optional('lower');upper=optional('upper');body=child('body')
            return SYMBOLS[node['symbol']]+('_{'+lower+'}' if lower else '')+('^{'+upper+'}' if upper else '')+' '+body
        rows=node['rows']
        if not isinstance(rows,list) or not 1<=len(rows)<=12 or any(not isinstance(row,list) or not 1<=len(row)<=12 for row in rows):raise ValueError('矩阵为空或超限')
        if len({len(row) for row in rows})!=1:raise ValueError('矩阵各行列数不一致')
        return r'\begin{matrix}'+r'\\'.join(' & '.join(visit(n,depth+1,path+'.rows.'+str(i)+'.'+str(j)) for j,n in enumerate(row)) for i,row in enumerate(rows))+r'\end{matrix}'
    latex=visit(tree)
    return {'latex':latex,'text':latex,'tree':tree,'structure_complete':not unknown,
            'structure_audit':{'version':VERSION,'nodes':count,'kinds':kinds,'unknown':unknown,
              'tree_sha256':hashlib.sha256(json.dumps(tree,ensure_ascii=False,sort_keys=True).encode()).hexdigest()},
            'status':'pending','verification':'visual_structure_requires_human_review',
            'reason':'视觉模型公式候选须对照原图核验；语法完整不代表识别正确',
            'human_reviewed':False}
