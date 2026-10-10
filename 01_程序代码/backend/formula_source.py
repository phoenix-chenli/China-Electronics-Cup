"""Exact document formula binding and deterministic dimensional checks."""
import ast
import copy
import re
from decimal import Decimal,InvalidOperation
from . import documents

UNITS={'元':{'CNY':1},'人民币元':{'CNY':1},'CNY':{'CNY':1},'分钟':{'min':1},'min':{'min':1},'小时':{'hour':1},
       'hour':{'hour':1},'件':{'piece':1},'个':{'item':1},'台':{'device':1},'人':{'person':1},
       'ratio':{},'%':{},'元/分钟':{'CNY':1,'min':-1},'CNY/min':{'CNY':1,'min':-1},
       '元/小时':{'CNY':1,'hour':-1},'CNY/hour':{'CNY':1,'hour':-1},'元/件':{'CNY':1,'piece':-1},'CNY/件':{'CNY':1,'piece':-1}}


def dimension(unit):
    if unit not in UNITS:raise ValueError('文档公式参数或结果的单位不明确，需先确认')
    return dict(UNITS[unit])


def database_parameter(parameter,candidates,spans,delivered):
    ref=parameter['unit_source']
    if not isinstance(ref,dict) or set(ref)!={'support_span_id','source_quote'}:raise ValueError('数据库单位需提供原文位置及精确字段单位声明')
    sid,quote=ref['support_span_id'],ref['source_quote']
    if sid not in spans or sid not in delivered or not isinstance(quote,str) or quote not in spans[sid]['text']:raise ValueError('字段单位声明不是本轮已发送原文')
    source=spans[sid]
    if documents.resolve(source['locator'],source['text'])['status']!='located':raise ValueError('字段单位来源不能复查')
    m=re.fullmatch(r'\s*([a-zA-Z_][\w]*(?:\s*(?:和|、|,)\s*[a-zA-Z_][\w]*)*)\s*的单位(?:均)?为\s*(人民币元|元|分钟|小时|件|个|台|人)\s*[。；;]?\s*',quote)
    if not m:raise ValueError('单位声明需明确列出数据库字段名及单位，不从字段后缀猜测')
    fields=re.split(r'\s*(?:和|、|,)\s*',m[1]);unit={'人民币元':'CNY','元':'CNY','分钟':'min','小时':'hour'}.get(m[2],m[2])
    if parameter.get('unit')!=unit:raise ValueError('参数单位与字段单位声明不一致')
    try:number=Decimal(str(parameter['value']))
    except (InvalidOperation,KeyError):raise ValueError('数据库参数数值不正确') from None
    ids=set(parameter.get('evidence_ids',[]))
    for fact in candidates:
        if fact.get('unit')!='untyped' or not fact.get('unit_origin'):continue
        if fact['unit_origin'].rsplit('.',1)[-1] not in fields:continue
        if Decimal(str(fact['value']))!=number or ids!=set(fact['evidence_ids'])|{source['evidence_id']}:continue
        return {**fact,'unit':unit,'evidence_ids':parameter['evidence_ids'],
                'unit_binding':{**ref,'field_origin':fact['unit_origin'],'locator':source['locator']}}
    raise ValueError('查询输出没有可确认的同字段数值及单位来源；计算列、计数或不明单位需澄清')


def _shape(node):
    if isinstance(node,ast.Constant) and type(node.value) in (int,float):return ('number',str(Decimal(str(node.value)).normalize()))
    return (type(node).__name__,tuple((key,_shape(value) if isinstance(value,ast.AST) else tuple(_shape(x) for x in value) if isinstance(value,list) else value) for key,value in ast.iter_fields(node)))


def bind(expression,parameters,reference,spans,delivered):
    if not isinstance(reference,dict) or set(reference)!={'support_span_id','source_quote','result_unit'}:
        raise ValueError('文档公式需提供原文位置、精确公式引文和结果单位')
    sid,quote=reference['support_span_id'],reference['source_quote']
    if sid not in spans or sid not in delivered or not isinstance(quote,str) or quote not in spans[sid]['text']:
        raise ValueError('公式结构没有本轮已发送的精确原文')
    source=spans[sid]
    if documents.resolve(source['locator'],source['text'])['status']!='located':raise ValueError('公式原文无法回到固定版本')
    try:tree=ast.parse(expression,mode='eval')
    except (SyntaxError,ValueError):raise ValueError('公式无法解析') from None
    matches=[]
    # A literal named equation with a printed result unit is supported. Prose
    # inferred equations remain unconfirmed instead of being treated as rules.
    substitutions={}
    for parameter in parameters:
        name=parameter.get('source_name')
        if name is not None:
            if not isinstance(name,str) or not name.isidentifier():raise ValueError('原文公式变量需使用原文明确的变量名')
            substitutions[parameter['name']]=ast.Name(id=name,ctx=ast.Load())
        elif parameter.get('source_quote'):
            numbers=re.findall(r'[-+]?\d[\d,]*(?:\.\d+)?',parameter['source_quote'])
            if len(numbers)!=1:raise ValueError('原文系数缺少唯一精确数值')
            substitutions[parameter['name']]=ast.Constant(value=float(numbers[0].replace(',','')))
    class Replace(ast.NodeTransformer):
        def visit_Name(self,node):return copy.deepcopy(substitutions.get(node.id,node))
    expanded=Replace().visit(copy.deepcopy(tree))
    for line in re.split(r'[\n。；;]',quote):
        m=re.fullmatch(r'\s*(?:公式[：:]\s*)?([^\W\d]\w*)\s*(?:[\[(（]([^\])）]+)[\])）])?\s*=\s*(.+?)\s*',line)
        if not m:continue
        try:original=ast.parse(m[3].replace('×','*').replace('÷','/').replace('−','-').replace('，',','),mode='eval')
        except SyntaxError:continue
        # Named symbolic equations keep variables; literal coefficients may be
        # replaced only by parameters that already have exact numeric quotes.
        if _shape(original) not in (_shape(tree),_shape(expanded)):continue
        declared=m[2].strip() if m[2] else None
        if declared is None:
            unit=re.search(re.escape(m[1])+r'为\s*[-+]?\d[\d,]*(?:\.\d+)?\s*(元|分钟|小时|件|个|台|人)',source['text'])
            if unit:declared=unit[1]
        if declared:matches.append((m,declared))
    if len(matches)!=1:raise ValueError('计算结构与原文明确公式不一致；自然语言规则需先确认明确公式')
    declared=dimension(matches[0][1])
    if dimension(reference['result_unit'])!=declared:raise ValueError('结果单位与原文不一致')
    units={p['name']:dimension(p['unit']) for p in parameters}
    used=set()
    def same(values):
        nonzero=[v for v in values if v is not None]
        if not nonzero:return None
        if any(v!=nonzero[0] for v in nonzero):raise ValueError('公式加减或比较的单位不一致')
        return nonzero[0]
    def visit(node):
        if isinstance(node,ast.Expression):return visit(node.body)
        if isinstance(node,ast.Name) and node.id in units:used.add(node.id);return units[node.id]
        if isinstance(node,ast.Constant) and type(node.value) in (int,float) and node.value in (0,1):return None if node.value==0 else {}
        if isinstance(node,ast.UnaryOp) and isinstance(node.op,(ast.UAdd,ast.USub)):return visit(node.operand)
        if isinstance(node,ast.BinOp):
            a,b=visit(node.left),visit(node.right)
            if isinstance(node.op,(ast.Add,ast.Sub)):return same([a,b])
            if isinstance(node.op,(ast.Mult,ast.Div)):
                if a is None or b is None:raise ValueError('零常数只用于加减或上下限比较，不能替代有单位的参数')
                result=dict(a);sign=1 if isinstance(node.op,ast.Mult) else -1
                for key,value in b.items():result[key]=result.get(key,0)+sign*value
                return {k:v for k,v in result.items() if v}
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id in {'max','min'} and not node.keywords and 1<=len(node.args)<=5:return same([visit(a) for a in node.args])
        raise ValueError('公式包含未支持内容或缺少有来源参数')
    if len(list(ast.walk(tree)))>80:raise ValueError('公式过于复杂')
    if visit(tree)!=declared:raise ValueError('参数量纲不能得到原文声明的结果单位')
    if used!=set(units):raise ValueError('公式参数必须与实际使用变量逐一对应')
    return {'support_span_id':sid,'source_quote':quote,'evidence_id':source['evidence_id'],
            'locator':source['locator'],'result_unit':reference['result_unit'],
            'structure_check':'exact_document_equation','unit_check':'passed',
            'conditions':'原文公式结构与单位已绑定；业务对象、时期和适用条件仍由逐条证据审查核对'}
