import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from evaluation.core import equal_rows, assess, load_suite, input_payload, summarize, digest
from evaluation.runner import run, score, replay


def frozen(tmp_path):
    (tmp_path/'facts.txt').write_text('实际材料为 2024 年。')
    suite={'schema_version':'zhiheng.eval-input.v1','name':'explicit fixture',
           'sources':[{'id':'s1','file':'facts.txt','sha256':digest(tmp_path/'facts.txt'),'cluster':'original-1','origin':'synthetic','license':'CC0'}],
           'cases':[{'id':'q1','question':'材料是哪一年？','source_ids':['s1'],'mode':'document','split':'development'}]}
    path=tmp_path/'input.json'
    path.write_text(json.dumps(suite,ensure_ascii=False))
    path.with_suffix('.json.sha256').write_text(digest(path))
    return path,suite


def test_multiset_comparison_preserves_duplicates_nulls_units_and_order():
    assert equal_rows([[2],[1],[1]],[[1],[1],[2]])
    assert not equal_rows([[2],[2],[1]],[[1],[1],[2]])
    assert not equal_rows([[2],[1]],[[1],[2]],ordered=True)
    assert not equal_rows([[None]],[[0]])
    assert not equal_rows([[True]],[[1]])
    assert equal_rows([['0.10']],[[.1]],tolerance=0)
    assert not equal_rows([['100%']],[[100]])


def test_execution_success_is_not_text_correctness():
    record={'status':'completed','result':{'text':'ungraded','evidence':[{'id':'E1'}]}}
    assert assess({'status':'completed'},record)['passed'] is None
    sha=hashlib.sha256(json.dumps(record,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    review={'reviewer':'AI-assisted evidence review, not human study','record_sha256':sha,'correct':True,'complete':True,'grounded':True}
    assert assess({'status':'completed','review':review},record)['passed'] is True
    record['result']['text']='changed'
    assert assess({'review':review},record)['passed'] is None
    assert assess({'status':'completed'},{'status':'insufficient_evidence'})['category']=='availability_failure'


def test_input_leakage_and_same_source_split_rejected(tmp_path):
    path,suite=frozen(tmp_path)
    assert load_suite(path)['name']=='explicit fixture'
    payload=input_payload(dict(suite['cases'][0],expected='secret-answer'),{'s1':'actual-id'})
    assert 'expected' not in payload
    suite['cases'][0]['expected']='reference answer'
    path.write_text(json.dumps(suite));path.with_suffix('.json.sha256').write_text(digest(path))
    with pytest.raises(ValueError,match='问题字段'):load_suite(path)
    suite['cases'][0].pop('expected')
    suite['cases'].append(dict(suite['cases'][0],id='q2',split='held_out'))
    path.write_text(json.dumps(suite));path.with_suffix('.json.sha256').write_text(digest(path))
    with pytest.raises(ValueError,match='同源资料簇'):load_suite(path)


def test_fault_run_score_replay_do_not_claim_live_success(tmp_path):
    path,_=frozen(tmp_path)
    report=tmp_path/'report.json'
    run(SimpleNamespace(suite=path,runtime=tmp_path/'isolated-runtime',output=report,mode='fault',config_runtime=None,timeout=10))
    data=json.loads(report.read_text())
    assert data['mode']=='fault' and data['cases'][0]['record']['status']=='failed'
    key=tmp_path/'grading.json'
    key.write_text(json.dumps({'suite_sha256':digest(path),'cases':{'q1':{'status':'completed'}}}))
    scored=tmp_path/'score.json'
    score(SimpleNamespace(input=report,key=key,output=scored))
    outcome=json.loads(scored.read_text())
    assert outcome['summary']['total']==1 and outcome['summary']['passed']==0
    assert outcome['model_calls']==0 and outcome['evaluated_mode']=='fault'
    replayed=tmp_path/'replayed.json';replay(SimpleNamespace(input=report,output=replayed))
    assert json.loads(replayed.read_text())['mode']=='offline_replay'
    with pytest.raises(ValueError,match='已存在'):replay(SimpleNamespace(input=report,output=replayed))


def test_summary_keeps_unrun_and_pending_in_denominator():
    items=[{'record':{},'assessment':{'passed':True,'category':'passed'}},
           {'record':{},'assessment':{'passed':None,'category':'review_pending'}},
           {'record':None,'assessment':{'passed':False,'category':'not_run'}}]
    summary=summarize(items)
    assert summary['total']==3 and summary['end_to_end_success_rate']==1/3
    assert summary['pending_review']==1 and summary['not_run']==1
