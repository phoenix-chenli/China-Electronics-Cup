"""Synthetic source pixels + scripted gateways exercise chart publication/export.

No OCR or remote model is used. These are integration contracts, not claims about
real-document chart recognition accuracy.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3

import pytest

from backend.document_engine import answer_document
from backend.cross_source_engine import answer_cross_source
from backend.export_service import export_answer
from backend.reasoning_tools import ReasoningLimits


CHART_QUESTION = '图1中甲类的数值是多少？'
POINT_TEXT = '甲类为10元。'


def payload_of(messages):
    content = messages[-1]['content']
    return json.loads(content[0]['text'] if isinstance(content, list) else content)


def has_pixels(messages):
    content = messages[-1]['content']
    return isinstance(content, list) and any(
        part.get('type') == 'image_url' and part['image_url']['url'].startswith('data:image/jpeg;base64,')
        for part in content)


def pixel_hashes(parts):
    return [hashlib.sha256(part['image_url']['url'].encode()).hexdigest()
            for part in parts if part.get('type') == 'image_url']


def record_original_page_reads(monkeypatch, *, fail_page=None):
    from backend import visual_context
    original = visual_context.page_content
    reads, pixels_by_page = [], {}

    def observed(source, page):
        reads.append(page)
        if page == fail_page:
            raise OSError('Explicit fixture: required original page is unreadable')
        parts = original(source, page)
        pixels_by_page[page] = pixel_hashes(parts)
        assert pixels_by_page[page], 'The original reader must produce actual pixels'
        return parts

    monkeypatch.setattr(visual_context, 'page_content', observed)
    return reads, pixels_by_page


@pytest.fixture
def chart_source(tmp_path, monkeypatch):
    """Build actual synthetic PDF pixels and matching native-text observations."""
    import pymupdf
    from backend import ocr_native

    def forbidden_ocr(*args, **kwargs):
        raise AssertionError('This integration fixture must not call OCR')

    monkeypatch.setattr(ocr_native, 'recognize', forbidden_ocr)

    def build(*, labeled=True, requires_review=False, missing_original=False, second_page=False):
        path = tmp_path / ('labeled.pdf' if labeled else 'unlabeled.pdf')
        doc = pymupdf.open()
        page = doc.new_page(width=400, height=400)
        page.insert_text((35, 40), '图1 测试销量', fontname='china-s', fontsize=16)
        page.draw_rect(pymupdf.Rect(50, 160, 110, 320), color=(0, .4, .8), fill=(0, .4, .8))
        page.draw_rect(pymupdf.Rect(230, 240, 290, 320), color=(0, .4, .8), fill=(0, .4, .8))
        page.insert_text((50, 145), '甲类10元' if labeled else '甲类', fontname='china-s', fontsize=14)
        page.insert_text((230, 225), '乙类20元' if labeled else '乙类', fontname='china-s', fontsize=14)
        if second_page:
            other = doc.new_page(width=400, height=400)
            other.insert_text((35, 40), '附页没有图表数据', fontname='china-s', fontsize=16)
        doc.save(path)
        doc.close()
        with pymupdf.open(path) as doc:
            page = doc[0]
            text_parts, blocks, offset = [], [], 0
            for raw in page.get_text('blocks'):
                text = raw[4].strip()
                if not text:
                    continue
                text_parts.append(text)
                blocks.append({'text': text, 'start': offset, 'end': offset + len(text),
                               'bbox': [raw[0]/400, raw[1]/400, raw[2]/400, raw[3]/400]})
                offset += len(text) + 1
        quality = {'visual_review_required': True, 'preprocessing': {
            'version': 'explicit-test', 'status': 'kept_original', 'selected': 'original',
            'text_disagreement': True, 'numeric_symbol_disagreement': True,
            'visual_review_required': True, 'extra_ocr_calls': 1}} if requires_review else {}
        pages = [{'number': 1, 'text': '\n'.join(text_parts), 'blocks': blocks,
                  'extraction': 'native', 'has_image': False, 'quality': quality}]
        if second_page:
            pages.append({'number': 2, 'text': '附页没有图表数据', 'blocks': [],
                          'extraction': 'native', 'has_image': False, 'quality': {}})
        return {'id': 'chart-source', 'name': '显式合成图表.pdf', 'format': 'pdf',
                'status': 'ready', 'archived': False, 'tables': [], 'warnings': [],
                'original_path': str(path.with_name('absent.pdf') if missing_original else path),
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'parser_version': 'synthetic-chart-integration-v1',
                'metadata': {'page_unit': 'physical_page', 'source_group': 'synthetic-chart-test'},
                'pages': pages}
    return build


def make_binding(context, statement, *, kind='printed_point', value='10', evidence_id=None):
    if kind == 'visual_observation':
        binding = {'kind': kind, 'statement': statement, 'observation_type': 'missing_printed_values',
                   'observation_ids': []}
    else:
        row = next(row for row in context['observations'] if row['text'].startswith('甲类'))
        binding = {'kind': kind, 'statement': statement, 'category': '甲类', 'value': value,
                   'unit': '元', 'association': 'same_line',
                   'category_observation_id': row['id'], 'value_observation_id': row['id'],
                   'unit_observation_id': row['id']}
    if evidence_id:
        binding['evidence_id'] = evidence_id
    return binding


class DocumentGateway:
    """Deliberately optimistic fake verifier; local gates must still reject lies."""
    def __init__(self, *, statement=POINT_TEXT, kind='printed_point', value='10', binding=True,
                 relation='supported', visual=True):
        self.statement, self.kind, self.value = statement, kind, value
        self.binding, self.relation, self.visual = binding, relation, visual
        self.calls = []

    def __call__(self, messages, purpose):
        payload = payload_of(messages)
        self.calls.append({'purpose': purpose, 'payload': payload, 'has_pixels': has_pixels(messages)})
        assert len(self.calls) <= 4, 'Unexpected unbounded document retry'
        if purpose == 'document':
            chunk = payload['chunks'][0]
            claim = {'text': self.statement,
                     'citations': [{'chunk_id': chunk['chunk_id'], 'quote': chunk['text']}]}
            if self.binding:
                claim['chart_bindings'] = [make_binding(chunk['chart_context'], self.statement,
                    kind=self.kind, value=self.value)]
            return {'status': 'completed', 'claims': [claim]}
        ids = [claim['id'] for claim in payload['claims']]
        response = {'claims': [{'id': cid, 'supported': True} for cid in ids], 'answer_complete': True,
                    'coverage': [{'question_id': part['id'], 'status': 'answered', 'claim_ids': ids}
                                 for part in payload['question_parts']]}
        if self.visual is not None:
            response['visual_supported'] = self.visual
        if self.relation != 'missing':
            response['chart_relations'] = [{'id': cid, 'supported': self.relation == 'supported'}
                                          for cid in payload['chart_relation_claim_ids']]
        return response


class CrossGateway:
    def __init__(self, *, read_image=True, relation='supported', visual=True, binding=True,
                 statement=POINT_TEXT, kind='printed_point', value='10', image_page=1):
        self.read_image, self.relation, self.visual, self.binding = read_image, relation, visual, binding
        self.statement, self.kind, self.value = statement, kind, value
        self.image_page = image_page
        self.calls = []

    def __call__(self, messages, purpose):
        payload = payload_of(messages)
        content = messages[-1]['content']
        self.calls.append({'purpose': purpose, 'payload': payload, 'has_pixels': has_pixels(messages),
                           'pixel_hashes': pixel_hashes(content) if isinstance(content, list) else []})
        assert len(self.calls) <= 8, 'Unexpected unbounded cross-source retry'
        if purpose == 'table':
            if not payload['evidence']:
                actions = [{'id': 'search-page', 'kind': 'search', 'source_id': 'chart-source',
                            'query': '甲类 图1', 'page': 1, 'depends_on': []}]
                if self.read_image:
                    actions.append({'id': 'view-page', 'kind': 'read_image', 'source_id': 'chart-source',
                                    'page': self.image_page, 'depends_on': []})
                return {'actions': actions}
            evidence = payload['evidence'][0]
            claim = {'text': self.statement, 'evidence_ids': [evidence['id']]}
            if self.binding:
                claim['chart_bindings'] = [make_binding(evidence['chart_context'], self.statement,
                    kind=self.kind, value=self.value, evidence_id=evidence['id'])]
            return {'status': 'completed', 'claims': [claim]}
        ids = [claim['id'] for claim in payload['candidate_claims']]
        result = {'supported': True, 'answer_complete': True, 'conflict': False,
                  'claims': [{'id': cid, 'supported': True} for cid in ids],
                  'coverage': [{'question_id': part['id'], 'status': 'answered', 'claim_ids': ids}
                               for part in payload['question_parts']]}
        if self.visual is not None:
            result['visual_supported'] = self.visual
        if self.relation != 'missing':
            result['chart_relations'] = [{'id': cid, 'supported': self.relation == 'supported'}
                                        for cid in payload['chart_relation_claim_ids']]
        return result


def run_document(source, gateway, question=CHART_QUESTION):
    return answer_document(source, question, gateway, lambda: False, lambda stage: None)


def run_cross(source, gateway, question=CHART_QUESTION):
    return answer_cross_source([source], question, [], gateway)


@pytest.mark.parametrize('mode', ['document', 'cross'])
def test_chart_publication_requires_pixels_and_independent_relation_review(chart_source, mode):
    source = chart_source()
    before = copy.deepcopy(source)
    gateway = DocumentGateway() if mode == 'document' else CrossGateway()
    result = run_document(source, gateway) if mode == 'document' else run_cross(source, gateway)[1]
    assert result['verification']['status'] == 'passed'
    assert result['text'] == POINT_TEXT and len(result['claims']) == 1
    binding = result['claims'][0]['chart_bindings'][0]
    assert binding['page'] == 1 and binding['sha256'] == source['sha256']
    assert binding['field_observations']['value']['text'] == '甲类10元'
    assert result['verification']['chart_relations'][0]['supported'] is True
    assert gateway.calls[-1]['purpose'] == 'verify' and gateway.calls[-1]['has_pixels']
    assert source == before
    assert 'data:image' not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize('mode', ['document', 'cross'])
@pytest.mark.parametrize('relation', ['missing', 'rejected'])
def test_semantic_yes_cannot_replace_chart_relation_verdict(chart_source, mode, relation):
    gateway = DocumentGateway(relation=relation) if mode == 'document' else CrossGateway(relation=relation)
    if mode == 'document':
        result = run_document(chart_source(), gateway)
        status = result['status']
    else:
        status, result = run_cross(chart_source(), gateway)
    assert status != 'completed'
    assert result['claims'] == [] and POINT_TEXT not in result['text']
    assert any(call['purpose'] == 'verify' for call in gateway.calls)


@pytest.mark.parametrize('question', [CHART_QUESTION, '根据图一，甲类的收入是多少？'])
def test_document_missing_original_stops_before_gateway(chart_source, question):
    gateway = DocumentGateway()
    result = run_document(chart_source(missing_original=True), gateway, question)
    assert result['status'] == 'insufficient_evidence' and result['claims'] == []
    assert gateway.calls == []


@pytest.mark.parametrize('question', [CHART_QUESTION, '根据图一，甲类的收入是多少？'])
def test_cross_text_only_plan_reads_required_original_before_review(chart_source, question, monkeypatch):
    source = chart_source()
    reads, pixels = record_original_page_reads(monkeypatch)
    gateway = CrossGateway(read_image=False)
    status, result = run_cross(source, gateway, question)
    assert status == 'completed' and result['text'] == POINT_TEXT
    assert reads == [1] and result['verification']['tool_actions'] == 2
    assert [call['purpose'] for call in gateway.calls] == ['table', 'table', 'verify']
    assert gateway.calls[-1]['pixel_hashes'] == pixels[1]
    assert gateway.calls[-1]['payload']['candidate_claims'][0]['chart_bindings'][0]['page'] == 1
    assert result['claims'][0]['chart_bindings'][0]['sha256'] == source['sha256']
    assert any(item['kind'] == 'visual_required' for item in result['diagnostics']['tool_errors'])


def test_cross_wrong_page_plan_reads_correct_page_before_review(chart_source, monkeypatch):
    reads, pixels = record_original_page_reads(monkeypatch)
    gateway = CrossGateway(image_page=2)
    status, result = run_cross(chart_source(second_page=True), gateway)
    assert status == 'completed' and result['text'] == POINT_TEXT
    assert reads == [2, 1] and pixels[2] != pixels[1]
    assert result['verification']['tool_actions'] == 3
    assert [call['purpose'] for call in gateway.calls] == ['table', 'table', 'verify']
    # Wrong-page pixels reached planning, but the independent reviewer sees
    # exactly the true cited page; the unrelated page cannot authorize it.
    assert gateway.calls[1]['pixel_hashes'] == pixels[2]
    assert gateway.calls[-1]['pixel_hashes'] == pixels[1]
    assert result['claims'][0]['chart_bindings'][0]['page'] == 1
    missing = [item for item in result['diagnostics']['tool_errors'] if item['kind'] == 'visual_required']
    assert missing and missing[-1]['pages'] == [{'source_id': 'chart-source', 'page': 1}]


@pytest.mark.parametrize('blocker', ['read_failure', 'image_budget', 'action_budget'])
def test_cross_wrong_page_cannot_authorize_when_required_read_is_blocked(chart_source, monkeypatch, blocker):
    reads, pixels = record_original_page_reads(monkeypatch, fail_page=1 if blocker == 'read_failure' else None)
    gateway = CrossGateway(image_page=2)
    limits = (ReasoningLimits(max_image_pages=1) if blocker == 'image_budget'
              else ReasoningLimits(max_actions=2) if blocker == 'action_budget' else ReasoningLimits())
    status, result = answer_cross_source([chart_source(second_page=True)], CHART_QUESTION, [], gateway, limits=limits)
    assert status != 'completed' and result['claims'] == [] and POINT_TEXT not in result['text']
    assert reads == ([2, 1] if blocker == 'read_failure' else [2])
    assert set(pixels) == {2}
    assert any(call['pixel_hashes'] == pixels[2] for call in gateway.calls)
    assert all(call['purpose'] != 'verify' for call in gateway.calls)
    missing = [item for item in result['diagnostics']['tool_errors'] if item['kind'] == 'visual_required']
    assert missing and missing[-1]['pages'] == [{'source_id': 'chart-source', 'page': 1}]


def test_on_demand_ocr_reuses_existing_spans_without_charging_them_again(chart_source, monkeypatch):
    from backend import cross_source_engine as engine
    from backend.visual_requirements import evidence_cost

    source = chart_source()
    # Explicit synthetic parsed-text load and OCR observations test accounting,
    # not recognition of the generated page's actual chart values.
    source['pages'][0]['text'] += '\n' + '甲类说明需要依据图内对应标签核对，其他说明形成独立原文片段。\n' * 90
    source['pages'][0]['has_image'] = True
    before = copy.deepcopy(source)
    observations = [{'text': f'标签{index}' + '原图文字' * 12,
                     'bbox': [.1, .21 + index * .002, .9, .211 + index * .002], 'confidence': .99}
                    for index in range(240)]
    ocr_calls, calls = [], []

    def explicit_fake_observe(current, page, image_parts, *, timeout):
        ocr_calls.append(timeout)
        current['pages'][0]['chart_observations'] = copy.deepcopy(observations)
        current['pages'][0]['chart_ocr'] = {'status': 'observed'}
        return .001

    monkeypatch.setattr(engine, 'observe_chart_page', explicit_fake_observe)

    def gateway(messages, purpose):
        payload = payload_of(messages)
        calls.append({'purpose': purpose, 'payload': payload, 'pixels': has_pixels(messages)})
        assert len(json.dumps(payload, ensure_ascii=False)) <= engine.MAX_CONTEXT
        assert sum(evidence_cost(item) for item in payload.get('evidence', [])) <= engine.MAX_EVIDENCE_CHARACTERS
        if purpose == 'verify':
            ids = [claim['id'] for claim in payload['candidate_claims']]
            return {'supported': True, 'answer_complete': True, 'visual_supported': True,
                    'claims': [{'id': cid, 'supported': True} for cid in ids],
                    'chart_relations': [{'id': cid, 'supported': True} for cid in ids],
                    'coverage': [{'question_id': part['id'], 'status': 'answered', 'claim_ids': ids}
                                 for part in payload['question_parts']]}
        if not payload['evidence']:
            return {'actions': [{'id': 'locate', 'kind': 'search', 'source_id': source['id'],
                                 'query': '图1甲类', 'page': 1, 'depends_on': []}]}
        if not any(step['id'].startswith('view') for step in payload['steps']):
            # A broken implementation retries the same page and eventually hits
            # its bounded tool-error stop; the assertion below records the failure.
            return {'actions': [{'id': f'view{len(calls)}', 'kind': 'read_image',
                                 'source_id': source['id'], 'page': 1, 'depends_on': ['locate']}]}
        statement = '图中甲类的柱形高于乙类。'
        eid = payload['evidence'][0]['id']
        return {'status': 'completed', 'claims': [{'text': statement, 'evidence_ids': [eid],
            'chart_bindings': [{'kind': 'visual_observation', 'evidence_id': eid,
                                'statement': statement, 'observation_type': 'trend', 'observation_ids': []}]}]}

    status, result = run_cross(source, gateway)
    assert status == 'completed' and result['verification']['status'] == 'passed', {
        'diagnostics': result.get('diagnostics'), 'verification': result.get('verification'),
        'calls': [(call['purpose'], len(json.dumps(call['payload'], ensure_ascii=False))) for call in calls],
        'steps': result['steps']}
    assert len(ocr_calls) == 1 and len(calls) == 4
    assert [step['id'] for step in result['steps']] == ['locate', 'view2']
    assert result['steps'][1]['summary'].startswith('查看原页')
    assert result['steps'][0]['evidence_ids'] == result['steps'][1]['evidence_ids']
    assert len(result['evidence']) == len(result['steps'][0]['evidence_ids'])
    assert 23000 < sum(evidence_cost(item) for item in result['evidence']) <= engine.MAX_EVIDENCE_CHARACTERS
    assert result['diagnostics']['tool_errors'] == []
    assert calls[-1]['pixels'] and source == before


@pytest.mark.parametrize('mode', ['document', 'cross'])
def test_chart_question_cannot_publish_bare_ocr_value_without_binding(chart_source, mode):
    gateway = DocumentGateway(binding=False) if mode == 'document' else CrossGateway(binding=False)
    if mode == 'document':
        result = run_document(chart_source(), gateway)
        status = result['status']
    else:
        status, result = run_cross(chart_source(), gateway)
    assert status != 'completed' and result['claims'] == []
    assert all(call['purpose'] != 'verify' for call in gateway.calls)


@pytest.mark.parametrize('mode', ['document', 'cross'])
@pytest.mark.parametrize('visual', [False, None])
def test_flagged_preprocessing_cannot_publish_without_visual_yes_even_for_plain_question(chart_source, mode, visual):
    # A plain-text question cannot evade the page's numeric-disagreement flag.
    source = chart_source(requires_review=True)
    gateway = (DocumentGateway(binding=False, visual=visual) if mode == 'document'
               else CrossGateway(binding=False, visual=visual))
    if mode == 'document':
        result = run_document(source, gateway, '甲类的金额是多少？')
        status = result['status']
    else:
        status, result = run_cross(source, gateway, '甲类的金额是多少？')
    assert status != 'completed' and result['claims'] == []
    assert all(call['has_pixels'] for call in gateway.calls if call['purpose'] == 'verify')


@pytest.mark.parametrize('mode', ['document', 'cross'])
def test_unlabeled_chart_can_report_precise_value_unavailable_after_visual_review(chart_source, mode):
    statement = '甲类的柱形高于乙类，图中没有印刷精确值。'
    kwargs = {'kind': 'visual_observation', 'statement': statement}
    gateway = DocumentGateway(**kwargs) if mode == 'document' else CrossGateway(**kwargs)
    if mode == 'document':
        result = run_document(chart_source(labeled=False), gateway)
        status = result['status']
    else:
        status, result = run_cross(chart_source(labeled=False), gateway)
    assert status == 'completed' and result['text'] == statement
    binding = result['claims'][0]['chart_bindings'][0]
    assert binding['kind'] == 'visual_observation' and 'value' not in binding
    assert gateway.calls[-1]['has_pixels']


@pytest.mark.parametrize('mode', ['document', 'cross'])
@pytest.mark.parametrize('kind', ['printed_point', 'visual_observation'])
def test_unlabeled_chart_never_publishes_estimated_exact_value_even_with_optimistic_gateway(chart_source, mode, kind):
    kwargs = {'kind': kind, 'statement': '甲类为19.7元。', 'value': '19.7'}
    gateway = DocumentGateway(**kwargs) if mode == 'document' else CrossGateway(**kwargs)
    if mode == 'document':
        result = run_document(chart_source(labeled=False), gateway)
        status = result['status']
    else:
        status, result = run_cross(chart_source(labeled=False), gateway)
    assert status != 'completed' and result['claims'] == []
    assert '19.7' not in result['text']
    assert all(call['purpose'] != 'verify' for call in gateway.calls)


def test_export_keeps_actual_chart_bindings_and_source_observations_without_rerun_or_rewrite(chart_source, tmp_path):
    source = chart_source()
    gateway = DocumentGateway()
    result = run_document(source, gateway)
    assert result['status'] == 'completed'
    answer = {'id': 'answer-chart', 'project_id': 'project-chart', 'status': 'completed',
              'mode': 'document', 'question': CHART_QUESTION, 'result': result,
              'sources': [{key: source[key] for key in ('id', 'sha256', 'parser_version')}]}
    body = json.dumps(answer, ensure_ascii=False)
    db = tmp_path / 'export.sqlite'
    with sqlite3.connect(db) as connection:
        connection.executescript('CREATE TABLE projects(id TEXT, name TEXT);'
                                 'CREATE TABLE sources(id TEXT, project_id TEXT, body TEXT);'
                                 'CREATE TABLE answers(id TEXT, project_id TEXT, body TEXT);')
        connection.execute('INSERT INTO projects VALUES (?,?)', ('project-chart', '显式图表验收'))
        connection.execute('INSERT INTO sources VALUES (?,?,?)', (source['id'], 'project-chart', json.dumps(source)))
        connection.execute('INSERT INTO answers VALUES (?,?,?)', (answer['id'], answer['project_id'], body))
    calls = len(gateway.calls)
    exported = json.loads(export_answer(db, answer['project_id'], answer['id'], 'json').content)
    markdown = export_answer(db, answer['project_id'], answer['id'], 'markdown').content.decode()
    saved_result = exported['answers'][0]['result']
    assert saved_result['claims'][0]['chart_bindings'] == result['claims'][0]['chart_bindings']
    assert saved_result['evidence'][0]['chart_context'] == result['evidence'][0]['chart_context']
    assert '图表依据' in markdown and '甲类10元' in markdown and 'field_observations' in markdown
    assert 'chart_relations' in markdown and source['sha256'] in markdown
    assert 'data:image' not in markdown
    with sqlite3.connect(db) as connection:
        assert connection.execute('SELECT body FROM answers WHERE id=?', (answer['id'],)).fetchone()[0] == body
    assert len(gateway.calls) == calls
