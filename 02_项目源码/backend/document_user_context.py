"""Exact user-origin quantity scenarios for quotation-based document answers.

User inputs never enter the document evidence registry or a numeric whitelist.
Only their own occurrence may be annotated; applicability needs separate review.
"""
from __future__ import annotations
import copy
import hashlib
import re
from decimal import Decimal, InvalidOperation

_ASSUME = re.compile(r'假设|假定|情景设定')
_REVOKE = re.compile(r'撤销|取消|停止沿用|不再使用|不要沿用|不再沿用')
_DENY = re.compile(r'不要|不用|禁止|不能|并非|不是|不做|没有|不应|不得|尚未|未曾')
_UNITS = r'万元|千克|公斤|毫升|件|个|台|套|人|次|吨|元|kg|mL|g|L'
_QUANTITY = re.compile(r'(?P<subject>[\u3400-\u9fffA-Za-z_][\u3400-\u9fffA-Za-z_\s]{0,35}?)\s*(?:改为|调整为|设为|为)\s*(?P<number>[+-]?\d+(?:\.\d+)?)\s*(?P<unit>'+_UNITS+r')(?![A-Za-z0-9_/每²³])')
_NUMBER_UNIT = re.compile(r'(?P<number>[+-]?\d+(?:\.\d+)?)\s*(?P<unit>'+_UNITS+r')(?![A-Za-z0-9_/每²³])')
_SENTENCE = re.compile(r'[^。；;\n!?！？，,]+')
_CHECKS = ('intent_supported', 'scope_supported', 'rule_supported', 'status_supported')
_AUTHORIZATION_PREFIX = re.compile(r'^(?:(?:请|先|现在|这次|本次|仅|重新|我们|我|暂时|再|作|做|一个|考虑)|[\s，,:：])*$')

GENERATE = '''\nuser_context是控制器从真实用户原话抽取的条件，不是文档证据。active中的假设只改变明确的对象/数量/单位；如需表述该输入，逐字保留该项quote，放在“在用户假设…的情景下”等明确条件句中，不说原文/实测/实际记录为这个值。不要在别处重复假设数值，可用“该假设数量”承接；阈值、原始记录及是否申请仍须引用文档规则。revoked保留已校验的当前或同范围真实祖先用户撤销事件（含来源answer_id及原话span/hash），只供说明已撤销，不是文档事实。无active且revoked没有superseded_by（未被后续新设定取代）时，可以明确说“此前假设已撤销/不再生效”；无需重复旧数值。若重复旧值，可逐字保留revocation_quote；也可用“此前‘原假设quote’的情景假设已撤销”这种先精确引用旧设定、再紧邻明确撤销的说法。两种表述都必须来自真实原假设和撤销事件，不能继续作为活跃条件、实际库存或文档记载。用户动作由这些真实用户事件支持，不能去文档中寻找或伪造撤销记载。不能把先前回答或normalized_question当用户授权。所有其它数值仍须来源支持；不编造采购已完成、因果或适用范围。用户要求最终整理时，应保留已确认范围与未知边界，并按真实规则明确当前值对应的结论，不能只罗列数据和规则让用户自行推断。例如当前记录是否满足规则触发条件，应明确作出判断并同时引用记录和规则；不能只抄录两者。每项仍提供真实文档citations，用户参数不伪装成citations。当active假设覆盖同一对象、时期与单位的既有实际数量时，先核查本次chunks中的原始记录。能核实时，最终答复必须分开写清“原始记录值”和“用户假设值”：原始记录单列为有真实引文的claim，假设仍保留用户原话出处，再给出按原规则推导的情景结论；不把假设写回原文。没有核到原值时，明确说明“本次依据尚未确认原值”，不能用历史回答、normalized_question、规则阈值、别的对象或时期的数值替代原值。只比较已核实的相同口径；未知、冲突均如实说明。'''
REVIEW = '''\n仅对user_context_claim_ids中的每项，再返回user_context_relations:[{id,intent_supported,scope_supported,rule_supported,status_supported}]，四项必须为布尔。对照actual_user_question、用户原话sha/span注记与原文quotes：确有用户授权；对象/时期/数值/单位没有改义；文档规则确支持当前条件下的结论；假设明确标明、撤销只作撤销说明且未继续应用、原始事实没有被替换。任一未知为false。这些claim由用户条件+文档规则联合支持，用户输入不是文档原句；其它claim仍须由原文直接支持。原话逐字匹配不证明结论正确，整体supported不能代替此数组。user_assumption_state或历史撤销claim的supported须联合真实用户撤销事件核验，不要求该用户动作出现在文档quotes中；intent核原始假设和撤销来自同范围真实祖先，scope核对象及分支，rule核同句业务判断的真实引文，status核无后续新设定推翻撤销且未把旧值当活跃条件。用户要求最终整理时，要保留当前明确判定及已知范围/未知边界，不可只罗列数据和规则让用户自行推断；当前判断缺失时即使数据和规则各自正确，也应answer_complete=false并对相关分项给出evidence_available。仍须逐项引用并核验，不以改写后的question_parts省略了该结论为由忽略actual_user_question的整理要求。另外检查假设与原始事实的对照是否完整：active覆盖既有实际量时，如available_evidence中可核到同对象、时期、单位的原值，最终claims须含这个原值并引用原始记录，同时将其与用户假设分开。原值是否支持仍仅按该claim自己的quotes判断，不能用历史回答或未引用的片段替代引用。若原值可核但漏答或漏引，answer_complete=false，并将相关情景分项标为evidence_available，绑定真实available_evidence的id以补齐；不能因情景计算正确就判完整。当前片段不足以核原值且还需查找时，将相关分项标为missing_evidence，触发已有一次补检；补检仍无依据时保留“本次依据尚未确认原值”的范围说明，不得宣称原件无原值或编出一个原值。规则阈值及别的对象、时期或单位的数量均不是原始基线。'''


def _hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _quoted(text, start, end):
    for left,right in [('“','”'),('‘','’'),('「','」'),('『','』'),('"','"'),("'","'")]:
        before=text[:start]
        inside=before.count(left)%2==1 if left==right else before.rfind(left)>before.rfind(right)
        if inside and text.find(right,end)>=end:return True
    return False


def _scope(revisions):
    if not isinstance(revisions,list) or not revisions:return None
    out=[]
    for item in revisions:
        if not isinstance(item,dict) or not all(isinstance(item.get(k),str) and item[k] for k in ('id','sha256','parser_version')):return None
        out.append(tuple(item.get(k) for k in ('id','sha256','parser_version','revision','semantic_revision')))
    return sorted(out)


def _inputs(text, answer_id):
    out=[]
    for sentence in _SENTENCE.finditer(text):
        raw=sentence[0]
        markers=list(_ASSUME.finditer(raw))
        if not markers:continue
        marker=markers[0]
        if (not _AUTHORIZATION_PREFIX.fullmatch(raw[:marker.start()]) or
                _DENY.search(raw[:marker.end()]) or _REVOKE.search(raw) or
                _quoted(text,sentence.start()+marker.start(),sentence.start()+marker.end())):continue
        tail_start=marker.end()
        tail=raw[tail_start:].lstrip(' ：:，,')
        tail_start=len(raw)-len(tail)
        match=_QUANTITY.match(tail)
        if not match or _DENY.search(tail[:match.end()]):continue
        if tail[match.end():].lstrip().startswith(('/', '*', '每', '²', '³')):continue
        try:
            number=Decimal(match['number'])
            if not number.is_finite() or abs(number)>Decimal('1e18'):continue
        except InvalidOperation:continue
        start=sentence.start()+tail_start+match.start();end=sentence.start()+tail_start+match.end()
        nstart=sentence.start()+tail_start+match.start('number');nend=sentence.start()+tail_start+match.end('number')
        if _quoted(text,start,end):continue
        out.append({'id':'DU'+_hash(answer_id+'\n'+text+'\n'+str(start))[:12],
            'origin':'user_question','source_answer_id':answer_id,'source_text':text,'source_sha256':_hash(text),
            'quote':text[start:end],'start':start,'end':end,'number_start':nstart,'number_end':nend,
            'subject':match['subject'].strip(),'value':match['number'],'unit':match['unit'],
            'is_hypothetical':True,'is_document_evidence':False})
    return out[:4]


def validated_history(history, source_revisions):
    """Retain only a contiguous service-owned, completed, same-source suffix.

    Production supplies branch coordinates from real stored ancestors. Legacy
    direct callers retain their existing trusted-history contract.
    """
    expected=_scope(source_revisions);out=[]
    for item in list(history or [])[-30:]:
        valid=(isinstance(item,dict) and item.get('kind')!='structured_context'
            and isinstance(item.get('answer_id'),str) and bool(item['answer_id'])
            and isinstance(item.get('original_question'),str) and len(item['original_question'])<=12000
            and expected is not None and _scope(item.get('source_revisions'))==expected
            and item.get('status')=='completed')
        if not valid:
            out=[];continue
        if any(previous['answer_id']==item['answer_id'] for previous in out):
            out=[];continue
        if out and 'parent_answer_id' in item:
            previous=out[-1]
            if (item['parent_answer_id']!=previous['answer_id']
                or item.get('conversation_id')!=previous.get('conversation_id')
                or not isinstance(item.get('turn_index'),int)
                or item['turn_index']!=previous.get('turn_index',-2)+1):
                out=[];continue
        out.append(item)
    return out


def _valid_revocation(item):
    raw=item.get('source_text','');start=item.get('start');end=item.get('end')
    if not isinstance(start,int) or not isinstance(end,int) or _hash(raw)!=item.get('source_sha256') or raw[start:end]!=item.get('quote'):
        return False
    keys=('id','quote','start','end','number_start','number_end','subject','value','unit')
    if not any(all(candidate.get(k)==item.get(k) for k in keys) for candidate in _inputs(raw,item.get('source_answer_id',''))):
        return False
    revoked=item.get('revocation_source_text','');rs=item.get('revocation_start');re_=item.get('revocation_end')
    if (not isinstance(rs,int) or not isinstance(re_,int) or _hash(revoked)!=item.get('revocation_sha256')
        or revoked[rs:re_]!=item.get('revocation_quote') or item.get('active') is not False):return False
    for sentence in _SENTENCE.finditer(revoked):
        marker=_REVOKE.search(sentence[0])
        if (marker and sentence.start()+marker.start()==rs and sentence.end()==re_
            and _AUTHORIZATION_PREFIX.fullmatch(sentence[0][:marker.start()])
            and not _DENY.search(sentence[0][:marker.start()]) and _ASSUME.search(sentence[0])
            and not _quoted(revoked,rs,re_)):
            quantities=list(_NUMBER_UNIT.finditer(revoked[rs:re_]))
            def matches(q):
                prefix=revoked[rs:rs+q.start()]
                rest=re.sub(r'撤销|取消|停止沿用|不再使用|不要沿用|不再沿用|刚才|此前|之前|上一个|这个|该|假设|情景|的|\s','',prefix)
                return q['number']==item['value'] and q['unit']==item['unit'] and (not rest or item['subject'] in rest)
            return not quantities or any(matches(q) for q in quantities)
    return False


def build_context(actual_user_question, history=(), *, source_revisions=None):
    """History is a service-owned projection of real same-scope answer records.

    Never read normalized_question, generated answers, intent or structured slots.
    Legacy callers without actual_user_question get no numeric exception.
    """
    if actual_user_question is None:return None
    if not isinstance(actual_user_question,str) or len(actual_user_question)>12000:raise ValueError('用户原话无效')
    active=[];revoked=[]
    turns=[(item['answer_id'],item['original_question'],False) for item in validated_history(history, source_revisions)]
    turns.append(('current',actual_user_question,True))
    for answer_id,text,current in turns:
        if len(text)>12000:active=[];continue
        # Apply user instructions in their original order. A later withdrawal
        # must not be undone by re-adding an earlier input from the same turn.
        events=[(item['start'],'input',item) for item in _inputs(text,answer_id)]
        for sentence in _SENTENCE.finditer(text):
            m=_REVOKE.search(sentence[0])
            if (not m or not _AUTHORIZATION_PREFIX.fullmatch(sentence[0][:m.start()]) or
                    _DENY.search(sentence[0][:m.start()]) or not _ASSUME.search(sentence[0])):continue
            start=sentence.start()+m.start();end=sentence.end()
            if _quoted(text,start,end):continue
            events.append((start,'revoke',(start,end)))
        for _,kind,value in sorted(events,key=lambda event:event[0]):
            if kind=='input':
                active=[value]
                for event in revoked:
                    event['superseded_by']=value['id']  # Retain history, never mistake it for current state.
                continue
            start,end=value
            quantities=list(_NUMBER_UNIT.finditer(text[start:end]))
            def same_object(a, q):
                prefix=text[start:start+q.start()]
                rest=re.sub(r'撤销|取消|停止沿用|不再使用|不要沿用|不再沿用|刚才|此前|之前|上一个|这个|该|假设|情景|的|\s','',prefix)
                return not rest or a['subject'] in rest
            matches=[a for a in active if not quantities or any(q['number']==a['value'] and q['unit']==a['unit'] and same_object(a,q) for q in quantities)]
            if not matches:continue
            revoked.extend({**copy.deepcopy(a),'revocation_quote':text[start:end],
                'revocation_start':start,'revocation_end':end,'revocation_source_text':text,
                'revocation_source_answer_id':answer_id,
                'revocation_sha256':_hash(text),'active':False} for a in matches)
            active=[a for a in active if a not in matches]
    # Past numbers are usable only when the current user explicitly carries the
    # scenario forward. A general follow-up does not silently activate it.
    if active and active[0]['source_answer_id']!='current' and not (
            _ASSUME.search(actual_user_question) and re.search(r'沿用|仍按|继续|刚才|此前',actual_user_question)):
        active=[]
    return {'schema_version':'document-user-context-v1','actual_user_question':actual_user_question,
        'actual_user_question_sha256':_hash(actual_user_question),'active':active,'revoked':revoked,
        'boundary':'用户条件不是原文事实；仅对应出现位置可注记，结论仍须规则及独立复核。'}


def annotate(text, context):
    annotations=[];spans=[]
    if not context:return text,annotations
    for kind,items in [('user_quantity_assumption',context.get('active',[])),('user_assumption_revocation',context.get('revoked',[]))]:
        for item in items:
            raw=item['source_text'];start=item['start'];end=item['end']
            if _hash(raw)!=item['source_sha256'] or raw[start:end]!=item['quote']:continue
            keys=('id','quote','start','end','number_start','number_end','subject','value','unit')
            if not any(all(candidate.get(k)==item.get(k) for k in keys)
                       for candidate in _inputs(raw,item['source_answer_id'])):continue
            quote=item['quote'] if kind=='user_quantity_assumption' else item['revocation_quote']
            if kind=='user_assumption_revocation' and not _valid_revocation(item):continue
            for match in re.finditer(re.escape(quote),text):
                left=max(text.rfind(mark,0,match.start()) for mark in '。；;\n')+1
                ends=[i for mark in '。；;\n' if (i:=text.find(mark,match.end()))>=0]
                clause=text[left:min(ends,default=len(text))]
                prefix=text[left:match.start()]
                if kind=='user_quantity_assumption':
                    if not _ASSUME.search(clause) or _REVOKE.search(clause) or _DENY.search(prefix) or re.search(r'原文(?:实盘|记录|记载|为|是)|实测|实际(?:记录|库存|实盘|为|是)|真实(?:库存|实盘|为|是)',clause):continue
                else:
                    # A revoked value is only admitted inside the exact explicit
                    # revocation phrase. Following quantities remain unmasked.
                    if _DENY.search(prefix) or re.search(r'(?:继续|仍|沿用).{0,12}(?:假设|情景)',text[match.end():min(ends,default=len(text))]):continue
                for num in _NUMBER_UNIT.finditer(quote):
                    if num['number']!=item['value'] or num['unit']!=item['unit']:continue
                    ns=match.start()+num.start('number');ne=match.start()+num.end('number')
                    spans.append((ns,ne))
                    annotations.append({'kind':kind,'origin':'user_question','user_input_id':item['id'],
                        'source_answer_id':item['source_answer_id'],'subject':item['subject'],'value':item['value'],'unit':item['unit'],
                        'source_text':raw,'source_sha256':item['source_sha256'],'quote':item['quote'],'source_start':start,'source_end':end,
                        'start':ns,'end':ne,'is_hypothetical':kind=='user_quantity_assumption','is_document_evidence':False,
                        **({**{k:item[k] for k in ['revocation_quote','revocation_source_text','revocation_sha256','revocation_start','revocation_end']},'revocation_source_answer_id':item.get('revocation_source_answer_id','current')} if kind=='user_assumption_revocation' else {}),
                        'semantic_review_required':True})
    # A verbatim old input may be identified before an explicit withdrawal
    # statement. Bind that occurrence to BOTH real user events; it is never a
    # current quantity or a document fact, and still needs the four-way review.
    if not context.get('active'):
        for item in context.get('revoked',[]):
            if item.get('superseded_by') or not _valid_revocation(item):continue
            for match in re.finditer(re.escape(item['quote']),text):
                left=max(text.rfind(mark,0,match.start()) for mark in '。；;\n，,')+1
                prefix=text[left:match.start()]
                lead=re.fullmatch(r'(?:此前|刚才|之前|先前)(?:用户)?(?P<opening>[“‘「『\"\']?)',prefix)
                if not lead:continue
                closing={'“':'”','‘':'’','「':'」','『':'』','"':'"',"'":"'",'':''}[lead['opening']]
                rest=text[match.end():]
                suffix=re.match(re.escape(closing)+r'(?:的)?(?:情景)?假设(?:现已|已经|已)?(?:撤销|取消|不再生效|失效|不再沿用|不再适用)',rest)
                if not suffix:continue
                finish=match.end()+suffix.end()
                tail_end=min((i for mark in '。；;\n' if (i:=text.find(mark,finish))>=0),default=len(text))
                tail=text[finish:tail_end]
                if (text[finish:].lstrip().startswith(('？','?')) or
                        re.search(r'(?:继续|仍|沿用|重新|恢复).{0,12}(?:假设|情景)|假设.{0,8}(?:生效|启用)',tail)):
                    continue
                ns=match.start()+item['number_start']-item['start'];ne=match.start()+item['number_end']-item['start']
                spans.append((ns,ne))
                annotations.append({'kind':'user_assumption_revocation','origin':'user_question',
                    'user_input_id':item['id'],'source_answer_id':item['source_answer_id'],
                    'subject':item['subject'],'value':item['value'],'unit':item['unit'],
                    'source_text':item['source_text'],'source_sha256':item['source_sha256'],
                    'quote':item['quote'],'source_start':item['start'],'source_end':item['end'],
                    'revocation_source_answer_id':item.get('revocation_source_answer_id','current'),
                    **{key:item[key] for key in ['revocation_quote','revocation_source_text','revocation_sha256','revocation_start','revocation_end']},
                    'start':ns,'end':ne,'state':'revoked','is_hypothetical':False,'is_document_evidence':False,
                    'semantic_review_required':True})
    # A state-only explanation need not repeat the old numeric value. Bind the
    # exact statement to the real withdrawal and still require semantic review.
    if not context.get('active'):
        state_pattern=re.compile(r'(?:此前|刚才|之前|该|这个)(?:的)?(?:情景)?假设(?:现已|已经|已|现在)?(?:撤销|取消|不再生效|失效|不再沿用|不再适用)')
        for item in context.get('revoked',[]):
            if item.get('superseded_by') or not _valid_revocation(item):continue
            for match in state_pattern.finditer(text):
                left=max(text.rfind(mark,0,match.start()) for mark in '。；;\n，,')+1
                tail_end=min((i for mark in '。；;\n' if (i:=text.find(mark,match.end()))>=0),default=len(text))
                if (_DENY.search(text[left:match.start()]) or _quoted(text,match.start(),match.end())
                    or re.search(r'(?:继续|仍|沿用).{0,12}(?:假设|情景)',text[match.end():tail_end])
                    or text[match.end():].lstrip().startswith(('？','?'))):continue
                annotations.append({'kind':'user_assumption_state','origin':'user_question',
                    'user_input_id':item['id'],'source_answer_id':item['source_answer_id'],
                    'revocation_source_answer_id':item.get('revocation_source_answer_id','current'),
                    'source_text':item['revocation_source_text'],'source_sha256':item['revocation_sha256'],
                    'quote':item['revocation_quote'],'source_start':item['revocation_start'],'source_end':item['revocation_end'],
                    'start':match.start(),'end':match.end(),'state':'revoked','is_document_evidence':False,
                    'semantic_review_required':True})
    chars=list(text)
    for start,end in spans:chars[start:end]=' '*(end-start)
    return ''.join(chars),annotations


def reviewed_ids(review, required):
    if not required:return set()
    rows=review.get('user_context_relations')
    if (not isinstance(rows,list) or len(rows)!=len(required) or
            any(not isinstance(row,dict) or row.get('id') not in required or any(type(row.get(k)) is not bool for k in _CHECKS) for row in rows) or
            len({r['id'] for r in rows})!=len(required)):
        return set()
    return {r['id'] for r in rows if all(r[k] for k in _CHECKS)}
