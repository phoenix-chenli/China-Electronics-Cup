"""Question/requirement coverage and bounded recovery; never a truth oracle."""
from __future__ import annotations
import copy
import json
import re

VERSION='rag-completeness-v1'
MAX_REPAIRS=1


def question_parts(question):
    """Separate explicit clauses, preserving quotes, premises and conditions.

    This is a surface inventory, not semantic decomposition or a name recognizer.
    The model must map its minimal goals to every inventoried clause.
    """
    parts=[];buffer=[];stack=[]
    pairs={'“':'”','‘':'’','「':'」','『':'』','(' : ')','（':'）','[':']','【':'】'}
    for index,char in enumerate(question):
        buffer.append(char)
        if stack and char==stack[-1]:stack.pop();continue
        if char in pairs:stack.append(pairs[char]);continue
        if char in {'"',"'"}:
            if char=="'" and index and index+1<len(question) and question[index-1].isalnum() and question[index+1].isalnum():
                continue
            if stack and stack[-1]==char:stack.pop()
            elif not stack:stack.append(char)
            continue
        if not stack and char in ';；?？\n':
            text=''.join(buffer).strip();buffer=[]
            if text:parts.append(text)
    if ''.join(buffer).strip():parts.append(''.join(buffer).strip())
    trigger=r'哪些|什么|多少|是否|能否|为何|怎么|如何|何时|给出|列出|比较|核对|说明|注明|解释|判断|汇总|\b(?:what|which|when|why|how|list|compare|explain)\b'
    # A premise separated by punctuation remains attached to the next goal;
    # it is never silently turned into another factual requirement.
    merged=[];prefix=[]
    for text in parts:
        if re.search(trigger,text,re.I):merged.append(''.join(prefix)+text);prefix=[]
        else:prefix.append(text)
    if prefix:
        if merged:merged[-1]+=''.join(prefix)
        else:merged=[''.join(prefix)]
    return [{'id':'Q'+str(i+1),'text':text} for i,text in enumerate(merged or [question])]


def validate_plan(raw, parts, kinds):
    if not isinstance(raw,list) or not 1<=len(raw)<=8:raise ValueError('问题事实规划必须包含1至8项')
    if len(parts)>8:raise ValueError('明确子问超过8项，请分批提问；未省略子问')
    allowed={p['id'] for p in parts};covered=set();seen=set();out=[]
    for i,item in enumerate(raw):
        if not isinstance(item,dict) or not isinstance(item.get('text'),str) or not 1<=len(item['text'].strip())<=500:raise ValueError('问题事实规划格式不正确')
        if type(item.get('independent',False)) is not bool:raise ValueError('独立子问标识不正确')
        kind=item.get('answer_kind','fact_value')
        if not isinstance(kind,str) or kind not in kinds:raise ValueError('必要事实回答类型不正确')
        ids=item.get('question_part_ids',[parts[0]['id']] if len(parts)==1 else None)
        if not isinstance(ids,list) or not ids or len(ids)>8 or any(not isinstance(q,str) or q not in allowed for q in ids) or len(set(ids))!=len(ids):raise ValueError('必要事实必须绑定真实问题分项question_part_ids，不能遗漏或虚构编号')
        key=''.join(item['text'].split()).casefold()
        if key in seen:raise ValueError('重复必要事实不能代替逐项回答，请明确不同对象和口径')
        seen.add(key);covered.update(ids)
        out.append({'id':'R'+str(i+1),'text':item['text'].strip(),'independent':item.get('independent',False),'answer_kind':kind,'question_part_ids':ids})
    if covered!=allowed:raise ValueError('必要事实遗漏问题分项：'+'、'.join(sorted(allowed-covered)))
    return out


def ledger(question, needed, assessment=None, verification=None):
    parts=question_parts(question);before={r['requirement_id']:r for r in (assessment or {}).get('coverage',[])}
    after={r['requirement_id']:r for r in (verification or {}).get('coverage',[])}
    accepted={c['id'] for c in (verification or {}).get('claims',[]) if c.get('semantic_check',{}).get('status')=='supported'}
    rows=[]
    for requirement in needed:
        rid=requirement['id'];prior=before.get(rid,{});final=after.get(rid,{})
        ids=[cid for cid in final.get('claim_ids',[]) if cid in accepted]
        answered=final.get('status')=='supported' and bool(ids)
        rows.append({'id':rid,'text':requirement['text'],'question_part_ids':requirement.get('question_part_ids',['Q1']),
            'answer_kind':requirement.get('answer_kind','fact_value'),'independent':requirement.get('independent',False),
            'source_status':prior.get('status','unknown'),'answer_status':'answered' if answered else 'conflicted' if final.get('status')=='conflicted' or prior.get('status')=='conflicted' else 'unanswered',
            'claim_ids':ids,'support_span_ids':list(prior.get('support_span_ids',[])),
            'reason':final.get('reason') or prior.get('reason') or ('已绑定通过检查的结论' if answered else '尚未形成已核验回答')})
    part_rows=[]
    for part in parts:
        attached=[r for r in rows if part['id'] in r['question_part_ids']]
        part_rows.append({**part,'requirement_ids':[r['id'] for r in attached],
            'status':'answered' if attached and all(r['answer_status']=='answered' for r in attached) else 'unplanned' if not attached else 'unanswered'})
    count=sum(r['answer_status']=='answered' for r in rows)
    return {'version':VERSION,'requirements':rows,'question_parts':part_rows,'answered':count,'total':len(rows),
        'percent':round(count/len(rows)*100) if rows else None,
        'complete':bool(rows) and count==len(rows) and all(p['status']=='answered' for p in part_rows),
        'meaning':'逐项回答覆盖，由固定问题、证据充分性及最终模型支持检查共同记录；不是事实准确率或人工验收。'}


def recovery(coverage, checks, *, searches, calls_left, tools_left, attempted=False, empty_draft=False):
    missing=[r for r in coverage['requirements'] if r['answer_status']!='answered']
    if not missing:return {'action':'stop','reason':'all_requirements_answered'}
    if attempted:return {'action':'stop','reason':'repair_already_attempted'}
    if any(r['answer_status']=='conflicted' for r in missing):return {'action':'stop','reason':'conflicting_evidence_requires_resolution'}
    source_missing=[r for r in missing if r['source_status'] in {'missing','unknown'}]
    if source_missing and searches<2 and calls_left>=5 and tools_left>0:
        return {'action':'retrieve','requirement_ids':[r['id'] for r in source_missing],'reason':'source_gap'}
    ready=[r for r in missing if r['source_status']=='supported']
    # A rejected claim is not merely a drafting omission. Do not use a second
    # generation to bypass that rejection or reinterpret missing evidence.
    if ready and ((checks and all(c.get('status')=='supported' for c in checks)) or empty_draft) and calls_left>=4:
        return {'action':'rewrite','requirement_ids':[r['id'] for r in ready],'reason':'supported_evidence_omitted_from_answer'}
    return {'action':'stop','reason':'retrieval_or_verification_budget_exhausted' if source_missing or ready else 'no_supported_repair_path'}


def focused_query(question, needed, ids):
    chosen=[];texts=[]
    for row in needed:
        if row['id'] not in ids:continue
        query=question+'\n重点补全：'+'；'.join(texts+[row['text']])
        if len(query)>2000:continue
        texts.append(row['text']);chosen.append(row['id'])
        if len(chosen)==2:break
    return (question+'\n重点补全：'+'；'.join(texts),chosen) if chosen else (None,[])


def source_gaps(previous, current):
    out=[];seen=set()
    for row in previous+current:
        key=json.dumps(row,sort_keys=True,ensure_ascii=False)
        if key not in seen:seen.add(key);out.append(copy.deepcopy(row))
    return out
