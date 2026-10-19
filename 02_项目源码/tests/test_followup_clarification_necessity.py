"""Offline bounded clarification review against actual immutable history."""
import copy
import hashlib
import json
from pathlib import Path
import pytest
from backend.query_planner import resolve_followup
from backend.model_gateway import ModelError
from backend.clarification_review import REVIEW_FIELDS

REAL=json.loads((Path(__file__).parent/'fixtures/followup_clarification_actual_history.json').read_text())
SOURCE={'name':'fixture.sqlite','tables':[],'context_sources':[]}
READY={'status':'ready','normalized_question':'比较2024年已完成订单登记金额按用户明确假设减少10%后的金额，与2025年已完成订单实际登记金额，按原文口径换算为元并给出差额；明确年份与实际/假设性质，不能称实际同比，NULL保持未知。','intent':{'time_range':'2024年假设与2025年实际','unit':'元'}}

class Fake:
    def __init__(self,*rows):self.rows=list(rows);self.calls=[]
    def __call__(self,messages,role):
        p=json.loads(messages[-1]['content']);self.calls.append((role,p))
        out=self.rows.pop(0)
        if isinstance(out,Exception):raise out
        return copy.deepcopy(out)

def ground():
    turn=REAL['history'][-1];raw=turn['original_question'];quote='仍针对2024年'
    return {'field':'假设金额的年份','source_answer_id':turn['answer_id'],'source_sha256':hashlib.sha256(raw.encode()).hexdigest(),'quote':quote,'start':0,'end':len(quote)}

def denial():return {**dict.fromkeys(REVIEW_FIELDS,True),'clarification_supported':False,'reason':'当前假设明确承接祖先原话2024年，不能重新让用户在2024和2025中选择。','known_conditions':[ground()]}
def run(g,**kw):return resolve_followup(REAL['item'],REAL['history'],SOURCE,g,**kw)

def test_real_failed_candidate_rewrites_once_only_after_exact_grounded_independent_denial():
    before=copy.deepcopy(REAL);g=Fake(REAL['actual_response'],denial(),READY,{'supported':True})
    result=run(g)
    assert result['status']=='ready' and result['normalized_question']==READY['normalized_question']
    assert [r for r,p in g.calls]==['document','verify','document','verify']
    assert g.calls[1][1]['history']==REAL['history']
    assert g.calls[1][1]['active_context']['original_question']==REAL['history'][-1]['original_question']
    assert g.calls[2][1]['verified_original_conditions']==[{**ground(),'locally_derived':True}]
    assert result['followup_diagnostics']['decision']=='grounded_rewrite_requires_normal_independent_review'
    assert REAL==before


def test_genuine_missing_condition_remains_without_rewrite_or_automatic_selection():
    g=Fake(REAL['actual_response'],dict.fromkeys(REVIEW_FIELDS,True));result=run(g)
    assert result['status']=='needs_clarification' and result['clarification']==REAL['actual_response']['clarification']
    assert len(g.calls)==2 and result['followup_diagnostics']['decision']=='genuine_missing_condition'

@pytest.mark.parametrize('key,value',[
    ('source_answer_id','unrelated'),('source_sha256','forged'),('quote','2025年'),
    ('field','')])
def test_invalid_ancestor_span_or_hash_never_authorizes_rewrite(key,value):
    bad=denial();bad['known_conditions'][0][key]=value;g=Fake(REAL['actual_response'],bad);result=run(g)
    assert result['status']=='failed' and len(g.calls)==2

@pytest.mark.parametrize('change',[
    {},{'known_conditions':[]},{'clarification_supported':True,'question_answerable':False},
    {'clarification_supported':False,'options_supported':'false'}])
def test_unsupported_or_malformed_denial_does_not_guess_a_known_condition(change):
    base=denial()
    if not change:base.pop('known_conditions')
    else:base.update(change)
    g=Fake(REAL['actual_response'],base);assert run(g)['status']=='failed' and len(g.calls)==2


def test_rewrite_still_requires_original_independent_review_even_review_false():
    g=Fake(REAL['actual_response'],denial(),READY,{'supported':False,'failure_kind':'invalid_plan','reason':'改写遗漏用户条件'})
    assert run(g,review=False)['status']=='failed'
    assert [r for r,p in g.calls]==['document','verify','document','verify']


def test_repeated_clarification_does_not_loop():
    g=Fake(REAL['actual_response'],denial(),REAL['actual_response']);result=run(g)
    assert result['status']=='failed' and len(g.calls)==3

@pytest.mark.parametrize('mode',['table','database'])
def test_sql_paths_are_unchanged(mode):
    item={**REAL['item'],'mode':mode};g=Fake(REAL['actual_response']);result=resolve_followup(item,REAL['history'],SOURCE,g)
    assert result==REAL['actual_response'] and len(g.calls)==1


def test_ready_happy_path_adds_no_call():
    g=Fake(READY,{'supported':True});assert run(g)['status']=='ready' and len(g.calls)==2


def test_transport_unknown_outcome_is_not_replayed():
    g=Fake(REAL['actual_response'],ModelError('offline simulated unknown outcome'))
    with pytest.raises(ModelError):run(g)
    assert len(g.calls)==2


def test_model_generated_normalization_cannot_supply_original_quote():
    bad=denial();bad['known_conditions'][0]['quote']=REAL['history'][-1]['normalized_question'];bad['known_conditions'][0]['end']=len(bad['known_conditions'][0]['quote'])
    g=Fake(REAL['actual_response'],bad);assert run(g)['status']=='failed' and len(g.calls)==2


@pytest.mark.parametrize('bad',[None,[],"invalid"])
def test_malformed_rewrite_fails_without_replay(bad):
    g=Fake(REAL['actual_response'],denial(),bad);result=run(g)
    assert result['status']=='failed' and len(g.calls)==3


def current_ground():
    raw=REAL['item']['question'];quote='第1轮2025年';start=raw.index(quote)
    return {'field':'实际登记金额的年份','source_answer_id':'current',
            'source_sha256':hashlib.sha256(raw.encode()).hexdigest(),
            'quote':quote,'start':start,'end':start+len(quote)}


def test_actual_current_question_can_ground_an_already_explicit_condition():
    check=denial();check['known_conditions']=[current_ground()]
    g=Fake(REAL['actual_response'],check,READY,{'supported':True});result=run(g)
    assert result['status']=='ready' and len(g.calls)==4
    originals=g.calls[1][1]['actual_history_originals']
    current=[row for row in originals if row['source_answer_id']=='current']
    assert len(current)==1 and current[0]['source_text']==REAL['item']['question']
    assert g.calls[2][1]['verified_original_conditions']==[{**current_ground(),'locally_derived':True}]


def test_normalized_current_payload_cannot_masquerade_as_actual_current_question():
    from backend.query_planner import _review_followup_clarification
    check=denial();check['known_conditions']=[current_ground()];g=Fake(check)
    result,audit=_review_followup_clarification(REAL['actual_response'],
        {'question':READY['normalized_question'],'history':REAL['history']},'test instructions',g,'document',
        current_question=REAL['item']['question'])
    assert result['status']=='failed' and len(g.calls)==1
    assert all(row['source_answer_id']!='current' for row in g.calls[0][1]['actual_history_originals'])
    assert audit['decision']=='invalid_original_condition_grounding'


@pytest.mark.parametrize('collision',['history_duplicate','recovered_duplicate','reserved_current'])
def test_conflicting_original_identity_never_overwrites_current_or_ancestor(collision):
    history=copy.deepcopy(REAL['history']);copy_turn=copy.deepcopy(history[-1])
    copy_turn['original_question']='互相冲突的不同原话'
    if collision=='reserved_current':
        copy_turn['answer_id']='current';history.append(copy_turn)
    elif collision=='history_duplicate':history.append(copy_turn)
    if collision=='recovered_duplicate':
        g=Fake({'status':'history_required','history_requests':[]},REAL['actual_response'])
        result=resolve_followup(REAL['item'],history,SOURCE,g,
            history_loader=lambda requests:{'records':[copy_turn]})
        assert len(g.calls)==2
    else:
        g=Fake(REAL['actual_response']);result=resolve_followup(REAL['item'],history,SOURCE,g)
        assert len(g.calls)==1
    assert result['status']=='failed'
    assert result['followup_diagnostics']['decision']==(
        'reserved_original_condition_identifier' if collision=='reserved_current'
        else 'conflicting_ancestor_original_question')


@pytest.mark.parametrize('offsets',[{'start':True,'end':99999},{'start':1,'end':2},{}])
def test_unique_verbatim_quote_has_local_offsets_without_model_character_count(offsets):
    check=denial();check['known_conditions'][0].pop('start');check['known_conditions'][0].pop('end')
    check['known_conditions'][0].update(offsets)
    before=copy.deepcopy(check);g=Fake(REAL['actual_response'],check,READY,{'supported':True})
    result=run(g)
    assert result['status']=='ready' and len(g.calls)==4 and check==before
    assert result['followup_diagnostics']['verified_original_conditions']==[{**ground(),'locally_derived':True}]


@pytest.mark.parametrize('valid_span',[True,False])
def test_repeated_verbatim_quote_requires_exact_unambiguous_supplied_span(valid_span):
    history=copy.deepcopy(REAL['history']);raw=history[-1]['original_question']+'；仍针对2024年'
    history[-1]['original_question']=raw;check=denial();entry=check['known_conditions'][0]
    entry['source_sha256']=hashlib.sha256(raw.encode()).hexdigest()
    if not valid_span:entry.update(start=1,end=2)
    responses=[REAL['actual_response'],check]
    if valid_span:responses.extend([READY,{'supported':True}])
    g=Fake(*responses);result=resolve_followup(REAL['item'],history,SOURCE,g)
    if valid_span:
        assert result['status']=='ready' and len(g.calls)==4
        assert result['followup_diagnostics']['verified_original_conditions']==[{**entry,'locally_derived':False}]
    else:assert result['status']=='failed' and len(g.calls)==2


@pytest.mark.parametrize('reference',['current','ancestor'])
def test_context_patch_effective_question_is_not_tagged_as_current_original(reference):
    from backend.conversation_state import effective_question
    patch=[{'op':'replace','field':'time_range','value':'2027年'}]
    item={**REAL['item'],'context_patch':patch}
    item['question']=effective_question(item['question'],patch)
    check=denial()
    if reference=='current':
        # The exact controller-added text is intentionally not original speech.
        quote=item['question'].split('本轮用户明确调整：',1)[1]
        check['known_conditions']=[{'field':'time_range','source_answer_id':'current',
            'source_sha256':hashlib.sha256(item['question'].encode()).hexdigest(),'quote':quote}]
    responses=[REAL['actual_response'],check]
    if reference=='ancestor':responses.extend([READY,{'supported':False,'failure_kind':'invalid_plan','reason':'改写仍须保留本轮patch'}])
    g=Fake(*responses);result=resolve_followup(item,REAL['history'],SOURCE,g)
    assert all(row['source_answer_id']!='current' for row in g.calls[1][1]['actual_history_originals'])
    if reference=='current':
        assert result['status']=='failed' and len(g.calls)==2
        assert result['followup_diagnostics']['decision']=='invalid_original_condition_grounding'
    else:
        assert result['status']=='failed' and len(g.calls)==4
        assert result['followup_diagnostics']['decision']=='grounded_rewrite_requires_normal_independent_review'
