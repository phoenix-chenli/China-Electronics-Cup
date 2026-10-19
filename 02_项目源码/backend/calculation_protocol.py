"""Bounded rejected-action diagnostics and typed advice, never formula execution."""
from __future__ import annotations

import copy
import hashlib
import json
import re

from . import candidate_audit
from .audit_redaction import redact_diagnostics
from .document_receipts import received_snapshot

MAX_ACTION_CHARACTERS = candidate_audit.MAX_ATTEMPT_CHARACTERS
_ACTION_FIELDS = ('id','kind','expression','bindings','output_unit','precision','rule_evidence_ids','operation','depends_on')
_BINDING_FIELDS = ('evidence_id','row','column','unit','unit_column','unit_evidence_id','quote','start',
                   'user_input_id','end','calculation_id','formula_id','parameter_id','symbol','visual_item_id','value')
_SQL_FIELDS = ['evidence_id','row','column','unit','unit_column?','unit_evidence_id?']

INSTRUCTIONS = '''calculate.bindings 与完成答案的 query_bindings 是不同协议：前者禁止 value，数值由控制器从已执行结果读取；后者的 value 是最终结果格逐项核验字段，不能复制到 calculate.bindings。
SQL计算参数完整形式为 {evidence_id:"E_query",row:0,column:"原查询数值列",unit:"该格真实单位",unit_column:"同结果行的真实单位列"}。unit_column必须是查询中确有的同原表直接字段，不能是SELECT常量或另一表的单位。unit_evidence_id仅指向kind=text且明确写出单位的原文片段，不得填SQL结果自身的E编号；同一SQL行已带单位格时使用unit_column。若原字段已提供可追溯单位，可省略这两个额外单位证明字段。
纯单位换算也属于calculate规则：按单位归一化后仅用单参数表达式（如expression:"a"）即可，仍须在rule_evidence_ids显式引用实际已读取、确有该换算关系的原文。bindings中的unit_evidence_id只证明该参数单位，不会自动授权计算规则或自动填入rule_evidence_ids；已有单位出处时先核对该原文是否也包含本次换算关系，包含才显式复用其真实证据ID，不包含就继续查证。不得将费用计价operation用于普通单位换算，不能重复乘除已由单位系统处理的系数；最终独立语义与适用条件复核仍必须通过。
以下仅展示协议，E_query和列名必须换成已读结果的实际标识；不要把模板当证据或选择某对象的授权：{"id":"S_calc","kind":"calculate","expression":"q*p","bindings":{"q":{"evidence_id":"E_query","row":0,"column":"用量","unit":"kWh","unit_column":"用量单位"},"p":{"evidence_id":"E_query","row":0,"column":"单价","unit":"元/kWh","unit_column":"单价单位"}},"output_unit":"元","precision":2,"rule_evidence_ids":[],"operation":{"kind":"quantity_times_unit_price","pairs":[{"quantity":"q","unit_price":"p"}]},"depends_on":["S_query"]}。只有用户明确要求费用计算且真实单位、对象、时期、税口径与计价条件成立时才可用此有限操作，最终四项业务复核仍不可省略。多行先按实际对象/时期筛选，不由模板替用户选第0行。错误反馈不是支持依据，不要仅换步骤ID重复错误动作。'''


def _project_action(action):
    """Omit unknown keys by count; do not collect arbitrary nested model content."""
    if not isinstance(action,dict):
        return {'invalid_type':type(action).__name__}
    out={};omitted=max(0,len(action)-sum(k in action for k in _ACTION_FIELDS))
    for key in _ACTION_FIELDS:
        if key not in action: continue
        value=action[key]
        if key=='bindings' and isinstance(value,dict):
            rows=[]
            for name,binding in list(value.items())[:20]:
                row={'parameter':name}
                if isinstance(binding,dict):
                    row['binding']={k:copy.deepcopy(binding[k]) for k in _BINDING_FIELDS if k in binding}
                    row['omitted_field_count']=max(0,len(binding)-len(row['binding']))
                else:row['invalid_type']=type(binding).__name__
                rows.append(row)
            out[key]=rows
            if len(value)>20:out['omitted_binding_count']=len(value)-20
        elif key=='operation' and isinstance(value,dict):
            op={k:copy.deepcopy(value[k]) for k in ('kind','base','rate','direction','left','right') if k in value}
            pairs=value.get('pairs')
            if isinstance(pairs,list):
                op['pairs']=[{**{k:copy.deepcopy(v[k]) for k in ('quantity','unit_price') if k in v},
                              'omitted_field_count':max(0,len(v)-sum(k in v for k in ('quantity','unit_price')))}
                             if isinstance(v,dict) else {'invalid_type':type(v).__name__} for v in pairs[:20]]
                if len(pairs)>20:op['omitted_pair_count']=len(pairs)-20
            elif 'pairs' in value:op['invalid_pairs_type']=type(pairs).__name__
            op['omitted_field_count']=max(0,len(value)-sum(k in value for k in ('kind','pairs','base','rate','direction','left','right')))
            out[key]=op
        else:out[key]=copy.deepcopy(value)
    if omitted:out['omitted_action_field_count']=omitted
    return out


def rejection_receipt(action, message):
    raw=json.dumps(action,ensure_ascii=False,sort_keys=True,default=str)
    projected=_project_action(action)
    # Reuse the shared storage privacy rules before any retained prefix is cut.
    safe=redact_diagnostics({'action':projected,'message':message})
    snapshot=received_snapshot(safe)
    privacy=safe.get('_audit_redaction',{})
    snapshot['redacted']=snapshot['redacted'] or privacy.get('redacted') is True
    snapshot['truncated']=snapshot['truncated'] or privacy.get('truncated') is True
    record={'publication_status':'not_adopted','diagnostic_only':True,'is_evidence':False,
            'raw_action_sha256':hashlib.sha256(raw.encode()).hexdigest(),'raw_action_characters':len(raw),
            'raw_action_serialization':'decoded_action_json_sorted_keys_ensure_ascii_false',
            'projection':'allowed_action_and_binding_fields','received_action':snapshot}
    if len(json.dumps(record,ensure_ascii=False))>MAX_ACTION_CHARACTERS:
        # Cut only the already privacy-projected snapshot. The broader `safe`
        # diagnostics shape may still contain private nested field names.
        prefix = (json.dumps(snapshot['value'],ensure_ascii=False)
                  if 'value' in snapshot else snapshot.get('prefix',''))
        snapshot.pop('value',None)
        snapshot['truncated']=True
        snapshot['prefix']=prefix[:MAX_ACTION_CHARACTERS-700]
        while len(json.dumps(record,ensure_ascii=False))>MAX_ACTION_CHARACTERS:
            excess=len(json.dumps(record,ensure_ascii=False))-MAX_ACTION_CHARACTERS
            snapshot['prefix']=snapshot['prefix'][:max(0,len(snapshot['prefix'])-excess)]
    return record


def failure_feedback(action, message, evidence):
    """Advice reflects the observed local failure; it never repairs the action."""
    bindings=action.get('bindings') if isinstance(action,dict) else None
    fields=[]
    if '不能接收模型填写的数值' in message:
        code='model_value_forbidden'
        if isinstance(bindings,dict):
            fields=[f'bindings.{name}.value' if isinstance(name,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,31}',name)
                    else 'bindings[invalid_parameter].value' for name,b in bindings.items() if isinstance(b,dict) and 'value' in b]
        guidance='calculate.bindings禁止value；它仅属于最终query_bindings。重新核对实际evidence_id/row/column及单位来源后，由规划器重提位置绑定；控制器不会删value后执行。'
    elif '数值常量' in message and '缺少来源绑定' in message:
        code='coefficient_binding_required';fields=['expression','bindings','rule_evidence_ids']
        guidance='表达式语法可成立，但该数值常量没有来源授权。核对已读原文，将业务系数写为独立参数（例如表达式k*x，k绑定真实text证据的精确数值quote和实际单位），同时保留原文rule_evidence_ids；禁止自行填value或改用白名单数字绕过。若系数仅在图像公式中可见，调用formula_read独立核验结构；read_image本身不产生formula_structure。控制器不会自动绑定、改写表达式或批准运算与适用条件。'
    elif '缺少计算规则原文' in message:
        code='rule_authorization_missing';fields=['rule_evidence_ids','operation']
        guidance='缺少本次运算的规则授权；先核对真实已读原文并显式提供rule_evidence_ids。纯单位换算也须引用包含换算关系的原文，单变量expression使用已归一化参数，不重复乘除单位系数。unit_evidence_id只证明参数单位，不会自动授权规则；可检查现有绑定所指原文是否确含本次换算关系，只有确认后才由规划器将其真实ID显式列入rule_evidence_ids，否则继续查证。普通换算不能套用费用计价operation；只有用户确实要求费用计算时才考虑quantity_times_unit_price及真实q/p配对。控制器不补引用、不改动作、不放宽原门禁；参数、量纲、适用条件及原有独立业务/语义复核仍不可省略。'
    elif '单位依据' in message:
        code='text_unit_proof_required'
        if isinstance(bindings,dict):
            fields=[f'bindings.{name}.unit_evidence_id' if isinstance(name,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,31}',name)
                    else 'bindings[invalid_parameter].unit_evidence_id' for name,b in bindings.items()
                    if isinstance(b,dict) and b.get('unit_evidence_id') is not None]
        guidance='unit_evidence_id必须引用明确单位说明的text证据，SQL结果自身不是文字证明。若同一真实查询行有单位格，核对后改用unit_column真实列名；否则检索明确单位原文，不能猜单位。'
    elif any(s in message for s in ('单位列','单位格','SQL数值的单位','可追溯单位')):
        code='sql_unit_binding_invalid';fields=['bindings.*.unit','bindings.*.unit_column']
        guidance='核对同结果行的实际单位格及direct_columns来源。unit_column与数值列须来自同一原表直接字段；不能用常量、别名、另一行或另一表替代。没有明确单位依据则继续查证。'
    else:
        code='local_calculation_invalid'
        guidance='按实际错误核对已有证据及参数类型；文本用精确quote，SQL用evidence_id/row/column，先前计算用calculation_id。不可自行填写value，不改变对象/时期或绕过原门禁。'
    return {'code':code,'fields':fields[:4],'field_omissions':max(0,len(fields)-4),'sql_binding_fields':_SQL_FIELDS,'repair_guidance':guidance,
            'action_rewritten':False,'diagnostic_only':True}


def execution_receipt(action, calculation):
    """Keep a bounded mechanical receipt even if semantic publication fails."""
    fields=('id','expression','value','display_value','unit','parameters','evidence_ids',
            'rule_evidence_ids','rule_origin','is_hypothetical','unit_handling')
    data={'action':_project_action(action),'calculation':{k:copy.deepcopy(calculation[k]) for k in fields if k in calculation}}
    raw=json.dumps(data,ensure_ascii=False,sort_keys=True,default=str)
    safe=redact_diagnostics(data)
    snapshot=received_snapshot(safe)
    privacy=safe.get('_audit_redaction',{})
    snapshot['redacted']=snapshot['redacted'] or privacy.get('redacted') is True
    snapshot['truncated']=snapshot['truncated'] or privacy.get('truncated') is True
    record={'step_id':action.get('id'),'calculation_id':calculation['id'],
            'mechanical_status':'executed','semantic_status':'not_reviewed',
            'publication_status':'pending_semantic_review','is_evidence':False,'diagnostic_only':True,
            'raw_execution_sha256':hashlib.sha256(raw.encode()).hexdigest(),
            'execution':snapshot}
    if len(json.dumps(record,ensure_ascii=False))>MAX_ACTION_CHARACTERS:
        prefix=json.dumps(snapshot.pop('value'),ensure_ascii=False) if 'value' in snapshot else snapshot.get('prefix','')
        snapshot.update(truncated=True,prefix=prefix[:MAX_ACTION_CHARACTERS-650])
        while len(json.dumps(record,ensure_ascii=False))>MAX_ACTION_CHARACTERS:
            excess=len(json.dumps(record,ensure_ascii=False))-MAX_ACTION_CHARACTERS
            snapshot['prefix']=snapshot['prefix'][:max(0,len(snapshot['prefix'])-excess)]
    return record
