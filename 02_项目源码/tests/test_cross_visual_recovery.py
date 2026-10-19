"""Actual saved candidate projection + scripted independent verdicts; zero model API."""
import copy,hashlib,json
from pathlib import Path
import pytest
from backend.cross_source_engine import answer_cross_source
from backend.document_engine import Chunk
from backend.model_gateway import ModelError
from backend.reasoning_tools import ReasoningLimits
FIXTURE=Path(__file__).parent/'fixtures/ctx04-t7-terminal-projection.json'
class Gateway:
    def __init__(self,values):self.values=copy.deepcopy(values);self.calls=[]
    def __call__(self,messages,purpose):
        self.calls.append((purpose,messages));value=copy.deepcopy(self.values.pop(0))
        if isinstance(value,BaseException):raise value
        if value.get('claims') and purpose=='table':
            raw=messages[-1]['content'];payload=json.loads(raw[0]['text'] if isinstance(raw,list) else raw)
            current=payload.get('evidence',[])
            # The production row/prose projection changes runtime E numbers.
            # Relocate this historical candidate only by its immutable old
            # source page/range and real selected cell, never by its answer value.
            old={e['id']:e for e in json.loads(FIXTURE.read_text())['evidence']}
            for claim in value['claims']:
                ids=[]
                for eid in claim.get('evidence_ids',[]):
                    prior=old.get(eid)
                    matches=[e['id'] for e in current if prior and e.get('page')==prior['page']
                             and prior['start']<=e.get('start',-1)<e.get('end',-1)<=prior['end']]
                    ids.extend(matches or [eid])
                claim['evidence_ids']=list(dict.fromkeys(ids))
                for binding in claim.get('table_bindings',[]):
                    prior=old.get(binding.get('evidence_id'))
                    matches=[e['id'] for e in current if prior and e.get('page')==prior['page']
                        and any(t.get('table_name')==binding.get('table_name')
                            and binding.get('cell_id') in t.get('selected_cell_ids',[])
                            for t in e.get('table_context',{}).get('tables',[]))]
                    if len(matches)==1:binding['evidence_id']=matches[0]
        return value
class SelectedOriginalChunks:
    def __init__(self,entries):self.chunks=[Chunk(e['id'],e['page'],e['start'],e['end'],e['quote']) for e in entries]
    def prepared(self):return self.chunks,False
    def select(self,*a,pages=None,**kw):return [c for c in self.chunks if pages is None or c.page in pages],False
    def audit(self):return {'scope':'controlled replay of original selected spans; not a new retrieval accuracy result'}
def setup():
    f=json.loads(FIXTURE.read_text());source=copy.deepcopy(f['source'])
    source['original_path']=str(Path(__file__).parent/'fixtures/multimodal/石景山区2024年统计公报.pdf')
    assert hashlib.sha256(Path(source['original_path']).read_bytes()).hexdigest()==f['source_sha256']
    vals=[{'actions':[{'id':'S1','kind':'search','source_id':source['id'],'query':'工业 增加值 不变价格','depends_on':[]}]}]
    for a in f['candidate_audit']:
        claims=[]
        for original in a['record']['claims']:
            c={k:copy.deepcopy(v) for k,v in original.items() if k in {'text','evidence_ids','calculation_ids','table_bindings'}}
            for b in c.get('table_bindings',[]):b.pop('binding_index',None)
            claims.append(c)
        vals.append({'status':'completed','claims':claims})
    return f,source,vals,SelectedOriginalChunks(f['evidence'])
def approved(value=True):
    return {'supported':value,'answer_complete':value,'visual_supported':value,'conflict':False,
        'claims':[{'id':'C1','supported':value},{'id':'C2','supported':value}],
        'table_relations':[{'id':'C1','supported':value},{'id':'C2','supported':value}],
        'coverage':[{'question_id':'Q1','status':'answered' if value else 'unknown','claim_ids':['C1','C2'] if value else []}]}
def run(values,source,f,retriever,**kwargs):
    gateway=Gateway(values);status,result=answer_cross_source([source],f['question'],[],gateway,retrieval_factory=lambda _:retriever,**kwargs)
    return status,result,gateway

def test_saved_repaired_candidate_reaches_real_pages_and_independent_review(monkeypatch):
    from backend import visual_context
    f,source,values,retriever=setup();original=visual_context.page_content;read=[]
    def observed(s,p):read.append((s['sha256'],p));return original(s,p)
    monkeypatch.setattr(visual_context,'page_content',observed)
    before=copy.deepcopy(source);status,result,gateway=run(values+[approved()],source,f,retriever)
    assert status=='completed'
    assert read==[(f['source_sha256'],2),(f['source_sha256'],3)]
    assert [c['text'] for c in result['claims']]==[c['text'] for c in values[-1]['claims']]
    assert [p for p,_ in gateway.calls]==['table']*6+['verify']
    assert source==before
    review=gateway.calls[-1][1][-1]['content'];assert isinstance(review,list)
    assert sum(x.get('type')=='image_url' for x in review)==2
    assert result['verification']['tool_actions']==3
    assert any(a['stage']=='visual_required' for a in result['diagnostics']['candidate_attempts'])
    assert any(a['kind']=='visual_recovery_scheduled' for a in result['diagnostics']['protocol_recoveries'])
@pytest.mark.parametrize('limits',[ReasoningLimits(max_actions=1),ReasoningLimits(max_image_pages=1)])
def test_insufficient_action_or_image_budget_never_reads_or_publishes(monkeypatch,limits):
    f,s,v,r=setup();monkeypatch.setattr('backend.visual_context.page_content',lambda *_:pytest.fail('budget before rendering'))
    status,result,g=run(v,s,f,r,limits=limits)
    assert status=='insufficient_evidence' and result['claims']==[] and not any(p=='verify' for p,_ in g.calls)
def test_local_original_read_failure_does_not_repeat_or_publish(monkeypatch):
    f,s,v,r=setup();reads=[]
    def fail(*a):reads.append(a[1]);raise OSError('unreadable original')
    monkeypatch.setattr('backend.visual_context.page_content',fail)
    status,result,g=run(v,s,f,r)
    assert status!='completed' and result['claims']==[] and reads==[2] and len(g.calls)==6
def test_false_visual_verdict_stays_false_and_unpublished():
    f,s,v,r=setup();status,result,g=run(v+[approved(False),v[-1],approved(False)],s,f,r)
    assert status=='failed' and not result['claims']
    assert [p for p,_ in g.calls].count('verify')==2
    assert any(a['review']['visual_supported'] is False for a in result['diagnostics']['received_reviews'])
def test_unknown_verify_post_is_not_replayed():
    f,s,v,r=setup();gateway=Gateway(v+[ModelError('unknown response',code='transport_failed')])
    with pytest.raises(ModelError):answer_cross_source([s],f['question'],[],gateway,retrieval_factory=lambda _:r)
    assert [p for p,_ in gateway.calls].count('verify')==1
def test_cancel_after_first_actual_page_stops_before_second_page_or_verify(monkeypatch):
    f,s,v,r=setup();state={'cancel':False};reads=[]
    def pixels(source,page):reads.append(page);state['cancel']=True;return [{'type':'image_url','image_url':{'url':'data:image/png;base64,AA=='}}]
    monkeypatch.setattr('backend.visual_context.page_content',pixels);gateway=Gateway(v)
    with pytest.raises(InterruptedError):answer_cross_source([s],f['question'],[],gateway,retrieval_factory=lambda _:r,cancelled=lambda:state['cancel'])
    assert reads==[2] and all(p!='verify' for p,_ in gateway.calls)

def test_original_page_without_pixels_cannot_be_marked_read(monkeypatch):
    f,s,v,r=setup();reads=[]
    def text_only(source,page):reads.append(page);return [{'type':'text','text':'OCR is not pixels'}]
    monkeypatch.setattr('backend.visual_context.page_content',text_only)
    status,result,g=run(v,s,f,r)
    assert status!='completed' and result['claims']==[] and reads==[2]
    assert all(p!='verify' for p,_ in g.calls)

def test_invalid_local_numeric_candidate_does_not_trigger_image_recovery(monkeypatch):
    f,s,v,r=setup();bad=copy.deepcopy(v[-1]);bad['claims'][0]['text']='2024年石景山区工业增加值为999亿元。'
    monkeypatch.setattr('backend.visual_context.page_content',lambda *_:pytest.fail('invalid claim must not trigger image recovery'))
    status,result,g=run([v[0],bad,bad,bad,bad],s,f,r)
    assert status!='completed' and result['claims']==[]
    assert all(p!='verify' for p,_ in g.calls)
