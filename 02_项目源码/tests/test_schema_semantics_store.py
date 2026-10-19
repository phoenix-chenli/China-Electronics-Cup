"""Transactional user confirmation tests; all business contents are synthetic."""
import json
import sqlite3

import pytest

from backend.schema_catalog import build_catalog
from backend.semantic_store import SemanticStore, SemanticStoreError, read_state
from backend.store import Store, now
from test_schema_semantics import source_for


@pytest.fixture
def semantics(tmp_path):
    store=Store(tmp_path/'runtime'); store.initialize()
    project=store.create_project('合成测试项目','')
    path=tmp_path/'business.sqlite'
    with sqlite3.connect(path) as c:c.execute('CREATE TABLE orders(id INTEGER PRIMARY KEY,amount TEXT)')
    source=source_for(path)
    source.update(project_id=project['id'],kind='database',format='sqlite',bytes=path.stat().st_size,
        status='ready',stage='完成',progress=100,summary='',created_at=now(),updated_at=now(),
        original_path=str(path),pages=[],metadata={})
    record={k:json.dumps(v,ensure_ascii=False) if k in ('warnings','tables','pages','metadata') else v for k,v in source.items()}
    with store.transaction() as c:
        c.execute(f"INSERT INTO sources({','.join(record)}) VALUES({','.join('?' for _ in record)})",list(record.values()))
    semantic=SemanticStore(store); semantic.initialize(); semantic.initialize()
    return store,semantic,project['id'],source['id']


def definition(**extra):
    return {'id':'revenue','kind':'metric','label':'收入','definition':'订单头实际金额',
            'references':[{'table':'orders','column':'amount'}],'unit':'元',**extra}


def patch(semantic,pid,sid,action='confirm',**extra):
    state=semantic.get(pid,sid)
    return {'expected_revision':state['revision'],'expected_source_revision':state['source_revision'],
            'action':action,**({'confirmed':True} if action=='confirm' else {}),
            'definition':definition(),**extra}


def test_explicit_confirmation_is_scoped_persisted_and_injected_without_source_mutation(semantics):
    store,semantic,pid,sid=semantics
    before=store.get('sources',sid)
    result=semantic.update(pid,sid,patch(semantic,pid,sid))
    assert result['revision']==1 and result['confirmed'][0]['origin']=='user_confirmed'
    assert result['confirmed'][0]['confirmation_id']
    assert store.get('sources',sid)==before
    restarted=SemanticStore(store)
    injected=restarted.inject(before)
    assert 'schema_semantics' not in before
    layer=build_catalog(injected)['semantic_layer']
    assert layer['definitions'][0]['status']=='confirmed'
    assert restarted.events(pid,sid)[0]['action']=='confirm'


def test_candidate_cannot_promote_by_origin_or_partial_confirmation(semantics):
    store,semantic,pid,sid=semantics
    semantic.update(pid,sid,patch(semantic,pid,sid,'save_candidate'))
    assert semantic.get(pid,sid)['candidates'][0]['status']=='requires_confirmation'
    for candidate in (definition(origin='user_confirmed'),{'id':'revenue'},definition(confirmation_id='forged')):
        with pytest.raises(SemanticStoreError):semantic.update(pid,sid,patch(semantic,pid,sid,definition=candidate))
    with pytest.raises(SemanticStoreError,match='明确确认'):
        semantic.update(pid,sid,{k:v for k,v in patch(semantic,pid,sid).items() if k!='confirmed'})
    confirmed=semantic.update(pid,sid,patch(semantic,pid,sid))
    assert len(confirmed['confirmed'])==1 and not confirmed['candidates']
    with pytest.raises(SemanticStoreError,match='不能覆盖'):
        semantic.update(pid,sid,patch(semantic,pid,sid,'save_candidate'))


def test_revoke_preserves_confirmation_history_and_removes_injected_definition(semantics):
    store,semantic,pid,sid=semantics
    saved=semantic.update(pid,sid,patch(semantic,pid,sid))
    result=semantic.delete(pid,sid,'revenue',expected_revision=saved['revision'],expected_source_revision=saved['source_revision'])
    assert not result['confirmed'] and result['revoked'][0]['id']=='revenue'
    events=semantic.events(pid,sid)
    assert [e['action'] for e in events]==['confirm','revoke']
    assert events[1]['before']['confirmation_id']==events[0]['after']['confirmation_id']
    assert not build_catalog(semantic.inject(store.get('sources',sid)))['semantic_layer']['definitions']


def test_stale_revision_and_other_project_do_not_write(semantics):
    store,semantic,pid,sid=semantics
    first=patch(semantic,pid,sid)
    semantic.update(pid,sid,first)
    with pytest.raises(SemanticStoreError) as exc:semantic.update(pid,sid,first)
    assert exc.value.code=='revision_conflict'
    other=store.create_project('另一个项目','')['id']
    with pytest.raises(SemanticStoreError) as exc:semantic.get(other,sid)
    assert exc.value.code=='not_found'
    assert len(semantic.events(pid,sid))==1


def test_reparse_scope_does_not_automatically_reapply_old_confirmation(semantics):
    store,semantic,pid,sid=semantics
    before=store.get('sources',sid)
    semantic.update(pid,sid,patch(semantic,pid,sid))
    with store.transaction() as c:c.execute("UPDATE sources SET metadata=? WHERE id=?",(json.dumps({'new_parse':True}),sid))
    current=semantic.get(pid,sid)
    assert current['revision']==0 and not current['confirmed'] and current['stale_scope_count']==1
    with pytest.raises(SemanticStoreError) as exc:semantic.inject(before)
    assert exc.value.code=='revision_conflict'


@pytest.mark.parametrize('reference',[{'table':'missing'},{'table':'orders','column':'missing'},{'table':[]},{'table':'orders','column':[]}])
def test_user_confirmation_still_requires_real_schema_fields(semantics,reference):
    store,semantic,pid,sid=semantics
    with pytest.raises(SemanticStoreError):
        semantic.update(pid,sid,patch(semantic,pid,sid,definition=definition(references=[reference])))
    assert semantic.get(pid,sid)['revision']==0


def test_archived_source_cannot_be_edited(semantics):
    store,semantic,pid,sid=semantics
    payload=patch(semantic,pid,sid)
    with store.transaction() as c:c.execute('UPDATE sources SET archived=1 WHERE id=?',(sid,))
    with pytest.raises(SemanticStoreError) as exc:semantic.update(pid,sid,payload)
    assert exc.value.code=='source_unavailable'


def test_semantic_revision_changes_without_changing_parse_revision(semantics):
    store,semantic,pid,sid=semantics
    initial=semantic.get(pid,sid)
    saved=semantic.update(pid,sid,patch(semantic,pid,sid))
    assert initial['source_revision']==saved['source_revision']
    assert initial['semantic_revision']!=saved['semantic_revision']
    assert semantic.inject(store.get('sources',sid))['semantic_revision']==saved['semantic_revision']
    with store.transaction() as c:c.execute('UPDATE sources SET parser_version=? WHERE id=?',('changed-parser',sid))
    changed=semantic.get(pid,sid)
    assert changed['source_revision']==saved['source_revision']
    assert changed['semantic_revision']!=saved['semantic_revision']
    assert not changed['confirmed'] and changed['stale_scope_count']==1


def test_revoking_one_conflicting_confirmation_recomputes_visible_status(semantics):
    store,semantic,pid,sid=semantics
    semantic.update(pid,sid,patch(semantic,pid,sid))
    conflicting=semantic.update(pid,sid,patch(semantic,pid,sid,definition=definition(id='revenue2',unit='万元')))
    assert all(d['status']=='conflicting_confirmations' for d in conflicting['confirmed'])
    resolved=semantic.delete(pid,sid,'revenue2',expected_revision=conflicting['revision'],expected_source_revision=conflicting['source_revision'])
    assert resolved['confirmed'][0]['status']=='confirmed' and not resolved['warnings']


def test_export_state_reader_works_in_readonly_connection(semantics):
    store,semantic,pid,sid=semantics
    saved=semantic.update(pid,sid,patch(semantic,pid,sid))
    with sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True) as connection:
        connection.execute('PRAGMA query_only=ON')
        result=read_state(connection,semantic.inject(store.get('sources',sid)))
        assert result['semantic_revision']==saved['semantic_revision']
        assert result['source_revision']==saved['source_revision']
