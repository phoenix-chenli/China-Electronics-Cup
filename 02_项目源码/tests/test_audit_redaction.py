"""Pure/local fake-gateway privacy tests; no real service requests."""
import copy
import json
import pytest
from backend.audit_redaction import redact_diagnostics, redact_sql_result_diagnostics
from backend.sql_review_evidence import audit_record


@pytest.mark.parametrize('key', ['api_key','api-key','access_token','refresh_token','token','password','authorization','endpoint','base_url','model_endpoint','private_key'])
def test_private_fields_masked_without_changing_verdicts_or_hashes(key):
    data={'review':{'supported':False,key:'synthetic-private-value','confidence':0.2,'total_tokens':123,'sha256':'a'*64},'rows':[[0,-12.5,None]],'scope_review':{'source_basis':False}}
    before=copy.deepcopy(data);out=redact_diagnostics(data)
    assert data==before
    assert 'synthetic-private-value' not in json.dumps(out)
    assert out['review']['supported'] is False and out['review']['confidence']==0.2
    assert out['review']['total_tokens']==123 and out['review']['sha256']=='a'*64
    assert out['rows']==[[0,-12.5,None]] and out['scope_review']['source_basis'] is False
    assert out['_audit_redaction']['redacted'] is True
    assert redact_diagnostics(out)==out


@pytest.mark.parametrize('text,forbidden',[
 ('reason api_key="synthetic secret with spaces" then supported=false','synthetic secret with spaces'),
 ("password='synthetic second secret' after",'synthetic second secret'),
 ('endpoint="https://provider-for-test.invalid/private/v1" rejected','provider-for-test.invalid'),
 ('Bearer synthetic-token-value rejected','synthetic-token-value'),
 ('path /Users/synthetic-owner/private.txt reject','synthetic-owner'),
 ('path C:\\Users\\synthetic-owner\\private.txt reject','synthetic-owner'),
 ('bare sk-synthetic_secret_token_123456 rejected','sk-synthetic_secret_token_123456'),
 ('-----BEGIN PRIVATE KEY-----\nsynthetic private material\n-----END PRIVATE KEY-----','synthetic private material'),
])
def test_string_embedded_private_fields_and_paths(text,forbidden):
    out=redact_diagnostics({'reason':text,'supported':False})
    assert forbidden not in json.dumps(out) and out['supported'] is False
    assert redact_diagnostics(out)==out


def test_ordinary_large_history_is_not_sliced_to_envelope_4k():
    data={'reviews':[{'index':i,'reason':'普通已收到复核。'*120,'supported':False,'sha256':str(i)}for i in range(20)]}
    assert len(json.dumps(data,ensure_ascii=False))>4000
    assert redact_diagnostics(data)==data


def test_budget_omissions_are_explicit_and_do_not_publish(monkeypatch):
    from backend import audit_redaction as mod
    monkeypatch.setattr(mod,'MAX_TEXT_CHARACTERS',30)
    out=mod.redact_diagnostics({'supported':False,'reason':'x'*100,'api_key':'synthetic-not-retained'})
    assert out['supported'] is False and out['_audit_redaction']['truncated'] is True
    assert 'synthetic-not-retained' not in json.dumps(out)


def test_audit_record_detaches_and_cleans_first_and_second_reviews():
    first={'supported':False,'api_key':'synthetic-first','reason':'/Users/synthetic-owner/private.txt'}
    second={'supported':True,'endpoint':'https://synthetic-provider.invalid/v1'}
    q={'sql':'SELECT 1','parameters':[],'review_response':first,'review_adjudication':{'response':{'review':second},'passed_remaining_gates':False}}
    before=copy.deepcopy(q);record=audit_record(q,{'columns':['one'],'rows':[[1]]},1,'failed')
    assert q==before and record['review_response']['supported'] is False
    assert record['review_adjudication']['response']['review']['supported'] is True
    assert record['review_adjudication']['passed_remaining_gates'] is False
    text=json.dumps(record);assert all(x not in text for x in ['synthetic-first','synthetic-owner','synthetic-provider'])


def test_output_projection_never_changes_business_rows_or_internal_check():
    value={'text':'业务路径原文','rows':[['/Users/test/business-cell']], 'claims':[{'text':'原事实'}],
      'evidence':[{'quote':'原句'}], 'query':{'sql':'SELECT ?','parameters':['/Users/test/business-cell'],
      'executed_result':{'rows':[['/Users/test/business-cell']]},
      'plan':{'verification_reason':'api_key="synthetic private string" failed'},
      'review_response':{'supported':False,'endpoint':'https://synthetic-provider.invalid/v1'},
      'effective_review_response':{'supported':True,'api_key':'synthetic-extra'},
      'review_adjudication':{'status':'received','response':{'token':'synthetic-token'}},
      'semantic_reviews':[{'supported':False,'password':'synthetic-pass'}]}}
    before=copy.deepcopy(value);out=redact_sql_result_diagnostics(value)
    assert value==before
    for key in ('text','rows','claims','evidence'):assert out[key]==before[key]
    for key in ('sql','parameters','executed_result'):assert out['query'][key]==before['query'][key]
    assert out['query']['review_response']['supported'] is False
    assert out['query']['effective_review_response']['supported'] is True
    assert 'synthetic-' not in json.dumps(out['query']) and 'synthetic private' not in json.dumps(out['query'])


def test_public_wrapper_cleans_final_output_without_mutating_model_internal_result(monkeypatch):
    from backend import query_planner
    original={'query':{'review_response':{'supported':False,'token':'synthetic-token'}},'rows':[[2]],'claims':[]}
    def implementation(*args):return 'failed',original
    monkeypatch.setattr(query_planner,'_answer_database',implementation)
    status,out=query_planner.answer_database({},'q',[],None)
    assert status=='failed' and out['query']['review_response']['supported'] is False
    assert original['query']['review_response']['token']=='synthetic-token'
    assert 'synthetic-token' not in json.dumps(out)
