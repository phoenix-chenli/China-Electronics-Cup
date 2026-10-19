"""Direct SQL writes must invalidate even with an unchanged updated_at field."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from backend.source_guard import FIELDS, StoreSourceGuard


@pytest.fixture
def guarded(tmp_path):
    path = tmp_path / 'workspace.sqlite'
    source = {'id': 's', 'project_id': 'p', 'name': 'source.pdf', 'sha256': 'a' * 64,
        'parser_version': 'test', 'archived': 0, 'status': 'ready',
        'original_path': '/test/original.pdf', 'data_path': None,
        'pages': [{'number': 1, 'text': 'initial text'}], 'tables': [], 'metadata': {'tag': 'initial'}}
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE sources(' + ','.join(f'{k} TEXT' if k != 'archived' else 'archived INTEGER' for k in FIELDS) + ', updated_at TEXT)')
        db.execute('CREATE TABLE unrelated(value TEXT)')
        values = [json.dumps(source[k]) if k in ('pages', 'tables', 'metadata') else source[k] for k in FIELDS]
        db.execute('INSERT INTO sources VALUES(' + ','.join('?' for _ in range(len(FIELDS) + 1)) + ')', [*values, 'unchanged'])
    guard = StoreSourceGuard(SimpleNamespace(path=path), source)
    try:
        yield path, source, guard
    finally:
        guard.close()


@pytest.mark.parametrize(('field', 'value'), [('metadata', {'tag': 'changed'}),
    ('pages', [{'number': 1, 'text': 'changed directly'}]), ('tables', [{'name': 'changed'}])])
def test_direct_sql_content_change_without_updated_at_invalidates(guarded, field, value):
    path, _, guard = guarded
    assert guard()
    with sqlite3.connect(path) as db:
        db.execute(f'UPDATE sources SET {field}=? WHERE id=?', (json.dumps(value), 's'))
        assert db.execute('SELECT updated_at FROM sources').fetchone()[0] == 'unchanged'
    assert not guard()


def test_unchanged_database_fast_path_and_unrelated_commit(guarded):
    path, _, guard = guarded
    before = guard.full_reads
    assert all(guard() for _ in range(10))
    assert guard.full_reads == before
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO unrelated VALUES(?)', ('unrelated commit',))
    assert guard()
    assert guard.full_reads == before + 1
    assert guard()
    assert guard.full_reads == before + 1


@pytest.mark.parametrize(('field', 'value'), [('archived', 1), ('project_id', 'other'), ('parser_version', 'changed')])
def test_scope_and_lifecycle_fields_are_not_skipped(guarded, field, value):
    path, _, guard = guarded
    with sqlite3.connect(path) as db:
        db.execute(f'UPDATE sources SET {field}=? WHERE id=?', (value, 's'))
    assert not guard()
