"""Synthetic gateways test missing-object recovery; no model accuracy claim."""
import copy

import pytest

from backend import chart_answer_engine as engine, completeness
from test_chart_answer_engine import Gateway, payload, source


def test_distributive_objects_preserve_scope_and_bare_comparisons_do_not_split():
    question = '图1趋势怎样？SensorA与SensorB分别利用什么物理量形成信号？'
    parts = engine._chart_question_parts(question)
    assert [part['id'] for part in parts] == ['Q1', 'Q2', 'Q3']
    assert [part.get('focus_entity') for part in parts] == [None, 'SensorA', 'SensorB']
    assert parts[1]['text'] == parts[2]['text'] == 'SensorA与SensorB分别利用什么物理量形成信号？'
    assert len(engine._chart_question_parts('图1中甲与乙谁更高？')) == 1
    assert len(engine._chart_question_parts('图1中甲和乙的差值是多少？')) == 1
    assert len(engine._chart_question_parts('甲传感器、乙传感器分别有什么限制？')) == 2
    assert completeness.question_parts(question)[1] == {'id': 'Q2', 'text': parts[1]['text']}


def test_page_selection_pairs_english_caption_with_other_object_body_not_contents():
    sample = {'metadata': {}, 'pages': [
        {'number': 3, 'text': 'TABLE OF CONTENTS\nSensorA SensorB SensorB\nFigure 1'},
        {'number': 10, 'text': 'SensorA\nFigure 1\nResistance curves\nSensorA resistance'},
        {'number': 11, 'text': 'SensorA\nFigure 2\nSensorA construction'},
        {'number': 27, 'text': 'SensorB Construction\nSensorB parts and wires'},
        {'number': 28, 'text': 'SensorB Operation\nSensorB produces voltage.\nFigure 6\nCircuit'},
    ]}
    question = '图1曲线顺序怎样？SensorA与SensorB分别利用什么物理量形成信号？'
    selected = engine.select_pages(sample, question)
    assert selected[:2] == [10, 28] and 3 not in selected
    assert engine.select_pages(sample, '第10页图1中SensorA与SensorB分别是什么？') == [10]
    assert engine.select_pages(sample, 'Fig. 6 中 SensorB 怎样工作？') == [28]


def test_independent_object_review_requires_actual_quotes_and_rejects_other_name():
    parts = engine._chart_question_parts('SensorA与SensorB分别利用什么物理量？')
    claims = [{'id': 'C1', 'text': 'SensorA利用电阻。'}, {'id': 'C2', 'text': 'SensorB利用电压。'}]
    raw = {'coverage': [{'question_id': p['id'], 'status': 'answered', 'claim_ids': ['C1'],
                         'evidence_ids': ['E1'], 'subject_mention': 'SensorA', 'answer_quote': claims[0]['text']}
                        for p in parts]}
    adjusted, errors = engine._object_coverage(raw, claims, parts, ['E1'])
    assert not errors and adjusted['complete'] is False
    assert adjusted['items'][1]['status'] == 'missing_evidence'
    assert not engine._entity_mentioned('PVTsensor', 'VT')
    raw['coverage'][1]['subject_mention'] = 'SensorB'
    raw['coverage'][1]['answer_quote'] = 'SensorB利用电压。'
    assert engine._object_coverage(raw, claims, parts, ['E1'])[0] is None  # Not in bound C1.


def test_object_review_accepts_semantically_judged_translated_terminology():
    parts = engine._chart_question_parts('RTD与Thermocouple分别利用什么物理量？')
    claims = [{'id': 'C1', 'text': '热电阻通过电阻变化测温。'}, {'id': 'C2', 'text': '热电偶通过热电势测温。'}]
    raw = {'coverage': [{'question_id': p['id'], 'status': 'answered', 'claim_ids': [claim['id']],
                         'evidence_ids': ['E1'], 'subject_mention': mention, 'answer_quote': claim['text']}
                        for p, claim, mention in zip(parts, claims, ['热电阻', '热电偶'])]}
    coverage, errors = engine._object_coverage(raw, claims, parts, ['E1'])
    assert not errors and coverage['complete'] is True
    raw['coverage'][1]['status'] = 'missing_evidence'
    assert engine._object_coverage(raw, claims, parts, ['E1'])[0]['complete'] is False


def _linked_quote_case():
    parts = engine._chart_question_parts('SensorA与SensorB分别利用什么物理量？')
    claims = [{'id': 'C1', 'text': 'SensorA利用电阻变化。'},
              {'id': 'C2', 'text': '乙器件的测量端电压高于参考端。'},
              {'id': 'C3', 'text': '这两个电压之差与温差成正比。'}]
    raw = {'coverage': [
        {'question_id': 'Q1', 'status': 'answered', 'claim_ids': ['C1'], 'evidence_ids': ['E1'],
         'subject_mention': 'SensorA', 'answer_quotes': [{'claim_id': 'C1', 'quote': claims[0]['text']}]},
        {'question_id': 'Q2', 'status': 'answered', 'claim_ids': ['C2', 'C3'], 'evidence_ids': ['E1'],
         'subject_mention': '乙器件', 'answer_quotes': [
             {'claim_id': 'C2', 'quote': claims[1]['text']}, {'claim_id': 'C3', 'quote': claims[2]['text']}]}]}
    return parts, claims, raw


def test_cross_claim_explanation_retains_separate_exact_quotes_and_translation():
    parts, claims, raw = _linked_quote_case()
    original = copy.deepcopy(claims)
    coverage, errors = engine._object_coverage(raw, claims, parts, ['E1'])
    assert not errors and coverage['complete'] is True
    assert coverage['items'][1]['answer_quotes'] == raw['coverage'][1]['answer_quotes']
    assert claims == original  # Validation never merges or rewrites published claims.


@pytest.mark.parametrize('mutation', ['joined', 'unbound', 'unknown', 'reordered'])
def test_multi_quote_binding_cannot_invent_text_borrow_claims_or_reverse_antecedent(mutation):
    parts, claims, raw = _linked_quote_case()
    quotes = raw['coverage'][1]['answer_quotes']
    if mutation == 'joined':
        quotes[0]['quote'] += quotes[1]['quote']
    elif mutation == 'unbound':
        quotes[1] = {'claim_id': 'C1', 'quote': claims[0]['text']}
    elif mutation == 'unknown':
        quotes[1]['claim_id'] = 'C99'
    else:
        quotes.reverse()
    assert engine._object_coverage(raw, claims, parts, ['E1'])[0] is None


def test_multiple_quotes_do_not_override_wrong_subject_or_semantic_missing_verdict():
    parts, claims, raw = _linked_quote_case()
    raw['coverage'][1].update(claim_ids=['C1'], subject_mention='SensorA',
                              answer_quotes=[{'claim_id': 'C1', 'quote': claims[0]['text']}])
    coverage, errors = engine._object_coverage(raw, claims, parts, ['E1'])
    assert not errors and coverage['items'][1]['status'] == 'missing_evidence'
    parts, claims, raw = _linked_quote_case()
    # The reviewer may find the pronoun ambiguous or the condition incomplete.
    # Even valid quotes must not upgrade that semantic rejection.
    raw['coverage'][1]['status'] = 'missing_evidence'
    coverage, errors = engine._object_coverage(raw, claims, parts, ['E1'])
    assert not errors and coverage['complete'] is False


def test_subject_heading_without_any_fact_cannot_be_complete():
    parts, claims, raw = _linked_quote_case()
    claims[1]['text'] = '乙器件。'
    raw['coverage'][1].update(claim_ids=['C2'], answer_quotes=[{'claim_id': 'C2', 'quote': '乙器件。'}])
    assert engine._object_coverage(raw, claims, parts, ['E1'])[0] is None


def _read(page):
    texts = ['曲线向上。', 'SensorA利用电阻变化形成信号。'] if page == 1 else ['SensorB利用电压变化形成信号。']
    items = [{'id': f'V{i+1}', 'type': 'visual_relation', 'text': text, 'page': page,
              'bbox': [0, 0, 1, 1], 'provenance': 'separate_visual_read_and_review'} for i, text in enumerate(texts)]
    return {'status': 'reviewed', 'version': 'synthetic-v1', 'items': items,
            'provenance': 'separate_visual_read_and_review',
            'review': {'valid': True, 'supported_ids': [i['id'] for i in items], 'rejected_ids': []},
            'audit': {'original_page_image_sha256': str(page) * 64}}


def _draft(include_second):
    claims = [{'id': 'C1', 'text': '曲线向上。', 'item_ids': ['P1_V1']},
              {'id': 'C2', 'text': 'SensorA利用电阻变化形成信号。', 'item_ids': ['P1_V2']}]
    if include_second:
        claims.append({'id': 'C3', 'text': 'SensorB利用电压变化形成信号。', 'item_ids': ['P2_V1']})
    return {'claims': claims}


def _false_positive_review(messages, purpose):
    body = payload(messages)
    assert [p.get('focus_entity') for p in body['question_parts']] == [None, 'SensorA', 'SensorB']
    ids = [c['id'] for c in body['claims']]
    return {'claims': [{'id': cid, 'supported': True} for cid in ids],
            'coverage': [{'question_id': p['id'], 'status': 'answered',
                          'claim_ids': [ids[min(i, len(ids)-1)]], 'evidence_ids': [body['evidence_ids'][-1]]}
                         for i, p in enumerate(body['question_parts'])]}


def _independent_review(messages, purpose):
    body = payload(messages)
    assert 'reviewed_items' not in body and 'claims' not in body and 'accepted_claims' in body
    claims = body['accepted_claims']
    rows = []
    for i, part in enumerate(body['question_parts']):
        claim = claims[i] if i < len(claims) else None
        row = {'question_id': part['id'], 'status': 'answered' if claim else 'missing_evidence',
               'claim_ids': [claim['id']] if claim else [], 'evidence_ids': []}
        if claim and part.get('focus_entity'):
            row.update(subject_mention=part['focus_entity'], answer_quote=claim['text'])
        rows.append(row)
    return {'coverage': rows}


@pytest.mark.parametrize('second_page', [True, False])
def test_missing_object_forces_new_page_recovery_instead_of_completed(source, monkeypatch, second_page):
    source['pages'] = [{'number': 1, 'text': 'SensorA\nFigure 1\nSensorA resistance curve'}]
    if second_page:
        source['pages'].append({'number': 2, 'text': 'SensorB Operation\nSensorB voltage\nFigure 6'})
    before, read_pages = copy.deepcopy(source), []
    def read(s, page, *args):
        read_pages.append(page)
        return _read(page)
    monkeypatch.setattr(engine, 'read_chart_page', read)
    monkeypatch.setattr(engine, 'page_content', lambda *a, **k: [])
    responses = [_draft(False), _false_positive_review, _independent_review]
    if second_page:
        responses += [_draft(True), _false_positive_review, _independent_review]
    gateway = Gateway(*responses)
    result = engine.answer_chart(source, '图1曲线怎样？SensorA与SensorB分别利用什么物理量形成信号？', gateway)
    assert source == before
    assert result['diagnostics']['entity_guards'][0]['coverage']['items'][2]['status'] == 'missing_evidence'
    assert read_pages == ([1, 2] if second_page else [1])
    assert result['status'] == ('completed' if second_page else 'insufficient_evidence')
    if second_page:
        assert result['claims'][-1]['text'].startswith('SensorB')
        assert result['claims'][-1]['chart_bindings'][0]['page'] == 2
        assert len(gateway.calls) == 6
    else:
        assert len(gateway.calls) == 3 and result['completeness']['complete'] is False
