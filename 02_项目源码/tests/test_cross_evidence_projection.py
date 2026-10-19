"""Scripted retrieval projection preserves source offsets and shared budgets."""
import copy
import pytest
from backend import cross_source_engine as engine
from backend.document_engine import Chunk
from backend.reasoning_tools import ReasoningLimits
from test_deferred_table_context import fixture
from test_cross_source_engine import Responses,actions


def prepared_source():
    s=fixture();s.update(name='例表.pdf',parser_version='fixture',tables=[],warnings=[])
    s['metadata']['page_unit']='physical_page'
    text=s['pages'][0]['text']
    return s,Chunk('original-mixed',2,0,len(text),text)


class Retriever:
    def __init__(self,chunk):self.chunk=chunk
    def prepared(self):return [self.chunk],False
    def audit(self):return {"scripted":True}
    def select(self,*a,**k):return [self.chunk],False


def run_source(monkeypatch,budget=None):
    s,chunk=prepared_source()
    if budget is not None:monkeypatch.setattr(engine,'MAX_EVIDENCE_CHARACTERS',budget)
    g=Responses(actions({'id':'S1','kind':'search','source_id':'s','query':'TN 设计能力','depends_on':[]}),
                actions({'id':'S2','kind':'search','source_id':'s','query':'other','depends_on':[]}))
    status,result=engine.answer_cross_source([s],'查询 TN 和设计能力。',[],g,
        retrieval_factory=lambda _:Retriever(chunk),limits=ReasoningLimits(max_actions=1))
    return s,chunk,result


def test_mixed_projection_changes_ids_but_never_offsets_or_plain_prose_structure():
    s,c=prepared_source();before=copy.deepcopy(s)
    projected=engine._split_evidence_chunks(s,[c,c],'TN')
    assert len({x.id for x in projected})==len(projected)>1
    for x in projected:
        assert x.id==f'p2:{x.start}-{x.end}' and x.id!=c.id
        assert s['pages'][0]['text'][x.start:x.end]==x.text
        assert c.start<=x.start<x.end<=c.end
    assert s==before


def test_actual_search_path_keeps_real_rows_and_plain_prose_with_audit(monkeypatch):
    s,c,r=run_source(monkeypatch)
    records=r['evidence'];p=next(e for e in records if '设计能力' in e['quote'])
    assert not p.get('table_context',{}).get('has_table')
    rows=[e for e in records if e.get('table_context',{}).get('has_table')]
    assert len(rows)>=2 and any('R2C2' in t['selected_cell_ids'] for e in rows for t in e['table_context']['tables'])
    search=r['steps'][0]['search'];assert search['retrieved_chunk_ids']==['original-mixed']
    assert search['accepted_span_count']==len(records)==search['projected_span_count']
    assert sum(engine.evidence_cost(e) for e in records)<=engine.MAX_EVIDENCE_CHARACTERS


def test_shared_budget_still_limits_projection_and_reports_omission(monkeypatch):
    s,c,r=run_source(monkeypatch,3300)
    assert sum(engine.evidence_cost(e) for e in r['evidence'])<=3300
    search=r['steps'][0]['search']
    assert search['truncated'] and search['accepted_span_count']<search['projected_span_count']


def test_projection_does_not_accept_out_of_source_retrieval_or_helper_ranges(monkeypatch):
    s,c=prepared_source()
    with pytest.raises(engine.CrossSourceError,match='原页文本'):engine._split_evidence_chunks(s,[Chunk(c.id,c.page,c.start,c.end,'forged')],'TN')
    monkeypatch.setattr(engine,'split_evidence_span',lambda *a:[{'start':0,'end':c.end+1}])
    with pytest.raises(engine.CrossSourceError,match='范围'):engine._split_evidence_chunks(s,[c],'TN')
