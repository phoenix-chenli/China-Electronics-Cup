"""Isolated local API/storage tests. No model call and no original workspace."""
import copy
import json
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.reliability import public_processing_plan
from backend.semantic_store import SemanticStoreError


@pytest.fixture
def env(tmp_path):
    def no_model(*args, **kwargs):
        pytest.fail('This read-only API must never construct a model gateway')
    app = create_app(data_dir=tmp_path/'runtime', samples_dir=tmp_path/'samples',
                     enable_workers=False, answer_gateway_factory=no_model)
    with TestClient(app) as client:
        pid = client.post('/api/projects', json={'name':'版本比较隔离测试'}).json()['id']
        response = client.post(f'/api/projects/{pid}/import',
                               files=[('files', ('facts.csv', b'item,amount\nA,10\n'))])
        assert response.status_code == 200, response.text
        for job in response.json()['jobs']:
            app.state.service.process_job(job['id'])
        sid = response.json()['sources'][0]['id']
        source = app.state.answers.semantics.inject(app.state.service.store.get('sources',sid))
        assert source['status'] == 'ready'
        answer, _ = app.state.answers.store.create(pid, source, '隔离历史回答', 'table', 'facts',
            str(uuid.uuid4()), {}, sources=[source])
        yield client, app, pid, sid, answer['id']


def endpoint(env):
    return f'/api/projects/{env[2]}/answers/{env[4]}/source-versions'


def saved_body(env):
    with env[1].state.service.store.transaction() as con:
        return con.execute('SELECT body FROM answers WHERE id=?',(env[4],)).fetchone()[0]


def change_answer(env, transform):
    item=json.loads(saved_body(env));transform(item)
    with env[1].state.service.store.transaction() as con:
        con.execute('UPDATE answers SET body=? WHERE id=?',(json.dumps(item),env[4]))


def response(env):
    result=env[0].get(endpoint(env))
    assert result.status_code == 200,result.text
    return result.json()


def test_unchanged_scope_is_read_only_and_whitelisted(env):
    change_answer(env,lambda a:a['sources'][0].update(original_path='/private/secret',api_key='private-token'))
    before=saved_body(env)
    result=response(env);row=result['sources'][0]
    assert result['comparison_status']=='unchanged'
    assert row['availability']=='available' and row['old_snapshot']==row['current_snapshot']
    assert row['changed_fields']==row['unknown_fields']==[]
    assert result['old_snapshot_queryable'] is row['old_snapshot_queryable'] is False
    assert result['original_bytes_rehashed'] is False
    assert set(row['current_snapshot'])=={'id','name','sha256','parser_version','revision','semantic_revision'}
    assert 'private-token' not in json.dumps(result) and '/private/secret' not in json.dumps(result)
    assert saved_body(env)==before
    assert response(env)==result


def test_semantic_change_uses_existing_injection_revision(env):
    client,app,pid,sid,aid=env
    state=client.get(f'/api/sources/{sid}/semantics').json()
    source=app.state.service.store.get('sources',sid);table=source['tables'][0]['name']
    definition={'id':'gross','kind':'metric','label':'金额','definition':'amount 为逐项金额',
                'references':[{'table':table,'column':'amount'}],'aliases':[],'unit':'元'}
    changed=client.patch(f'/api/sources/{sid}/semantics',json={
        'action':'confirm','definition':definition,'confirmed':True,
        'expected_revision':state['revision'],'expected_source_revision':state['source_revision']})
    assert changed.status_code==200,changed.text
    before=saved_body(env);row=response(env)['sources'][0]
    assert row['changed_fields']==['semantic_revision']
    assert row['old_snapshot']['revision']==row['current_snapshot']['revision']
    assert row['current_snapshot']['semantic_revision']==changed.json()['semantic_revision']
    assert row['comparison_status']=='changed' and saved_body(env)==before


@pytest.mark.parametrize('field,value', [('sha256','b'*64),('parser_version','new-parser')])
def test_original_or_parser_change_visible_without_rewriting_old(env,field,value):
    with env[1].state.service.store.transaction() as con:
        con.execute(f'UPDATE sources SET {field}=? WHERE id=?',(value,env[3]))
    before=saved_body(env);row=response(env)['sources'][0]
    assert field in row['changed_fields'] and row['comparison_status']=='changed'
    assert row['old_snapshot'][field]!=row['current_snapshot'][field]
    assert saved_body(env)==before


def test_parsed_content_change_has_separate_revision(env):
    with env[1].state.service.store.transaction() as con:
        con.execute('UPDATE sources SET pages=? WHERE id=?',
                    (json.dumps([{'number':1,'text':'新增正文','blocks':[]}]),env[3]))
    row=response(env)['sources'][0]
    assert row['old_snapshot']['sha256']==row['current_snapshot']['sha256']
    assert 'revision' in row['changed_fields']


def test_archived_source_keeps_snapshot_but_is_not_available(env):
    assert env[0].post(f'/api/sources/{env[3]}/archive').status_code==200
    row=response(env)['sources'][0]
    assert row['availability']=='archived' and row['current_snapshot']
    assert row['comparison_status']=='changed' and 'availability' in row['changed_fields']
    assert any(r['code']=='source_archived' for r in row['reasons'])


def test_deleted_source_does_not_claim_unchanged(env):
    # Isolated fixture exercises legacy/orphan history; no application delete API.
    with sqlite3.connect(env[1].state.service.store.path) as con:
        con.execute('DELETE FROM sources WHERE id=?',(env[3],))
    row=response(env)['sources'][0]
    assert row['availability']=='unavailable' and row['current_snapshot'] is None
    assert row['comparison_status']=='unknown' and row['old_snapshot']['id']==env[3]


@pytest.mark.parametrize('field',['revision','semantic_revision','sha256','parser_version'])
def test_missing_historical_version_is_unknown_not_inferred_from_current(env,field):
    change_answer(env,lambda a:a['sources'][0].pop(field))
    row=response(env)['sources'][0]
    assert row['comparison_status']=='unknown' and field in row['unknown_fields']
    assert field not in row['old_snapshot'] and field in row['current_snapshot']


def test_legacy_record_without_sources_keeps_unknown_revisions(env):
    change_answer(env,lambda a:a.pop('sources'))
    row=response(env)['sources'][0]
    assert row['comparison_status']=='unknown'
    assert row['unknown_fields']==['revision','semantic_revision']


def test_project_scope_hides_answers_and_foreign_current_source(env):
    other=env[0].post('/api/projects',json={'name':'other'}).json()['id']
    assert env[0].get(endpoint(env).replace(env[2],other)).status_code==404
    assert env[0].get(endpoint(env).replace(env[4],'missing')).status_code==404
    with env[1].state.service.store.transaction() as con:
        con.execute('UPDATE sources SET project_id=?,name=? WHERE id=?',(other,'其他项目的私密文件名',env[3]))
    row=response(env)['sources'][0]
    assert row['availability']=='unavailable' and row['current_snapshot'] is None
    assert '私密' not in json.dumps(row,ensure_ascii=False)


def test_concurrent_scope_change_is_explicit_unknown_without_raw_error(env,monkeypatch):
    def changing(source):
        raise SemanticStoreError('/private/path private-token', 'revision_conflict')
    monkeypatch.setattr(env[1].state.answers.semantics,'inject',changing)
    result=response(env);row=result['sources'][0]
    assert row['comparison_status']=='unknown' and row['current_snapshot'] is None
    assert any(r['code']=='current_source_changed_during_read' for r in row['reasons'])
    assert 'private-token' not in json.dumps(result)


def test_source_processing_diagnostics_typed_whitelist_and_no_mutation(env):
    sid=env[3];store=env[1].state.service.store
    meta=store.get('sources',sid)['metadata']
    meta['document_profiles']={'quality':{'pages':[{'page':2,'flags':['ocr_uncertain']}]},'processing_plan':[
        {'page':2,'requested_action':'ocr_primary','basis':['no_native_text_nonblank_page','/private/secret'],
         'executed':True,'status':'ocr_primary_empty','selected_extraction':'native','elapsed_ms':12,
         'verification_status':'not_verified','followup_executed':False,
         'api_key':'private-token','original_path':'/private/secret','raw':{'password':'not-public'}},
        {'page':3,'requested_action':'/private/path','basis':[],'executed':'yes','elapsed_ms':float('inf')},
        {'page':True,'status':'executed'},
    ]}
    with store.transaction() as con:con.execute('UPDATE sources SET metadata=? WHERE id=?',(json.dumps(meta),sid))
    before=copy.deepcopy(store.get('sources',sid)['metadata'])
    result=env[0].get(f'/api/sources/{sid}/diagnostics').json()
    row=result['processing_plan'][0]
    assert row['basis']==['no_native_text_nonblank_page'] and row['basis_omitted']==1
    assert row['basis_labels']==['非空白页没有原生正文'] and row['status']=='ocr_primary_empty'
    assert row['executed'] is True and row['verification_status']=='not_verified'
    assert len(result['processing_plan'])==2 and result['processing_plan'][1]['requested_action']=='unknown'
    assert 'executed' not in result['processing_plan'][1] and 'elapsed_ms' not in result['processing_plan'][1]
    assert result['quality']['pages'][0]['page']==2
    encoded=json.dumps(result)
    assert '/private/' not in encoded and 'private-token' not in encoded and 'not-public' not in encoded
    assert store.get('sources',sid)['metadata']==before


def test_processing_projection_bounds_old_advisory_records_and_invalid_data():
    raw=[{'page':i+1,'suggested_action':'manual_source_check','basis':['unreadable'],'executed':False} for i in range(502)]
    rows,truncated=public_processing_plan(raw)
    assert len(rows)==500 and truncated and rows[0]['executed'] is False
    assert rows[0]['suggested_action']=='manual_source_check' and 'requested_action' not in rows[0]
    assert public_processing_plan(None)==([],False)


@pytest.mark.parametrize('status,old_parser,archived,expected',[
    ('ready',True,False,True),('ready',False,False,False),
    ('ready',True,True,False),('attention',False,False,True),
    ('failed',False,False,True),('cancelled',False,False,True),
    ('processing',True,False,False),('queued',True,False,False),
])
def test_diagnostics_retry_matches_parser_upgrade_availability(env,status,old_parser,archived,expected):
    from backend.parsers import PARSER_VERSION
    with env[1].state.service.store.transaction() as con:
        con.execute('UPDATE sources SET status=?,parser_version=?,archived=? WHERE id=?',
            (status,'previous-parser' if old_parser else PARSER_VERSION,int(archived),env[3]))
    response=env[0].get(f'/api/sources/{env[3]}/diagnostics')
    assert response.status_code==200,response.text
    assert response.json()['retry_available'] is expected
    if status=='ready' and not archived:
        detail=env[0].get(f'/api/sources/{env[3]}').json()
        assert detail['source']['reprocess_available'] is expected
