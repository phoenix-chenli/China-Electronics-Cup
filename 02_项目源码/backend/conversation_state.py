"""Branch-local context with explicit patch semantics and attributable slots.

Model interpretations are labelled as such, never promoted to user confirmation.
Original questions and answer snapshots remain in the answer store.
"""
from __future__ import annotations
import copy
import json

from .answer_contract import revision

SCHEMA='zhiheng.conversation-state.v1'
SLOTS={'topic','entities','metric','time_range','dimensions','filters','comparison_base','unit','grain','denominator','assumptions'}
LIST_SLOTS={'entities','dimensions','filters','assumptions'}
LABELS={'topic':'主题','entities':'对象','metric':'指标','time_range':'时间范围','dimensions':'分组',
        'filters':'筛选条件','comparison_base':'比较基期','unit':'单位','grain':'统计粒度','denominator':'分母','assumptions':'用户假设'}


def validate_patch(patch):
    if patch is None:return []
    if not isinstance(patch,list) or len(patch)>24:
        raise ValueError('口径调整最多24项')
    clean=[]
    for op in patch:
        if not isinstance(op,dict) or set(op)-{'op','field','value'} or op.get('op') not in {'add','replace','remove'} or op.get('field') not in SLOTS:
            raise ValueError('口径调整类型或字段无效')
        field=op['field']
        if op['op']=='remove':
            if 'value' in op:raise ValueError('移除口径不接受新值')
        else:
            value=op.get('value')
            if value is None or value=='' or value==[]:
                raise ValueError('口径值不能为空；清空请使用remove')
            if field in LIST_SLOTS and not isinstance(value,list):
                raise ValueError('多值口径必须为列表')
            if not isinstance(value,(str,int,float,list,dict)) or isinstance(value,bool):
                raise ValueError('口径值格式无效')
            try:encoded=json.dumps(value,ensure_ascii=False,allow_nan=False)
            except (TypeError,ValueError):raise ValueError('口径值包含不可表示内容') from None
            if len(encoded)>3000:raise ValueError('单项口径过长')
            if op['op']=='add' and field not in LIST_SLOTS:
                raise ValueError('单值口径请使用replace，不能追加成混合条件')
        clean.append(copy.deepcopy(op))
    return clean


def _independently_reviewed_current_fields(item, status, result):
    """Only a complete, separate field review can supersede old model slots."""
    from .context_intent_adapter import _typed_intent
    from .answer_composer import published_claims
    if (status != 'completed' or item.get('scope_stale') or result.get('scope_stale')
            or (result.get('verification') or {}).get('status') != 'passed'):
        return None
    adaptation=result.get('context_interpretation')
    if (not isinstance(adaptation,dict) or adaptation.get('status')!='passed'
            or adaptation.get('origin')!='model_interpretation' or adaptation.get('confirmed_by_user') is not False
            or adaptation.get('intent')!=item.get('intent')
            or not isinstance(adaptation.get('normalized_question'),str)
            or not adaptation['normalized_question'].strip()
            or adaptation['normalized_question']!=item.get('normalized_question')):
        return None
    try:intent=_typed_intent(adaptation['intent'])
    except (ValueError,TypeError):return None
    if not intent or intent!=adaptation['intent']:return None
    review=adaptation.get('intent_review')
    if (not isinstance(review,dict) or review.get('basis')!='separate_question_interpretation_review'
            or review.get('new_review') is not True or review.get('status')!='passed'
            or review.get('normalized_question_supported') is not True or review.get('overall_supported') is not True):
        return None
    fields=review.get('fields');grounding=review.get('grounding')
    if (not isinstance(fields,list) or len(fields)!=len(intent)
            or any(not isinstance(v,dict) or not isinstance(v.get('field'),str) or v['field'] not in intent or v.get('supported') is not True for v in fields)
            or {v['field'] for v in fields}!=set(intent)
            or review.get('accepted_fields')!=sorted(intent) or review.get('rejected_fields')!=[]
            or not isinstance(grounding,list) or not grounding
            or any(not isinstance(g,dict) or not isinstance(g.get('field'),str) or g['field'] not in intent for g in grounding)
            or {g['field'] for g in grounding}!=set(intent)):
        return None
    claims,issues=published_claims(result,status)
    return set(intent) if claims and not issues else None


def project_reviewed_context_state(item, status=None, result=None, state=None):
    """Return a detached state; omission is unknown, never a user revocation."""
    state=copy.deepcopy(state if state is not None else item.get('context_state'))
    if not isinstance(state,dict):return state
    fields=_independently_reviewed_current_fields(item,status or item.get('status'),
                                                result if result is not None else item.get('result') or {})
    if fields is None:return state
    changed=False
    for field,prior in list((state.get('slots') or {}).items()):
        if (field in fields or field not in SLOTS or not isinstance(prior,dict)
                or prior.get('origin')!='model_interpretation' or prior.get('confirmed_by_user') is not False
                or prior.get('verification') not in ('answer_semantic_review','independent_intent_review')
                or not isinstance(prior.get('source_answer_id'),str)
                or not prior['source_answer_id'] or prior['source_answer_id']==item.get('id')):
            continue
        state['slots'].pop(field);changed=True
        state.setdefault('unknown_fields',{})[field]=dict(origin='independent_intent_review',
            source_answer_id=item['id'],verification='omitted_from_complete_current_interpretation',
            confirmed_by_user=False,previous_slot=copy.deepcopy(prior),
            reason='该旧模型字段不在本轮独立完整解释中；尚未确认当前有效，不代表用户已撤销。')
        state.setdefault('changes',[]).append(dict(field=field,operation='mark_unknown',
            before=copy.deepcopy(prior),after=None,source_answer_id=item['id'],
            basis='complete_independent_current_intent'))
    if changed:
        state.pop('revision',None);state['revision']=revision(state)
    return state


def build_initial(answer_id,question,sources,parent=None,patch=None):
    patch=validate_patch(patch)
    parent_state=project_reviewed_context_state(parent or {})
    slots=copy.deepcopy((parent_state or {}).get('slots') or {})
    # Legacy intent is available for inspection but is not silently imported as
    # a verified or user-confirmed structured state.
    state=dict(schema_version=SCHEMA,answer_id=answer_id,branch_parent_id=(parent or {}).get('id'),
               source_revisions=copy.deepcopy(sources),slots=slots,changes=[],
               unresolved=copy.deepcopy((parent_state or {}).get('unresolved') or []),
               unknown_fields=copy.deepcopy((parent_state or {}).get('unknown_fields') or {}),
               result_reference=(parent_state or {}).get('result_reference'),
               interpretation_available=False,
               legacy_parent=bool(parent and not parent_state))
    for op in patch:
        field=op['field'];old=copy.deepcopy(slots.get(field));value=op.get('value')
        if op['op'] in ('replace','remove'):state['unknown_fields'].pop(field,None)
        if op['op'] in ('replace','remove'):
            resolved=[conflict for conflict in state['unresolved'] if conflict.get('field')==field]
            if resolved:
                state.setdefault('resolved_conflicts',[]).append(dict(field=field,operation=op['op'],
                    source_answer_id=answer_id,resolved_from=copy.deepcopy(resolved),origin='explicit_user_patch'))
                state['unresolved']=[conflict for conflict in state['unresolved'] if conflict.get('field')!=field]
        if op['op']=='remove':
            slots.pop(field,None)
        else:
            if op['op']=='add':
                previous=(old or {}).get('value',[])
                if not isinstance(previous,list):raise ValueError('原口径不是列表，不能追加')
                value=list(previous)
                for candidate in op['value']:
                    if candidate not in value:value.append(candidate)
            slots[field]=dict(value=copy.deepcopy(value),origin='user_patch',source_answer_id=answer_id,
                              user_question=question,confirmed_by_user=True,verification='explicit_request')
        state['changes'].append(dict(field=field,operation=op['op'],before=old,
                                     after=copy.deepcopy(slots.get(field)),source_answer_id=answer_id))
    state['revision']=revision(state)
    return state


def effective_question(question,patch):
    patch=validate_patch(patch)
    if not patch:return question
    instructions=[]
    for op in patch:
        label=LABELS[op['field']]
        if op['op']=='remove':text='移除'+label+'条件'
        elif op['op']=='add':text='在原'+label+'中新增 '+json.dumps(op['value'],ensure_ascii=False)
        else:text='将'+label+'替换为 '+json.dumps(op['value'],ensure_ascii=False)
        instructions.append(text)
    return question+'\n本轮用户明确调整：'+'；'.join(instructions)+'。其余未修改口径沿用当前分支；假设值不作为原始资料事实。'


def _reviewed_limited_sql_intent(item,result):
    """An independently reviewed interpretation is not a numeric verification.

    This is deliberately narrower than allowing every ``limited`` answer. It
    consumes only the already-published SQL result and the adapter's separate
    field review, never planner candidates or diagnostic fallback results.
    """
    from .answer_composer import published_claims
    from .context_intent_adapter import _typed_intent
    from .sql_answer_contract import REVIEW_FIELDS

    if item.get('mode')!='database' or item.get('scope_stale') or result.get('scope_stale'):
        return False
    query=result.get('query')
    rows=result.get('rows')
    if (not isinstance(query,dict) or not isinstance(query.get('sql'),str) or not query['sql'].strip()
            or not isinstance(query.get('parameters'),(list,dict)) or not query.get('used_tables')
            or not isinstance(rows,list) or type(query.get('returned_rows')) is not int
            or query['returned_rows']!=len(rows) or 'executed_result' in query):
        return False
    check=query.get('review_response')
    if query.get('effective_review_source')=='independent_adjudication':
        adjudication=query.get('review_adjudication')
        if (not isinstance(adjudication,dict) or adjudication.get('status')!='received' or adjudication.get('selected_as_effective_review') is not True
                or adjudication.get('passed_remaining_gates') is not True):
            return False
        check=query.get('effective_review_response')
    if (not isinstance(check,dict) or check.get('supported') is not True
            or check.get('interpretation_supported') is not True
            or not isinstance(check.get('scope_review'),dict) or not isinstance(query.get('scope_review'),dict)
            or any(check['scope_review'].get(k) is not True
                   or query['scope_review'].get(k) is not True for k in REVIEW_FIELDS)):
        return False
    mechanical=query.get('mechanical_contract')
    if (not isinstance(mechanical,dict) or mechanical.get('unresolved_issues')!=[]
            or mechanical.get('truncated')):
        return False
    claims,issues=published_claims(result,'completed')
    if issues or not claims:return False
    used={identifier for claim in claims for identifier in claim['evidence_ids']}
    if any(e.get('stale') or e.get('source_id')!=item.get('source_id')
           or not e.get('sha256') or e['sha256']!=item.get('source_sha256')
           or not e.get('parser_version') or e['parser_version']!=item.get('parser_version')
           for e in result.get('evidence',[]) if e.get('id') in used):
        return False
    adaptation=result.get('context_interpretation')
    if (not isinstance(adaptation,dict) or adaptation.get('status')!='passed'
            or adaptation.get('origin')!='model_interpretation'
            or adaptation.get('confirmed_by_user') is not False
            or adaptation.get('intent')!=item.get('intent')
            or not isinstance(adaptation.get('normalized_question'),str)
            or not adaptation['normalized_question'].strip()
            or adaptation['normalized_question']!=item.get('normalized_question')):
        return False
    try:intent=_typed_intent(adaptation.get('intent'))
    except ValueError:return False
    if not intent or intent!=adaptation['intent']:return False
    review=adaptation.get('intent_review')
    if (not isinstance(review,dict) or review.get('basis')!='separate_question_interpretation_review' or review.get('new_review') is not True
            or review.get('normalized_question_supported') is not True
            or type(review.get('overall_supported')) is not bool
            or review.get('status') not in ('passed','partially_supported')):
        return False
    verdicts=review.get('fields')
    if (not isinstance(verdicts,list) or not verdicts
            or any(not isinstance(v,dict) or not isinstance(v.get('field'),str) or v['field'] not in SLOTS or type(v.get('supported')) is not bool for v in verdicts)
            or len({v['field'] for v in verdicts})!=len(verdicts)):
        return False
    accepted={v['field'] for v in verdicts if v['supported'] is True}
    rejected={v['field'] for v in verdicts if v['supported'] is False}
    if (accepted!=set(intent) or sorted(accepted)!=review.get('accepted_fields')
            or sorted(rejected)!=review.get('rejected_fields')
            or (not rejected and (review['overall_supported'] is not True or review['status']!='passed'))
            or (rejected and review['status']!='partially_supported')):
        return False
    grounding=review.get('grounding')
    return (isinstance(grounding,list) and bool(grounding)
            and all(isinstance(g,dict) and isinstance(g.get('field'),str) and g['field'] in accepted for g in grounding)
            and {g['field'] for g in grounding}==accepted)


def finalize_state(item,status,result):
    state=copy.deepcopy(item.get('context_state') or build_initial(item['id'],item['question'],item.get('sources',[])))
    state=project_reviewed_context_state(item,status,result,state)
    verified=status=='completed' and (result.get('verification') or {}).get('status')=='passed'
    reviewed_limited=(status=='completed' and (result.get('verification') or {}).get('status')=='limited'
                      and _reviewed_limited_sql_intent(item,result))
    intent=item.get('intent') if isinstance(item.get('intent'),dict) else {}
    adaptation=result.get('context_interpretation') or {}
    review=adaptation.get('intent_review') or {}
    # Retaining supported fields must not resurrect an old value for a field
    # explicitly rejected in this turn. Unknown is not an implicit user removal.
    if (status in ('completed','insufficient_evidence') and adaptation.get('origin')=='model_interpretation'
            and review.get('normalized_question_supported') is True):
        explicitly_changed={p['field'] for p in item.get('context_patch',[]) if p['op']!='remove'}
        eligible=set(review.get('rejected_fields') or [])|set(review.get('omitted_ungrounded_fields') or [])
        for field in adaptation.get('unknown_fields') or []:
            if field not in SLOTS or field not in eligible:continue
            prior=state['slots'].get(field)
            if field in explicitly_changed or (prior or {}).get('origin')=='user_patch':
                conflict=dict(field=field,kind='explicit_patch_interpretation_unverified' if field in explicitly_changed
                              else 'prior_user_patch_not_reviewed_for_current_question',source_answer_id=item['id'])
                if field not in explicitly_changed:conflict['previous_source_answer_id']=prior.get('source_answer_id')
                state['unresolved'].append(conflict)
                continue  # A model veto cannot repeal the user's explicit patch.
            prior=state['slots'].pop(field,None)
            unknown=dict(origin='model_interpretation',source_answer_id=item['id'],
                         verification='field_not_verified_for_current_question',confirmed_by_user=False,
                         previous_source_answer_id=(prior or {}).get('source_answer_id'))
            unbound=[copy.deepcopy(c) for c in adaptation.get('unbound_conditions') or []
                     if isinstance(c,dict) and c.get('field')==field]
            if unbound:unknown.update(binding_status='unbound',conditions=unbound)
            state.setdefault('unknown_fields',{})[field]=unknown
            state['changes'].append(dict(field=field,operation='mark_unknown',before=copy.deepcopy(prior),
                                         after=None,source_answer_id=item['id']))
    # A model's semantic vote (or an explicit business phrase) cannot establish
    # a database identifier. Recheck at persistence as well, including inherited
    # bindings. Preserve the requested condition in an unbound audit slot.
    if status in ('completed','insufficient_evidence'):
        from .context_intent_adapter import _binding_review, _unbound_conditions
        candidates={k:v.get('value') for k,v in state['slots'].items() if isinstance(v,dict)}
        candidates.update(intent)
        binding=_binding_review(item,result,candidates)
        if binding['rejected_fields']:
            state['schema_binding']=binding
            for condition in _unbound_conditions(item,candidates,review.get('grounding') or [],binding):
                field=condition['field'];prior=state['slots'].pop(field,None)
                state.setdefault('unknown_fields',{})[field]=dict(origin='schema_binding_check',
                    source_answer_id=item['id'],verification='database_identifier_unbound',
                    confirmed_by_user=False,binding_status='unbound',conditions=[condition],
                    previous_slot=copy.deepcopy(prior))
                state['changes'].append(dict(field=field,operation='mark_unbound',before=copy.deepcopy(prior),
                    after=None,source_answer_id=item['id']))
            intent={k:v for k,v in intent.items() if k not in binding['rejected_fields']}
    if (item.get('mode')=='database' and status in ('completed','insufficient_evidence')
            and (result.get('verification') or {}).get('status')=='limited'):
        # An old interpretation remains retrievable from its original turn, but
        # omitted or unreviewed fields are not the current question's meaning.
        current_fields=set(intent) if reviewed_limited else set()
        for field,prior in list(state['slots'].items()):
            if field in current_fields:continue
            if prior.get('origin')=='user_patch':
                if prior.get('source_answer_id')!=item['id']:
                    conflict=dict(field=field,kind='prior_user_patch_not_reviewed_for_current_question',
                                  source_answer_id=item['id'],previous_source_answer_id=prior.get('source_answer_id'))
                    if conflict not in state['unresolved']:state['unresolved'].append(conflict)
                continue  # An absent model field does not revoke a user instruction.
            state['slots'].pop(field)
            state.setdefault('unknown_fields',{})[field]=dict(origin='model_interpretation',source_answer_id=item['id'],
                verification='field_not_verified_for_current_question',confirmed_by_user=False,
                previous_source_answer_id=prior.get('source_answer_id'))
            state['changes'].append(dict(field=field,operation='mark_unknown',before=copy.deepcopy(prior),
                                         after=None,source_answer_id=item['id']))
        state['interpretation_status']='independently_reviewed' if reviewed_limited else 'unavailable'
        if not reviewed_limited:
            if state.get('result_reference'):
                state['previous_result_reference']=copy.deepcopy(state['result_reference'])
            state['result_reference']=None
            state['interpretation_available']=False
    if verified or reviewed_limited:
        explicit_removals={p['field'] for p in item.get('context_patch',[]) if p['op']=='remove'}
        for field,value in intent.items():
            if field not in SLOTS or value is None:continue
            state.setdefault('unknown_fields',{}).pop(field,None)
            if field in explicit_removals:
                if value not in ('',[],None):
                    state['unresolved'].append(dict(field=field,kind='removed_condition_reintroduced',source_answer_id=item['id']))
                continue
            prior=state['slots'].get(field)
            if prior and prior.get('origin')=='user_patch' and prior.get('source_answer_id')==item['id']:
                # Preserve the explicit user value; a planner cannot rewrite it
                # into a different confirmed request after the fact.
                if prior['value']!=value:
                    state['unresolved'].append(dict(field=field,kind='interpretation_differs_from_user_patch',
                                                     source_answer_id=item['id'],candidate=copy.deepcopy(value)))
                continue
            if prior and prior.get('value')==value:continue
            slot=dict(value=copy.deepcopy(value),origin='model_interpretation',confirmed_by_user=False,
                      source_answer_id=item['id'],user_question=item['question'],
                      normalized_question=item.get('normalized_question'),verification='answer_semantic_review')
            if reviewed_limited:
                slot.update(verification='independent_intent_review',answer_verification_status='limited')
            if value==[] or value=='':
                # An empty typed list is an explicit cleared interpretation;
                # null is unknown and was skipped above.
                state['slots'].pop(field,None);after=None;operation='remove'
            else:
                state['slots'][field]=slot;after=slot;operation='replace' if prior else 'add'
            state['changes'].append(dict(field=field,operation=operation,before=copy.deepcopy(prior),
                                         after=copy.deepcopy(after),source_answer_id=item['id']))
        state['result_reference']=dict(answer_id=item['id'],claim_ids=[c['id'] for c in result.get('claims',[]) if c.get('id')],
                                       evidence_ids=[e['id'] for e in result.get('evidence',[]) if e.get('id')])
        state['pending_clarification']=None
        state['interpretation_available']=any(k in SLOTS and v is not None for k,v in intent.items())
    elif status=='needs_clarification':
        state['pending_clarification']=dict(answer_id=item['id'],detail=copy.deepcopy(result.get('clarification')))
    state.pop('revision',None);state['revision']=revision(state)
    return state


def context_history(chain,max_chars=60000,recent_turns=8):
    """Loss-aware compression. Small histories retain the legacy wire shape."""
    history=[]
    for turn in chain:
        result=turn.get('result') or {};query=result.get('query') or {}
        # Legacy snapshots remain immutable; their invalid SQL bindings are not
        # reintroduced as a confirmed interpretation in the model's history.
        from .context_intent_adapter import _binding_review, _unbound_conditions
        intent=turn.get('intent')
        binding=_binding_review(turn,result,intent) if isinstance(intent,dict) else {'rejected_fields':[]}
        unbound=[]
        if binding['rejected_fields']:
            unbound=_unbound_conditions(turn,intent,[],binding)
            intent={k:v for k,v in intent.items() if k not in binding['rejected_fields']}
        history.append(dict(question=effective_question(turn['question'],turn.get('context_patch')),
                            original_question=turn['question'],
                            answer_id=turn.get('id'),
                            normalized_question=turn.get('normalized_question'),intent=intent,
                            status=turn['status'],clarification=result.get('clarification'),
                            answer=result.get('text','')[:1600],columns=result.get('columns'),rows=result.get('rows') or [],
                            result_truncated=query.get('truncated',False),
                            history_projection={'answer_text_truncated':len(result.get('text',''))>1600,'rows_omitted':0,'retrieve_by_answer_id':turn.get('id')},
                            query={k:query[k] for k in ('sql','parameters','plan') if k in query}))
        if unbound:history[-1]['unbound_conditions']=unbound
    if len(json.dumps(history,ensure_ascii=False))<=max_chars and len(history)<=30:return history
    state=project_reviewed_context_state(chain[-1] if chain else {})
    if not state or not state.get('interpretation_available') or state.get('legacy_parent') or state.get('unresolved'):
        raise ValueError('本轮上文较长且旧口径未完整结构化，请用完整条件开始新问题')
    latest=chain[-1]
    values={k:v.get('value') for k,v in state.get('slots',{}).items() if isinstance(v,dict)}
    binding=_binding_review(latest,latest.get('result') or {},values)
    if binding['rejected_fields']:
        state=copy.deepcopy(state)
        for condition in _unbound_conditions(latest,values,[],binding):
            field=condition['field'];prior=state['slots'].pop(field,None)
            state.setdefault('unknown_fields',{})[field]=dict(binding_status='unbound',
                confirmed_by_user=False,conditions=[condition],previous_slot=prior,
                verification='legacy_snapshot_identifier_unbound')
        state.pop('revision',None);state['revision']=revision(state)
    # Preserve all active slots and their source turns. Large prior result tables
    # are not silently retained in memory; the report states exact omitted IDs.
    kept=history[-recent_turns:]
    for h,turn in zip(kept,chain[-recent_turns:]):
        h['answer_id']=turn['id']
        h['history_projection']=dict(rows_omitted=max(0,len(h['rows'])-8),answer_text_truncated=len((turn.get('result') or {}).get('text',''))>1600,
                                      retrieve_by_answer_id=turn['id'])
        h['rows']=h['rows'][:8]
    memory=dict(kind='structured_context',question='此前分支的已记录口径（不是新的用户指令）',
                normalized_question=None,intent=None,status='context_snapshot',clarification=state.get('pending_clarification'),
                answer='',columns=None,rows=[],query={},result_truncated=False,
                context_state=state,omitted_answer_ids=[t['id'] for t in chain[:-recent_turns]],
                boundary='保留全部当前槽位及原轮次出处；未列出的历史答案/原表必须按ID重新读取，不能推测其数值。')
    compressed=[memory,*kept]
    if len(json.dumps(compressed,ensure_ascii=False))>max_chars:
        raise ValueError('当前口径与近期证据仍超过上下文容量，请缩小资料或明确需要保留的条件')
    return compressed
