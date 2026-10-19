"""Controlled contracts for query aliases and exact evidence selection, not accuracy scores."""
import copy

from backend.retrieval import RetrievalService
from backend.retrieval.library_search import search_library
from backend.retrieval.policy import query_profile, rank_evidence
from backend.text_normalization import expand_query


def source(pages, identifier='s'):
    return {'id': identifier, 'project_id': 'p', 'name': '工程运行手册.pdf', 'sha256': identifier * 64,
            'parser_version': 'fixture', 'status': 'ready', 'metadata': {},
            'pages': [{'number': i + 1, 'text': text} for i, text in enumerate(pages)]}


def test_bilingual_aliases_preserve_question_numbers_and_evidence(tmp_path):
    original = source(['The gray body emissivity is lower than unity.', 'The pressure is 30 kPa.'])
    before = copy.deepcopy(original)
    question = '灰体的发射率是什么？压力30 kPa保持不变。'
    profile = expand_query(question)
    assert profile['original'] == question and 'emissivity' in profile['aliases']
    assert '30' not in profile['aliases']
    handle = RetrievalService(tmp_path, default_mode='lexical').prepare(original, 'p')
    result = search_library([handle], '灰体的发射率', limit=1)
    assert result['results'][0]['quote'] == original['pages'][0]['text']
    assert original == before


def test_library_and_answer_share_selection_and_drop_bare_number(tmp_path):
    data = source(['111', 'The training duration is 111 hours.', 'Silt', 'Ordinary material.'])
    handle = RetrievalService(tmp_path, default_mode='lexical').prepare(data, 'p')
    library = search_library([handle], '111 training duration', limit=1)
    actual, _ = handle.select('111 training duration', limit=1)
    assert [x['chunk_id'] for x in library['results']] == [x.id for x in actual]
    assert actual[0].page == 2
    assert library['audit']['selection_policy'] == handle.audit()['searches'][-1]['selection_policy']


def test_contents_and_publication_notice_cannot_displace_body_evidence(tmp_path):
    data = source(['CONTENTS\nPump inspection ............ 9\nPump operation ............ 10\nPump repair ............ 11',
                   'This document has been reproduced directly from the best available copy. Pump inspection manual.',
                   'Inspect the pump casing monthly for signs of leakage.'])
    handle = RetrievalService(tmp_path, default_mode='lexical').prepare(data, 'p')
    result = search_library([handle], 'pump inspection', limit=1)
    assert result['results'][0]['page'] == 3


def test_neighbour_is_separate_anchored_evidence_and_never_escapes_page_filter(tmp_path):
    data = source(['RADIATION HEAT TRANSFER', 'The gray body emissivity modifies radiation heat transfer.', 'Unrelated pump inspection.'])
    handle = RetrievalService(tmp_path, default_mode='lexical').prepare(data, 'p')
    selected, _ = handle.select('radiation heat transfer', limit=2, pages=[1])
    assert {c.page for c in selected} == {1}
    assert not handle.audit()['searches'][-1]['adjacent_candidates']
    chunks = {'a': {'source_key': 's', 'page': 1, 'text': data['pages'][0]['text']},
              'b': {'source_key': 's', 'page': 2, 'text': data['pages'][1]['text']},
              'c': {'source_key': 's', 'page': 3, 'text': data['pages'][2]['text']}}
    ranked, audit = rank_evidence('辐射换热', [('a', 1)], chunks, {'s': data})
    assert {x[0] for x in ranked} == {'a', 'b'}
    assert audit['adjacent_candidates'][0]['chunk_id'] == 'b'
    assert chunks['b']['text'] == data['pages'][1]['text']


def test_table_label_on_page_supports_rows_without_inventing_a_title():
    data = source(['Table 7.2\nSensor calibration\nAnnual maintenance takes two hours.', 'Routine maintenance text.'])
    chunks = {'a': {'source_key': 's', 'page': 1, 'text': 'Annual maintenance takes two hours.'},
              'b': {'source_key': 's', 'page': 2, 'text': 'Routine maintenance text.'}}
    ranked, _ = rank_evidence('表7.2的maintenance', [('b', 1), ('a', .8)], chunks, {'s': data})
    assert ranked[0][0] == 'a'
    assert 'Table' not in chunks['a']['text']


def test_unselected_source_is_never_added_by_title_hint(tmp_path):
    service = RetrievalService(tmp_path, default_mode='lexical')
    selected = service.prepare(source(['The pump requires routine inspection.'], 'a'), 'p')
    service.prepare(source(['The requested secret pump repair procedure.'], 'hidden'), 'p')
    result = search_library([selected], '《secret pump》repair inspection', limit=10)
    assert {row['source_id'] for row in result['results']} <= {'a'}


class CountingVectors:
    identity = {'model': 'controlled-not-production', 'revision': 'one', 'dimension': 2}
    def __init__(self): self.inputs = []
    def embed(self, texts, *, kind, cancelled):
        self.inputs.extend(texts)
        return [[1., 0.] for _ in texts]


def test_rebuild_reuses_only_identical_passage_input_and_model(tmp_path):
    data = source(['Stable pump maintenance.', 'Old temperature reading.'])
    model = CountingVectors()
    service = RetrievalService(tmp_path, model)
    service.prepare(data, 'p')
    updated = copy.deepcopy(data)
    updated['pages'][1]['text'] = 'New temperature reading.'
    model.inputs.clear()
    rebuilt = service.prepare(updated, 'p')
    assert model.inputs == ['\nNew temperature reading.']
    assert rebuilt.audit()['coverage']['reused_identical_passage_vectors'] == 1
    model.inputs.clear()
    model.identity = {**model.identity, 'revision': 'two'}
    service.prepare(updated, 'p')
    assert len(model.inputs) == 2


def test_embedding_cache_is_scoped_to_source_and_project(tmp_path):
    model = CountingVectors(); service = RetrievalService(tmp_path, model)
    service.prepare(source(['Stable pump maintenance.'], 'first'), 'p')
    model.inputs.clear()
    service.prepare(source(['Stable pump maintenance.'], 'second'), 'p')
    assert model.inputs == ['\nStable pump maintenance.']


def test_exact_query_entity_recalls_authorized_case_despite_generic_candidate_saturation():
    data = source(['Generic permit average concentration table.',
                   'East River Works has a design capacity and actual average flow.',
                   'Influent and effluent concentration measurements.',
                   'West River Works has different measurements.'])
    chunks = {str(p['number']): {'source_key': 's', 'page': p['number'], 'text': p['text'],
               'retrieval_hint': 'East River Works' if p['number'] == 3 else ''} for p in data['pages']}
    ranked, audit = rank_evidence('East River Works的平均实际流量和浓度', [('1', 1)], chunks, {'s': data})
    assert {'2', '3'} <= set(audit['exact_query_phrase_candidates'])
    assert ranked[0][0] in {'2', '3'}
    assert '4' not in audit['exact_query_phrase_candidates']
    assert chunks['3']['text'] == data['pages'][2]['text']


def test_original_terms_receive_diverse_evidence_in_compound_question():
    data = source(['A resistive sensor measures resistance.', 'More resistance details for a resistive sensor.',
                   'A thermoelectric sensor measures voltage.'])
    chunks = {str(p['number']): {'source_key': 's', 'page': p['number'], 'text': p['text']} for p in data['pages']}
    ranked, _ = rank_evidence('resistive和thermoelectric传感器的机制？', [('1', 1), ('2', .98), ('3', .85)], chunks, {'s': data}, allow_neighbours=False)
    assert {r[0] for r in ranked[:2]} == {'1', '3'}


def test_coordinated_ellipsis_requires_two_known_terms_and_preserves_original():
    question = '进、出水的TN与TP浓度；流量7.1 MGD。'
    expanded = expand_query(question)
    assert expanded['original'] == question
    assert {'influent', 'effluent'} <= set(expanded['aliases'].split())
    restored = next(m for m in expanded['matches'] if m['term'] == '进水')
    assert restored['basis'] == 'dictionary_confirmed_coordinated_ellipsis'
    assert '7.1' not in expanded['aliases']
    assert 'influent' not in expand_query('红、出水的浓度')['aliases'].split()


def test_confirmed_case_scope_and_literal_indicators_find_separated_table():
    data = source([
        'Overview: East River Works reported effluent TP and TN concentration averages.',
        'East River Works design capacity and actual average flow for the study period.',
        'Figure 2. East River Works process diagram.',
        'Table 8. Influent and effluent averages\nInfluent TP 6\nEffluent TP 0.3\nInfluent TN 30\nEffluent TN 2',
        'West River Works\nTable 8. Influent TP 9\nEffluent TP 1\nInfluent TN 40\nEffluent TN 3',
    ])
    data['metadata']['document_structure'] = {'headings': [
        {'id': 'east', 'structure_status': 'declared', 'inheritance_allowed': True},
        {'id': 'west', 'structure_status': 'declared', 'inheritance_allowed': True}]}
    chunks = {str(p['number']): {'source_key': 's', 'page': p['number'], 'text': p['text'],
               'heading_path': ['East River Works'] if 2 <= p['number'] <= 4 else ['West River Works'],
               'parent_id': 'east' if 2 <= p['number'] <= 4 else 'west',
               'retrieval_hint': 'East River Works' if 2 <= p['number'] <= 4 else ''} for p in data['pages']}
    ranked, audit = rank_evidence('East River Works的设计处理能力、实际流量和研究期进、出水TN和TP浓度',
        [('1', 1), ('5', .95)], chunks, {'s': data})
    assert {'2', '4'} <= {i for i, _ in ranked[:4]}
    assert '4' in audit['confirmed_scope_candidates']
    assert '4' in audit['requested_indicator_table_candidates']
    assert all(item['chunk_id'] != '3' for item in audit['adjacent_candidates'])


def test_candidate_or_unanchored_hint_cannot_gain_confirmed_scope_bonus():
    data = source(['Unrelated operations text.', 'Unrelated operations text.'])
    data['metadata']['document_structure'] = {'headings': [
        {'id': 'candidate', 'structure_status': 'candidate_requires_review', 'inheritance_allowed': False}]}
    chunks = {'a': {'source_key': 's', 'page': 1, 'text': data['pages'][0]['text'],
                    'heading_path': ['East River Works'], 'parent_id': 'candidate', 'retrieval_hint': 'East River Works'},
              'b': {'source_key': 's', 'page': 2, 'text': data['pages'][1]['text'],
                    'heading_path': ['East River Works'], 'retrieval_hint': 'East River Works'}}
    _, audit = rank_evidence('East River Works的平均浓度', [('a', 1), ('b', 1)], chunks, {'s': data})
    assert audit['confirmed_scope_candidates'] == []
