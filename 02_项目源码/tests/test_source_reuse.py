"""Cross-project reuse keeps immutable assets and project decisions isolated."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app


@pytest.fixture
def env(tmp_path):
    with TestClient(create_app(tmp_path/'runtime', tmp_path/'empty-samples', enable_workers=False)) as client:
        a = client.post('/api/projects', json={'name':'A'}).json()['id']
        b = client.post('/api/projects', json={'name':'B'}).json()['id']
        yield client, client.app.state.service, a, b


def upload(env, name='facts.csv', content=b'item,amount\nalpha,10\nbeta,20\n', process=True):
    client, service, a, _ = env
    result = client.post(f'/api/projects/{a}/import', files={'files':(name, content)}).json()
    if process:
        service.process_job(result['jobs'][0]['id'])
    return service.store.get('sources', result['sources'][0]['id'])


def reuse(env, ids, project=None):
    client, _, _, b = env
    return client.post(f'/api/projects/{project or b}/reuse', json={'source_ids':ids})


def test_reuse_shares_assets_and_tables_but_resets_review(env):
    client, service, a, b = env
    original = upload(env)
    service.store.review(original['id'], 'reviewed', 'Reviewed in A')
    result = reuse(env, [original['id']]).json()
    assert not result['jobs'] and not result['duplicates']
    linked = service.store.get('sources', result['sources'][0]['id'])
    assert linked['id'] != original['id'] and linked['project_id'] == b
    assert linked['original_path'] == original['original_path']
    assert linked['data_path'] == original['data_path']
    assert linked['pages'] == original['pages'] and linked['tables'] == original['tables']
    assert linked['review_status'] == 'pending' and linked['review_note'] == ''
    assert linked['metadata']['reused_from'] == original['id']
    assert len(list((service.root/'originals').iterdir())) == 1
    table = linked['tables'][0]['name']
    rows = client.get(f'/api/sources/{linked["id"]}/tables/{table}').json()
    assert rows['rows'] == [['alpha','10'],['beta','20']]
    assert client.get(f'/api/sources/{linked["id"]}/original').content == Path(original['original_path']).read_bytes()
    service.store.archive(original['id'], True)
    assert client.get(f'/api/sources/{linked["id"]}').json()['source']['usable']
    assert service.store.get('sources', linked['id'])['review_status'] == 'pending'


def test_repeat_and_same_content_different_name_are_deduplicated(env):
    client, service, a, b = env
    original = upload(env)
    first = reuse(env, [original['id']]).json()['sources'][0]
    repeated = reuse(env, [original['id'],original['id']]).json()
    assert not repeated['sources'] and repeated['duplicates'][0]['id'] == first['id']
    same = client.post(f'/api/projects/{b}/import', files={'files':('renamed.csv',Path(original['original_path']).read_bytes())}).json()
    assert not same['sources'] and not same['jobs'] and len(same['duplicates']) == 1
    assert not service.store.jobs(b)


def test_new_upload_in_another_project_automatically_reuses_parsed_content(env):
    client, service, a, b = env
    original = upload(env)
    result = client.post(f'/api/projects/{b}/import', files={'files':('renamed.csv',Path(original['original_path']).read_bytes())}).json()
    assert len(result['sources']) == 1 and not result['jobs']
    linked = service.store.get('sources', result['sources'][0]['id'])
    assert linked['name'] == 'renamed.csv' and linked['original_path'] == original['original_path']
    assert linked['data_path'] == original['data_path'] and linked['status'] == original['status']
    assert not list((service.root/'incoming').iterdir())


def test_batch_failure_rolls_back_prior_links(env):
    original = upload(env)
    assert reuse(env, [original['id'],'missing']).status_code == 409
    assert not env[1].store.sources(env[3])


@pytest.mark.parametrize('change', ['archived','processing','missing_original','corrupt_original','missing_table'])
def test_invalid_cache_cannot_be_explicitly_reused(env, change):
    original = upload(env)
    if change == 'archived':
        env[1].store.archive(original['id'], True)
    elif change == 'processing':
        with env[1].store.transaction() as c:
            c.execute("UPDATE sources SET status='processing' WHERE id=?", (original['id'],))
    elif change == 'missing_original':
        Path(original['original_path']).unlink()
    elif change == 'corrupt_original':
        path = Path(original['original_path']); path.chmod(0o600); path.write_bytes(b'changed')
    else:
        Path(original['data_path']).unlink()
    assert reuse(env, [original['id']]).status_code == 409
    assert not env[1].store.sources(env[3])


def test_invalid_cache_falls_back_to_normal_import(env):
    client, service, a, b = env
    original = upload(env)
    content = Path(original['original_path']).read_bytes()
    Path(original['data_path']).unlink()
    response = client.post(f'/api/projects/{b}/import', files={'files':('new.csv',content)}).json()
    assert len(response['jobs']) == 1
    service.process_job(response['jobs'][0]['id'])
    linked = service.store.get('sources', response['sources'][0]['id'])
    assert linked['data_path'] != original['data_path'] and linked['status'] == 'ready'


def test_racing_reuse_is_transactionally_idempotent(env):
    original = upload(env)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: env[1].store.reuse_sources(env[3],[original['id']]), range(2)))
    assert sum(len(r['sources']) for r in results) == 1
    assert sum(len(r['duplicates']) for r in results) == 1
    assert len(env[1].store.sources(env[3])) == 1


def test_reprocessing_uses_new_asset_version_without_changing_other_project(env):
    client, service, a, b = env
    original = upload(env)
    linked_id = reuse(env,[original['id']]).json()['sources'][0]['id']
    with service.store.transaction() as c:
        c.execute("UPDATE sources SET parser_version='old' WHERE id=?",(original['id'],))
    _, job = service.store.retry(original['id'])
    service.process_job(job['id'])
    assert service.store.get('sources', original['id'])['data_path'] != original['data_path']
    linked = service.store.get('sources', linked_id)
    assert linked['data_path'] == original['data_path'] and Path(linked['data_path']).is_file()
    assert linked['tables'] == original['tables']


def test_payload_limits_and_unknown_project(env):
    original = upload(env)
    assert reuse(env,[]).status_code == 422
    assert reuse(env,[original['id']]*21).status_code == 422
    assert reuse(env,[original['id']], project='missing').status_code == 404


def test_project_business_definitions_are_not_carried_over(env):
    from test_semantic_api import confirm
    client, service, a, b = env
    original = upload(env)
    detail = client.get(f'/api/sources/{original["id"]}').json()
    confirm(client, detail)
    linked = reuse(env,[original['id']]).json()['sources'][0]
    assert client.get(f'/api/sources/{original["id"]}/semantics').json()['confirmed']
    assert not client.get(f'/api/sources/{linked["id"]}/semantics').json()['confirmed']


def test_quality_warnings_remain_visible_in_target_project(env):
    import json
    original = upload(env)
    with env[1].store.transaction() as c:
        c.execute("UPDATE sources SET status='attention',stage='需要关注',warnings=? WHERE id=?",
                  (json.dumps(['OCR 数字需核对']), original['id']))
    linked = reuse(env,[original['id']]).json()['sources'][0]
    assert linked['usable'] and linked['status'] == 'attention'
    assert linked['warnings'] == ['OCR 数字需核对']
    assert linked['review_status'] == 'pending'
