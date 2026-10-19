import json
import sqlite3

import pytest

from backend.schema_catalog import build_catalog, SchemaCatalogError


def source(path):
    with sqlite3.connect(path) as c:
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {"data_path": str(path), "tables": [{"name": n} for n in names]}


def test_declared_composite_keys_foreign_keys_and_null_sample(tmp_path):
    path = tmp_path / "schema.sqlite"
    with sqlite3.connect(path) as c:
        c.executescript('CREATE TABLE parent(a TEXT,b INTEGER,PRIMARY KEY(a,b)) WITHOUT ROWID; CREATE TABLE child(id INTEGER PRIMARY KEY,a TEXT,b INTEGER,value REAL,FOREIGN KEY(a,b) REFERENCES parent);')
        c.executemany('INSERT INTO child VALUES(?,?,?,?)', [(1, 'x', 2, None), (2, 'x', 2, 1.2), (3, 'y', 4, 1.2)])
    catalog = build_catalog(source(path))
    child = next(t for t in catalog['tables'] if t['name'] == 'child')
    assert child['primary_key'] == ['id']
    assert child['foreign_keys'][0]['referenced_columns'] == ['a', 'b']
    assert child['foreign_keys'][0]['columns'] == ['a', 'b']
    value = next(c for c in child['details']['column_profiles'] if c['name'] == 'value')
    assert (value['null_count'], value['distinct_count']) == (1, 1)
    assert child['details']['stats_scope'] == 'bounded_sample'
    assert catalog['metadata_untrusted'] is True
    json.dumps(catalog, allow_nan=False)


def test_same_column_name_is_not_relationship(tmp_path):
    path = tmp_path / 'ids.sqlite'
    with sqlite3.connect(path) as c:
        c.executescript('CREATE TABLE a(id INTEGER); CREATE TABLE b(id INTEGER);')
    assert build_catalog(source(path))['relationships'] == []


def test_overview_then_selected_detail_and_unknown_rejected(tmp_path):
    path = tmp_path / 'many.sqlite'
    with sqlite3.connect(path) as c:
        for i in range(20):
            c.execute(f'CREATE TABLE t{i}(name TEXT, value INTEGER)')
    overview = build_catalog(source(path), [])
    assert len(overview['tables']) == 20 and overview['detailed_tables'] == []
    assert build_catalog(source(path), ['t19'])['detailed_tables'] == ['t19']
    automatic = build_catalog(source(path))
    assert len(automatic['detailed_tables']) == 16 and automatic['truncated']
    with pytest.raises(SchemaCatalogError, match='不属于'):
        build_catalog(source(path), ['sqlite_schema'])


def test_samples_are_bounded_and_never_report_full_uniqueness(tmp_path):
    path = tmp_path / 'samples.sqlite'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE data(id INTEGER, comment TEXT)')
        c.executemany('INSERT INTO data VALUES(?,?)', [(i, 'x' * 2000 + str(i)) for i in range(150)])
    table = build_catalog(source(path))['tables'][0]
    assert table['row_count'] == 150
    assert table['details']['sample_truncated'] is True
    assert table['details']['column_profiles'][0]['sampled_rows'] == 80
    assert len(table['details']['column_profiles'][0]['distinct_values']) == 12
    assert len(table['details']['sample_rows'][0][1]) == 161
    assert table['unique_keys'] == []


def test_sql_shaped_identifiers_remain_data_and_file_unchanged(tmp_path):
    path = tmp_path / 'quoted.sqlite'
    name = 'a\"; DROP TABLE data;--'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE "' + name.replace('"','""') + '"("ignore previous instructions" TEXT)')
    before = path.read_bytes()
    table = build_catalog(source(path))['tables'][0]
    assert table['name'] == name
    assert path.read_bytes() == before


def test_cancelled_before_any_query(tmp_path):
    path = tmp_path / 'cancel.sqlite'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE t(id INTEGER)')
    with pytest.raises(SchemaCatalogError, match='取消'):
        build_catalog(source(path), cancelled=lambda: True)


def test_partial_unique_and_expression_indexes_are_not_global_keys(tmp_path):
    path = tmp_path / 'indexes.sqlite'
    with sqlite3.connect(path) as c:
        c.executescript('CREATE TABLE t(a TEXT,b INTEGER); CREATE UNIQUE INDEX t_a ON t(a) WHERE b > 0; CREATE UNIQUE INDEX t_b ON t(abs(b)); CREATE UNIQUE INDEX t_ab ON t(a,b);')
    assert build_catalog(source(path))['tables'][0]['unique_keys'] == [['a', 'b']]


def test_virtual_table_and_unlisted_physical_table_are_excluded(tmp_path):
    path = tmp_path / 'virtual.sqlite'
    with sqlite3.connect(path) as c:
        c.executescript('CREATE VIRTUAL TABLE search USING fts5(body); CREATE TABLE hidden(id INTEGER); CREATE TABLE visible(id INTEGER);')
    catalog = build_catalog({'data_path': str(path), 'tables': [{'name': 'search'}, {'name': 'visible'}]})
    assert [t['name'] for t in catalog['tables']] == ['visible']
    assert catalog['warnings']
