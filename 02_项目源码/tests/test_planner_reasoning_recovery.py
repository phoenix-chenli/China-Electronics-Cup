"""Real local controller progress governs planner effort; zero remote calls."""
import copy
import pytest
from backend import cross_source_engine as engine
from test_cross_source_engine import sources, actions, search, sql, calculation, completed, approved
from test_cross_planner_state import ReasoningScripted, image_source, image_action
from test_cross_tool_resources import candidate


def run(sources,gateway,**kw):
    audit=[]
    status,result=engine.answer_cross_source(sources,'林禾的优惠金额是多少元？',[],gateway,audit=audit.append,**kw)
    events=audit[-1]['audit_history'] if audit else []
    return status,result,events


def restore_events(events):
    return [event['record'] for event in events if event['kind']=='planner_reasoning_restored_default']


def test_real_calculation_progress_releases_high_and_preserves_independent_review(sources):
    g=ReasoningScripted(actions(sql(),search()),{},actions(calculation()),completed(),approved())
    status,result,events=run(sources,g)
    assert status=='completed' and result['calculations'][0]['display_value']=='6000.00'
    assert g.methods==[('table','__call__',None),('table','__call__',None),
        ('table','call_with_reasoning','high'),('table','__call__',None),('verify','__call__',None)]
    restored=restore_events(events)
    assert len(restored)==1 and restored[0]['advanced_dimensions']==['calculations']
    assert restored[0]['previous_progress_marker'][2]==0 and restored[0]['current_progress_marker'][2]==1
    assert result['verification']['tool_actions']==3
    assert result['verification']['planning_progress']['max_consecutive_no_progress']==3
    assert g.calls[0]['payload']['tool_limits']['max_actions']==32


def test_new_stagnation_can_escalate_again_after_real_progress(sources):
    g=ReasoningScripted({},actions(search()),{},candidate(),approved())
    status,result,events=run(sources,g)
    assert status=='completed'
    assert g.methods==[('table','__call__',None),('table','call_with_reasoning','high'),
        ('table','__call__',None),('table','call_with_reasoning','high'),('verify','__call__',None)]
    assert len(restore_events(events))==1
    assert len([event for event in events if event['kind']=='planner_reasoning_escalated'])==2


@pytest.mark.parametrize('self_approval',[False,True])
def test_alias_ids_and_claimed_progress_do_not_restore_or_reset_stop(sources,self_approval):
    aliases=[actions(sql('alias1')),actions(sql('alias2'))]
    if self_approval:
        for response in aliases:
            response.update(progress=True,all_evidence_ready=True,planning_progress={'productive_rounds':999})
    g=ReasoningScripted(actions(sql()),{},*aliases)
    status,result,events=run(sources,g)
    assert status=='insufficient_evidence' and result['verification']['reason']=='no_progress'
    assert g.methods==[('table','__call__',None),('table','__call__',None)]+[('table','call_with_reasoning','high')]*2
    assert restore_events(events)==[]
    assert result['verification']['tool_actions']==1
    assert result['verification']['planning_progress']['consecutive_no_progress']==3
    assert not result['claims']


def test_first_original_page_restores_but_review_is_still_required(sources,monkeypatch):
    image_source(sources)
    reads=[]
    def pixels(source,page):
        reads.append((source['id'],page))
        return [{'type':'image_url','image_url':{'url':'data:image/png;base64,AA=='}}]
    monkeypatch.setattr('backend.visual_context.page_content',pixels)
    g=ReasoningScripted(actions(search('S1')),{},actions(image_action()),candidate(),approved(visual_supported=True))
    status,result,events=run(sources,g)
    assert status=='completed' and reads==[('rule',1)]
    assert 'original_pages' in restore_events(events)[0]['advanced_dimensions']
    assert g.methods[-2:]==[('table','__call__',None),('verify','__call__',None)]


def test_default_restoration_does_not_publish_after_independent_refusal(sources):
    reject=approved(supported=False,answer_complete=False,claims=[{'id':'C1','supported':False}])
    g=ReasoningScripted({},actions(search()),candidate(),reject,candidate(),reject)
    status,result,events=run(sources,g)
    assert status!='completed' and not result['claims']
    assert len(restore_events(events))==1
    assert [call for call in g.methods if call[0]=='verify']==[('verify','__call__',None)]*2



def test_same_calculation_content_with_new_action_id_does_not_restore_high(sources):
    duplicate=calculation();duplicate['id']='new_action_id'
    g=ReasoningScripted(actions(sql(),search()),actions(calculation()),{},actions(duplicate),completed(),approved())
    status,result,events=run(sources,g)
    assert status=='completed'
    assert g.methods[-3:]==[('table','call_with_reasoning','high')]*2+[('verify','__call__',None)]
    assert restore_events(events)==[]
    assert [event['record']['calculation_id'] for event in events
            if event['kind']=='calculation_mechanically_executed']==['K1','K2']
    assert result['verification']['tool_actions']==4


def test_relabelled_calculation_dependency_chain_is_not_new_substantive_progress(sources):
    dependent={'id':'D1','kind':'calculate','expression':'x','bindings':{'x':{'calculation_id':'K1','unit':'元'}},
        'output_unit':'元','precision':2,'rule_evidence_ids':['E2'],'depends_on':['S3']}
    base_alias=calculation();base_alias['id']='S4'
    dependent_alias=copy.deepcopy(dependent);dependent_alias.update(id='D2',depends_on=['S4'])
    dependent_alias['bindings']['x']['calculation_id']='K3'
    g=ReasoningScripted(actions(sql(),search()),actions(calculation()),actions(dependent),{},
        actions(base_alias),actions(dependent_alias),completed(),approved())
    status,result,events=run(sources,g)
    assert status=='completed' and restore_events(events)==[]
    assert g.methods[-4:]==[('table','call_with_reasoning','high')]*3+[('verify','__call__',None)]
    assert [event['record']['calculation_id'] for event in events
            if event['kind']=='calculation_mechanically_executed']==['K1','K2','K3','K4']
