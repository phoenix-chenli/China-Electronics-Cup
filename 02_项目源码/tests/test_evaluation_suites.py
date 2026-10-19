"""Offline fixture/protocol tests; not measurements of real model accuracy."""
import hashlib,json,sqlite3
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlglot

from evaluation.build_sql_suite import build
from evaluation.context_cases import context_cases
from evaluation.core import assess,digest,input_payload,load_suite,summarize
from evaluation.runner import score,register_fixture_provenance
from evaluation.synthetic_sources import build_boundary_database

FIXTURES=Path(__file__).parent/'fixtures/complex_queries'


def approved(record,**extra):
    return dict(reviewer='independent synthetic-test reviewer; not a human study',
        record_sha256=hashlib.sha256(json.dumps(record,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
        correct=True,complete=True,grounded=True,**extra)


def test_sql100_keys_execute_from_frozen_originals_and_never_enter_input(tmp_path):
    before={p.name:digest(p) for p in FIXTURES.glob('*.sqlite')}
    report=build(tmp_path/'suite',FIXTURES)
    root=tmp_path/'suite';suite=load_suite(root/'inputs/suite.json')
    keys=json.loads((root/'private-key.json').read_text())['cases']
    assert report['counts']=={'basic':40,'complex':60} and report['model_calls']==0
    assert len(suite['cases'])==len(keys)==100 and len({c['question'] for c in suite['cases']})==100
    assert before=={p.name:digest(p) for p in FIXTURES.glob('*.sqlite')}
    for case in suite['cases']:
        key=keys[case['id']]
        assert key['require_text_review'] and 'reference_sql' not in case and 'rows' not in case
        payload=input_payload(case,{s['id']:s['id'] for s in suite['sources']})
        assert not set(payload)&{'reference_sql','expected','rows','private_key','review_requirements'}
        assert key['ordered']==bool(sqlglot.parse_one(key['reference_sql'],read='sqlite').args.get('order'))
    # A window's ORDER BY alone does not make output row ordering contractual.
    assert any(' OVER' in k['reference_sql'].upper() and not k['ordered'] for k in keys.values())
    with pytest.raises(ValueError,match='已存在'):build(root,FIXTURES)


def test_controlled_original_has_real_fanout_and_equal_amount_traps(tmp_path):
    path=build_boundary_database(tmp_path/'boundary.sqlite');before=digest(path)
    with sqlite3.connect('file:'+str(path)+'?mode=ro&immutable=1',uri=True) as c:
        amounts=[r[0] for r in c.execute("SELECT total_cents FROM orders WHERE status='已完成'") if r[0] is not None]
        expected=sum(amounts)
        fanout=c.execute("SELECT SUM(o.total_cents) FROM orders o JOIN lines l ON l.order_id=o.id WHERE o.status='已完成'").fetchone()[0]
        distinct=c.execute("SELECT SUM(DISTINCT total_cents) FROM orders WHERE status='已完成'").fetchone()[0]
        assert expected==66000 and expected!=fanout and expected!=distinct
        assert c.execute('SELECT COUNT(*) FROM orders WHERE total_cents IS NULL').fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM orders WHERE total_cents=0').fetchone()[0]==1
        assert len(c.execute("SELECT id FROM staff WHERE name='张伟'").fetchall())==2
        assert len(c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())>=40
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
    assert digest(path)==before


def test_context64_is_eight_full_chains_with_real_patch_payload_and_separate_keys():
    cases=context_cases();counts=Counter(c['chain_id'] for c in cases)
    assert len(cases)==64 and len(counts)==8 and set(counts.values())=={8}
    by_id={c['id']:c for c in cases}
    for case in cases:
        assert case['private_key']['require_context_review']
        if case['turn_index']>1:assert by_id[case['parent_case_id']]['turn_index']==case['turn_index']-1
        else:assert 'parent_case_id' not in case
    patch=by_id['ctx06-t2'];ids={'boundary':'actual-imported-id'}
    payload=input_payload(patch,ids,'actual-parent','actual-context-revision')
    assert payload['expected_context_revision']=='actual-context-revision'
    assert payload['context_patch']==patch['context_patch'] and 'private_key' not in payload
    with pytest.raises(ValueError,match='真实父轮'):input_payload(patch,ids,'actual-parent')
    assert by_id['ctx05-t4']['private_key']['review_requirements']['facts'][-1]=='假设Q=3kW'
    assert by_id['ctx05-t5']['private_key']['status']=='insufficient_evidence'


def test_long_chain_references_use_actual_ancestor_order_and_exceed_context_budget(tmp_path):
    path=build_boundary_database(tmp_path/'boundary.sqlite');cases={c['id']:c for c in context_cases()}
    with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as c:
        rows={i:[list(r) for r in c.execute(cases[f'ctx08-t{i}']['private_key']['reference_sql'])] for i in (1,2,3,5,6,7,8)}
    assert rows[1][11][:2]==[12,'合成对象012'] and rows[2][11][0]==189
    assert rows[5]==[[12,'合成对象012',444]] and rows[6]==[[12,444],[189,6993]]
    assert rows[7]==[[67]] and rows[8]==rows[1]
    assert len(json.dumps([rows[i] for i in (1,2,3)],ensure_ascii=False))>60000


def test_negative_status_requires_record_bound_business_review_and_cannot_hide_service_failure():
    record={'status':'insufficient_evidence','result':{'text':'缺少成本，不能算利润','evidence':[]}}
    key={'status':['insufficient_evidence','needs_clarification'],'require_evidence':False}
    assert assess(key,record)['passed'] is None
    key['review']=approved(record)
    assert assess(key,record)['passed'] is True
    assert assess(key,{'status':'failed','result':None})['passed'] is False
    record['result']['text']='更换了未经审阅的理由'
    assert assess(key,record)['passed'] is None


def test_sql_execution_matches_are_separate_from_text_and_context_correctness():
    record={'status':'completed','result':{'rows':[[10]],'query':{'sql':'SELECT 10'},'evidence':[{'id':'E1'}]}}
    key={'rows':[[10]],'require_text_review':True,'require_context_review':True}
    check=assess(key,record)
    assert check['execution_match'] is True and check['passed'] is None
    key['review']=approved(record)
    assert assess(key,record)['passed'] is False
    key['review']['context_correct']=True
    assert assess(key,record)['passed'] is True


def test_crashed_run_keeps_all_frozen_questions_and_whole_chain_denominator(tmp_path):
    run={'suite_sha256':'frozen','mode':'live','cases':[{'id':'ctx-t1','record':{'status':'completed','result':{'rows':[[1]],'query':{'sql':'SELECT 1'},'evidence':[{'id':'E1'}]}}}]}
    key={'suite_sha256':'frozen','cases':{f'ctx-t{i}':{'rows':[[1]],'chain_id':'ctx','turn_index':i} for i in range(1,9)}}
    (tmp_path/'run.json').write_text(json.dumps(run));(tmp_path/'key.json').write_text(json.dumps(key))
    score(SimpleNamespace(input=tmp_path/'run.json',key=tmp_path/'key.json',output=tmp_path/'score.json'))
    report=json.loads((tmp_path/'score.json').read_text())
    assert report['summary']['total']==8 and report['summary']['not_run']==7
    assert report['summary']['end_to_end_success_rate']==1/8
    assert report['summary']['whole_chain_success_rate']==0
    assert report['summary']['chains']['ctx']['turns']==8


def test_fixture_provenance_retains_same_origin_views_without_merging_all_synthetic_authorship(tmp_path):
    from backend.main import create_app
    from fastapi.testclient import TestClient
    from backend.cross_source_engine import source_groups
    app=create_app(tmp_path/'runtime',tmp_path/'no-samples',False)
    sources=[]
    with TestClient(app) as client:
        project=client.post('/api/projects',json={'name':'显式合成出处测试'}).json()
        for i,cluster in enumerate(('original-a','original-a','original-b')):
            path=tmp_path/f'fixture{i}.txt';path.write_text(f'Explicit synthetic original view {i}')
            response=client.post(f"/api/projects/{project['id']}/import",files=[('files',(path.name,path.read_bytes()))]);response.raise_for_status()
            source=response.json()['sources'][0]
            for job in response.json()['jobs']:app.state.service.process_job(job['id'])
            manifest={'id':str(i),'sha256':digest(path),'cluster':cluster,'origin':'Created locally for evaluation','kind':'controlled_synthetic'}
            registered=register_fixture_provenance(app.state.service.store,source['id'],manifest)
            sources.append(app.state.service.store.get('sources',source['id']))
            assert registered['origin']=='evaluation-cluster:'+cluster
    assert sorted(len(g['source_ids']) for g in source_groups(sources))==[1,2]
    assert all(s['metadata']['provenance_description']=='Created locally for evaluation' for s in sources)


def test_separate_independent_reviews_never_rewrite_frozen_key(tmp_path):
    from evaluation.core import record_digest
    record={'status':'completed','result':{'text':'只有实际依据的事实','evidence':[{'id':'E1'}]}}
    run={'suite_sha256':'s','mode':'live','cases':[{'id':'q','record':record}]}
    key={'suite_sha256':'s','cases':{'q':{'status':'completed'}}}
    run_path=tmp_path/'run.json';key_path=tmp_path/'key.json';reviews_path=tmp_path/'review.json'
    run_path.write_text(json.dumps(run));key_path.write_text(json.dumps(key));before=digest(key_path)
    review={'run_sha256':digest(run_path),'reviewer':'independent AI review of synthetic test','cases':{'q':{'record_sha256':record_digest(record),'correct':True,'complete':True,'grounded':True}}}
    reviews_path.write_text(json.dumps(review))
    score(SimpleNamespace(input=run_path,key=key_path,reviews=reviews_path,output=tmp_path/'scored.json'))
    assert json.loads((tmp_path/'scored.json').read_text())['summary']['passed']==1
    assert digest(key_path)==before
    review['run_sha256']='other-run';reviews_path.write_text(json.dumps(review))
    with pytest.raises(ValueError,match='当前运行'):
        score(SimpleNamespace(input=run_path,key=key_path,reviews=reviews_path,output=tmp_path/'bad.json'))
