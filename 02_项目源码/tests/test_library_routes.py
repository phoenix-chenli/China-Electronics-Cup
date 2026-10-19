"""Real local API/store boundaries; lexical indexes, no remote model calls."""
import hashlib
import time
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from backend.main import create_app
from backend.retrieval import RetrievalService
from backend.source_snapshot import snapshot


@pytest.fixture
def workspace(tmp_path):
    def forbidden(*args,**kwargs):raise AssertionError('library search must not call answer model')
    app=create_app(tmp_path/'runtime',tmp_path/'samples',enable_workers=False,
                   answer_gateway_factory=forbidden,retrieval_factory=lambda root:RetrievalService(root))
    with TestClient(app) as client:
        service=app.state.service
        p=service.store.create_project('P','')['id'];q=service.store.create_project('Q','')['id']
        yield client,service,p,q


def add(service,pid,text):
    path=service.root/'incoming'/uuid4().hex;path.write_text(text)
    result=service.import_staged(pid,[dict(path=path,name=path.name+'.txt',format='txt',bytes=path.stat().st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest())])
    service.process_job(result['jobs'][0]['id'])
    return service.store.get('sources',result['sources'][0]['id'])


def body(*sources):
    return dict(question='pump inspection',sources=[{k:snapshot(s)[k] for k in ('id','sha256','parser_version','revision')} for s in sources])


def terminal(client,pid,jid):
    for _ in range(200):
        response=client.get(f'/api/projects/{pid}/library-search/{jid}');assert response.status_code==200
        value=response.json()
        if value['status'] not in ('queued','running'):return value
        time.sleep(.01)
    pytest.fail('local search did not terminate')


def test_library_route_explicit_scope_and_fallback(workspace):
    client,service,p,q=workspace
    selected=add(service,p,'pump inspection every fourteen days')
    add(service,p,'pump inspection hidden unselected value')
    response=client.post(f'/api/projects/{p}/library-search',json=body(selected));assert response.status_code==202
    result=terminal(client,p,response.json()['id']);assert result['status']=='completed'
    assert {r['source_id'] for r in result['result']['results']}=={selected['id']}
    assert result['result']['audit']['mode_used']=='lexical'
    assert result['result']['audit']['fallback_reason']
    assert result['result']['results'][0]['source_revision']==snapshot(selected)['revision']
    assert 'original_path' not in str(result)
    assert client.get(f'/api/projects/{q}/library-search/{result["id"]}').status_code==404
    assert client.post(f'/api/projects/{q}/library-search/{result["id"]}/cancel').status_code==404


def test_library_route_rejects_foreign_archived_and_stale_selection(workspace):
    client,service,p,q=workspace
    own=add(service,p,'pump inspection');foreign=add(service,q,'pump inspection private')
    assert client.post(f'/api/projects/{p}/library-search',json=body(own,foreign)).status_code==404
    changed=body(own);changed['sources'][0]['revision']='0'*64
    assert client.post(f'/api/projects/{p}/library-search',json=changed).status_code==409
    assert client.post(f'/api/sources/{own["id"]}/archive').status_code==200
    assert client.post(f'/api/projects/{p}/library-search',json=body(own)).status_code==409


def test_completed_library_result_invalidates_after_archive_or_original_change(workspace):
    client,service,p,q=workspace
    for archive in (True,False):
        s=add(service,p,'pump inspection '+str(archive))
        response=client.post(f'/api/projects/{p}/library-search',json=body(s))
        jid=response.json()['id'];assert terminal(client,p,jid)['status']=='completed'
        if archive:client.post(f'/api/sources/{s["id"]}/archive')
        else:
            from pathlib import Path
            Path(s['original_path']).chmod(0o600)
            Path(s['original_path']).write_text('changed raw original')
        result=client.get(f'/api/projects/{p}/library-search/{jid}').json()
        assert result['status']=='failed' and result['result'] is None


def test_library_selection_limits_do_not_expand_answer_limit(workspace):
    client,service,p,q=workspace
    sources=[add(service,p,f'pump inspection {n}') for n in range(13)]
    response=client.post(f'/api/projects/{p}/library-search',json=body(*sources));assert response.status_code==202
    assert terminal(client,p,response.json()['id'])['result']['audit']['source_count']==13
    duplicated=body(sources[0],sources[0]);assert client.post(f'/api/projects/{p}/library-search',json=duplicated).status_code==422
    too_many=body(sources[0]);too_many['sources']*=129
    assert client.post(f'/api/projects/{p}/library-search',json=too_many).status_code==422
    assert client.post(f'/api/projects/{p}/answers',json=dict(question='pump inspection',source_id=sources[0]['id'],source_ids=[s['id'] for s in sources],request_id=str(uuid4()))).status_code==422


def test_library_version_change_during_build_does_not_publish(workspace,monkeypatch):
    client,service,p,q=workspace
    s=add(service,p,'pump inspection during version change')
    original=RetrievalService.prepare
    def mutate(self,source,*args,**kwargs):
        with service.store.connect() as db:db.execute("UPDATE sources SET parser_version='changed' WHERE id=?",(source['id'],))
        return original(self,source,*args,**kwargs)
    monkeypatch.setattr(RetrievalService,'prepare',mutate)
    response=client.post(f'/api/projects/{p}/library-search',json=body(s))
    result=terminal(client,p,response.json()['id']);assert result['status']=='failed' and result['result'] is None


def test_library_cancel_cannot_publish_late_result(workspace,monkeypatch):
    import threading
    client,service,p,q=workspace
    s=add(service,p,'pump inspection cancellation fixture')
    entered,release=threading.Event(),threading.Event()
    original=RetrievalService.prepare
    def hold(self,*args,**kwargs):
        entered.set();assert release.wait(3)
        return original(self,*args,**kwargs)
    monkeypatch.setattr(RetrievalService,'prepare',hold)
    response=client.post(f'/api/projects/{p}/library-search',json=body(s));jid=response.json()['id']
    try:
        assert entered.wait(2)
        assert client.post(f'/api/projects/{p}/library-search',json=body(s)).status_code==409
        assert client.post(f'/api/projects/{p}/library-search/{jid}/cancel').json()['status']=='cancelled'
    finally:release.set()
    for _ in range(100):
        result=client.get(f'/api/projects/{p}/library-search/{jid}').json()
        assert result['status']=='cancelled' and result['result'] is None
        if result['elapsed_ms']>0:break
        time.sleep(.01)


@pytest.mark.parametrize('field', ['metadata', 'pages'])
def test_library_guard_detects_direct_sql_change_without_timestamp(workspace, monkeypatch, field):
    import json
    client, service, p, _ = workspace
    source = add(service, p, 'pump inspection direct SQL fixture')
    old_updated = source['updated_at']
    original = RetrievalService.prepare
    def mutate(self, candidate, *args, **kwargs):
        value = {'tag': 'changed'} if field == 'metadata' else [{'number': 1, 'text': 'changed text'}]
        with service.store.connect() as db:
            db.execute(f'UPDATE sources SET {field}=? WHERE id=?', (json.dumps(value), candidate['id']))
        return original(self, candidate, *args, **kwargs)
    monkeypatch.setattr(RetrievalService, 'prepare', mutate)
    response = client.post(f'/api/projects/{p}/library-search', json=body(source))
    result = terminal(client, p, response.json()['id'])
    assert result['status'] == 'failed' and result['result'] is None
    assert service.store.get('sources', source['id'])['updated_at'] == old_updated


def test_library_handle_checks_do_not_decode_store_source_again(workspace, monkeypatch):
    import backend.library_routes as routes
    client, service, p, _ = workspace
    source = add(service, p, 'pump inspection stable checks')
    original_get, original_search = service.store.get, routes.search_library
    ranking = [False]
    def get(table, identifier):
        if ranking[0] and table == 'sources':
            raise AssertionError('stable handle checks must not repeatedly decode Store JSON')
        return original_get(table, identifier)
    def search(*args, **kwargs):
        ranking[0] = True
        try:
            return original_search(*args, **kwargs)
        finally:
            ranking[0] = False
    monkeypatch.setattr(service.store, 'get', get)
    monkeypatch.setattr(routes, 'search_library', search)
    response = client.post(f'/api/projects/{p}/library-search', json=body(source))
    result = terminal(client, p, response.json()['id'])
    assert result['status'] == 'completed' and result['result']['results']
