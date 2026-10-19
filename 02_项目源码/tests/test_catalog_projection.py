"""Offline catalog-budget regression; no model/network calls."""
import copy
import json
import sqlite3

import pytest

from backend.catalog_projection import (INITIAL_DOCUMENT_CHARACTERS,
                                        expose_page_catalog, project_catalog)


def large_document(tmp_path):
    path = tmp_path / 'parsed.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE "真实表甲"("列1" TEXT,"列2" TEXT);'
                         'CREATE TABLE "真实表乙"("列1" TEXT,"列2" TEXT);')
    warnings = [f'第 {page} 页：{page+2} 处文字的 OCR 置信度偏低，请核对原件'
                for page in range(1, 501)]
    warnings += ['第 401 页：2 个单元格的全页与逐格 OCR 结果不一致，已保留两种识别值',
                 '该资料为草案，不能确认已经正式发布。']
    source = {'id': 'source-a', 'name': '长文档.pdf', 'format': 'pdf', 'data_path': str(path),
              'pages': [{'number': 401, 'text': '原始正文，数值 7.25 不可更改。'}],
              'warnings': warnings,
              'tables': [{'name': name, 'row_count': 0} for name in ('真实表甲', '真实表乙')],
              'metadata': {'table_regions': [{'table_name': '真实表甲', 'page': 1},
                                              {'table_name': '真实表乙', 'page': 401}]}}
    entry = {'id': source['id'], 'name': source['name'], 'format': 'pdf',
             'sha256': 'hash', 'parser_version': 'test', 'page_count': 500,
             'page_unit': 'physical_page', 'warnings': warnings,
             'tables': [{'name': '真实表甲', 'table_context': {'context': '无关目录' * 30000}}]}
    return source, entry


def test_long_document_projection_keeps_identity_and_all_local_data(tmp_path):
    source, entry = large_document(tmp_path)
    original = copy.deepcopy((source, entry))
    result = project_catalog(source, entry)
    assert len(json.dumps(result, ensure_ascii=False, sort_keys=True)) <= INITIAL_DOCUMENT_CHARACTERS
    assert [result[key] for key in ('id', 'name', 'page_count')] == ['source-a', '长文档.pdf', 500]
    assert result['catalog_projection']['table_count'] == 2
    assert result['warnings_summary']['categories'][0]['warning_count'] == 500
    assert result['warnings_summary']['categories'][0]['page_count'] == 500
    assert result['warnings_summary']['publication_caution'] is True
    assert result['catalog_projection']['table_schema_deferred'] is True
    assert (source, entry) == original


def test_located_page_exposes_real_schema_and_exact_risks_once(tmp_path):
    source, entry = large_document(tmp_path)
    original = copy.deepcopy(source)
    result = project_catalog(source, entry)
    expose_page_catalog(result, source, 401)
    expose_page_catalog(result, source, 401)
    assert len(result['located_pages']) == 1
    page = result['located_pages'][0]
    assert page['table_names'] == ['真实表乙']
    assert [t['name'] for t in result['tables']] == ['真实表乙']
    assert [c['name'] for c in result['tables'][0]['columns']] == ['列1', '列2']
    assert page['warnings'] == [note for note in source['warnings'] if note.startswith('第 401 页：')]
    assert 'table_context' not in result['tables'][0]
    assert source == original


def test_table_names_are_not_used_to_fabricate_page_metadata(tmp_path):
    source, entry = large_document(tmp_path)
    source['metadata'] = {}
    result = project_catalog(source, entry)
    expose_page_catalog(result, source, 401)
    assert result['located_pages'][0]['table_names'] == []


def test_native_database_schema_and_small_document_remain_unchanged(tmp_path):
    source, entry = large_document(tmp_path)
    source['format'] = 'sqlite'
    assert project_catalog(source, entry) is entry
    small = {'id': 's', 'tables': [{'name': 'Invoice', 'columns': [{'name': 'BillingCountry'}]}], 'warnings': []}
    assert project_catalog({'format': 'pdf'}, small) is small


def test_twelve_large_catalogs_leave_context_for_evidence(tmp_path):
    source, entry = large_document(tmp_path)
    projected = [project_catalog({**source, 'id': f's{i}'}, {**entry, 'id': f's{i}'}) for i in range(12)]
    assert len(json.dumps(projected, ensure_ascii=False)) <= 12 * (INITIAL_DOCUMENT_CHARACTERS + 2)
    assert len(json.dumps(projected, ensure_ascii=False)) + 24000 < 65000


def test_engine_reaches_planner_then_expands_located_page(tmp_path):
    from backend.cross_source_engine import answer_cross_source
    source, _ = large_document(tmp_path)
    source.update(sha256='hash', parser_version='test')
    calls = []
    class ProbeReached(RuntimeError):
        pass
    def gateway(messages, role):
        payload = json.loads(messages[-1]['content'])
        calls.append(payload)
        assert len(messages[-1]['content']) < 65000
        catalog = payload['sources'][0]
        assert catalog['catalog_projection']['scope'] == 'initial_navigation_only'
        if len(calls) == 1:
            assert not catalog.get('tables')
            return {'actions': [{'id': 'S1', 'kind': 'search', 'source_id': source['id'],
                                 'query': '原始正文数值', 'page': 401, 'depends_on': []}]}
        assert catalog['located_pages'][0]['page'] == 401
        assert catalog['located_pages'][0]['table_names'] == ['真实表乙']
        assert [c['name'] for c in catalog['tables'][0]['columns']] == ['列1', '列2']
        assert '原始正文，数值 7.25 不可更改。' == payload['evidence'][0]['quote']
        raise ProbeReached
    with pytest.raises(ProbeReached):
        answer_cross_source([source], '请说明原始正文数值。', [], gateway)
    assert len(calls) == 2
