"""Scripted model tests are protocol/safety evidence, not live model accuracy."""
import base64
import copy
import io
import json

from PIL import Image
import pytest

from backend import formula_vision as fv
from backend.model_gateway import ModelError


def text(value):
    return {'type': 'text', 'text': value}


def candidate():
    return {'id': 'F1', 'label': '式(1)', 'tree': {'type': 'row', 'children': [text('R='),
        {'type': 'fraction', 'numerator': text('a+b'), 'denominator': text('c'), 'layout': 'bar'}]},
        'parameters': [parameter('P1', 'a', '12', 'kWh'), parameter('P2', 'b', '8', 'kWh'), parameter('P3', 'c', '4', 'm²')],
        'limitations': []}


def parameter(identity='P1', symbol='a', value='12', unit='kWh'):
    return {'id': identity, 'symbol': symbol, 'value': value, 'unit': unit,
            'quote': f'{symbol}={value}{unit or ""}', 'unit_evidence': ''}


def reading(row=None):
    return {'formulas': [row or candidate()], 'limitations': []}


def review(*, formula=True, a=True, b=True, c=True):
    return {'items': [{'id': identity, 'supported': supported} for identity, supported in
                      [('F1', formula), ('F1.P1', a), ('F1.P2', b), ('F1.P3', c)]]}


class Gateway:
    def __init__(self, *responses):
        self.responses, self.calls, self.attempts = list(responses), [], []
        self.config = {'api_key': 'DO_NOT_EXPOSE_KEY', 'endpoint': 'https://secret.invalid'}
    def __call__(self, messages, purpose):
        self.calls.append((copy.deepcopy(messages), purpose))
        self.attempts.append({'purpose': purpose, 'requested_model': 'requested-' + purpose, 'reported_model': 'reported-' + purpose,
                              'elapsed_ms': 2, 'api_key': 'DO_NOT_EXPOSE_KEY', 'status': 'completed'})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(messages, purpose)
        return copy.deepcopy(response)


@pytest.fixture
def source(monkeypatch):
    output = io.BytesIO()
    Image.new('RGB', (120, 80), 'white').save(output, format='JPEG')
    pixels = {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(output.getvalue()).decode(), 'detail': 'high'}}
    monkeypatch.setattr(fv, 'page_content', lambda *a, **kw: [copy.deepcopy(pixels)])
    return {'id': 'S1', 'name': '公式.pdf', 'format': 'pdf', 'sha256': 'a' * 64, 'parser_version': '3.0',
            'original_path': '/private/source.pdf', 'pages': [{'number': 1, 'text': 'OCR错字a=99', 'blocks': []}]}


def test_two_roles_use_same_pixels_and_only_reviewed_source_structure_is_returned(source):
    before = copy.deepcopy(source)
    gateway = Gateway(reading(), review())
    result = fv.read_formula_page(source, 1, '识别公式及参数。', gateway)
    assert result['status'] == 'reviewed'
    assert [purpose for _, purpose in gateway.calls] == ['document', 'verify']
    assert gateway.calls[0][0][1]['content'][-1] == gateway.calls[1][0][1]['content'][-1]
    record = result['formulas'][0]
    assert record['verified'] and not record['execution_eligible']
    assert record['computation_ast']['right']['left']['operator'] == '+'
    assert record['missing_parameters'] == [] and record['parameter_conflicts'] == []
    assert record['source_position']['bbox'] == [0, 0, 1, 1]
    assert record['source_position']['localization_scope'] == 'original_page'
    assert record['parameter_candidates'][0]['quote'] == 'a=12kWh'
    assert source == before
    assert 'OCR错字' not in str(gateway.calls)
    serialized = json.dumps(result)
    assert 'DO_NOT_EXPOSE' not in serialized and 'secret.invalid' not in serialized and '/private/' not in serialized
    assert 'base64' not in serialized and len(result['audit']['original_page_image_sha256']) == 64


def test_supported_formula_with_false_parameter_reports_exact_missing_symbol(source):
    gateway = Gateway(reading(), review(b=False))
    result = fv.read_formula_page(source, 1, '识别参数。', gateway)
    row = result['formulas'][0]
    assert row['missing_parameters'] == ['b']
    assert [p['symbol'] for p in row['parameter_candidates']] == ['a', 'c']
    assert result['review']['verdicts'][2] == {'id': 'F1.P2', 'supported': False}
    assert len(gateway.calls) == 2 and not row['execution_eligible']


def test_semantically_false_formula_is_not_retried_or_executed(source):
    gateway = Gateway(reading(), review(formula=False))
    result = fv.read_formula_page(source, 1, '识别公式。', gateway)
    assert result['status'] == 'insufficient_evidence' and result['formulas'] == []
    assert result['review']['valid'] and len(gateway.calls) == 2
    assert result['audit']['candidates'][0] == candidate()
    assert result['audit']['candidates_are_evidence'] is False


@pytest.mark.parametrize('bad', [{'items': []}, {'items': [{'id': 'F1', 'supported': 'true'}]},
    {'items': [{'id': 'F1', 'supported': True}] * 4}, {'items': [{'id': 'bad', 'supported': True}] * 4}])
def test_invalid_incomplete_or_duplicate_review_cannot_publish(source, bad):
    gateway = Gateway(reading(), bad, bad)
    result = fv.read_formula_page(source, 1, '读取公式。', gateway)
    assert result['status'] == 'protocol_failed' and result['formulas'] == []
    assert [p for _, p in gateway.calls] == ['document', 'verify', 'verify']


def test_protocol_correction_is_bounded_and_cannot_mutate_supported_source(source):
    wrong = candidate(); wrong['tree']['children'][1]['layout'] = 'guess'
    gateway = Gateway(reading(wrong), reading(), review())
    result = fv.read_formula_page(source, 1, '识别公式。', gateway)
    assert result['status'] == 'reviewed'
    assert [p for _, p in gateway.calls] == ['document', 'document', 'verify']
    payload = json.loads(gateway.calls[1][0][1]['content'][0]['text'])
    assert payload['protocol_correction'] == ['unknown_fraction_layout']
    assert result['audit']['attempts'][0]['status'] == 'protocol_failed'


@pytest.mark.parametrize('code', ['timeout', 'network_failed', 'http_failed'])
def test_unknown_post_or_transport_failure_is_never_resent(source, code):
    gateway = Gateway(ModelError('secret failure', code))
    with pytest.raises(ModelError) as error:
        fv.read_formula_page(source, 1, '识别公式。', gateway)
    assert error.value.code == code and len(gateway.calls) == 1


def test_cancel_before_request_and_between_read_and_review(source):
    gateway = Gateway(reading())
    with pytest.raises(InterruptedError):
        fv.read_formula_page(source, 1, '识别公式。', gateway, cancelled=lambda: True)
    assert gateway.calls == []
    cancelled = [False]
    def respond(messages, purpose):
        cancelled[0] = True
        return reading()
    gateway = Gateway(respond)
    with pytest.raises(InterruptedError):
        fv.read_formula_page(source, 1, '识别公式。', gateway, cancelled=lambda: cancelled[0])
    assert len(gateway.calls) == 1


def test_formula_and_context_budgets_fail_without_invented_truncation(source, monkeypatch):
    rows = {'formulas': [dict(candidate(), id=f'F{i}') for i in range(5)], 'limitations': []}
    gateway = Gateway(rows, rows)
    assert fv.read_formula_page(source, 1, '识别公式。', gateway)['status'] == 'protocol_failed'
    monkeypatch.setattr(fv, 'MAX_CONTEXT', 100)
    unused = Gateway()
    with pytest.raises(ValueError, match='上下文'):
        fv.read_formula_page(source, 1, '识别公式。', unused)
    assert unused.calls == []


def test_missing_unit_does_not_become_dimensionless_and_conflicts_remain_visible(source):
    row = candidate()
    row['parameters'][0] = parameter('P1', 'a', '12', None)
    row['parameters'][1] = parameter('P2', 'c', '5', 'm²')
    result = fv.read_formula_page(source, 1, '识别参数。', Gateway(reading(row), review()))
    record = result['formulas'][0]
    assert record['missing_parameters'] == ['a', 'b']
    assert record['parameter_conflicts'] == ['c']
    assert record['parameter_candidates'][0]['unit_status'] == 'unknown'


@pytest.mark.parametrize('modification', [lambda p: p.update(value='99'), lambda p: p.update(unit='MWh'), lambda p: p.update(value=True)])
def test_parameter_value_and_unit_need_real_candidate_quote_before_review(source, modification):
    row = candidate(); modification(row['parameters'][0])
    gateway = Gateway(reading(row), reading(row))
    result = fv.read_formula_page(source, 1, '识别参数。', gateway)
    assert result['status'] == 'protocol_failed' and len(gateway.calls) == 2
    assert all(p == 'document' for _, p in gateway.calls)


def test_empty_visual_read_is_visible_missing_evidence_without_verify_call(source):
    gateway = Gateway({'formulas': [], 'limitations': ['分数线模糊，不能区分分母范围。']})
    result = fv.read_formula_page(source, 1, '识别公式。', gateway)
    assert result['status'] == 'insufficient_evidence' and len(gateway.calls) == 1
    assert result['limitations'] == ['分数线模糊，不能区分分母范围。']


def test_wrong_model_bbox_cannot_enter_formal_location(source):
    row = candidate(); row['bbox'] = [.1, .1, .4, .4]
    gateway = Gateway(reading(row), reading(row))
    result = fv.read_formula_page(source, 1, '读取公式。', gateway)
    assert result['status'] == 'protocol_failed' and not result['formulas']


def test_relation_not_direct_assignment_does_not_claim_a_supported_solver(source):
    row = candidate(); row['tree'] = text('a+b=c')
    result = fv.read_formula_page(source, 1, '读取公式。', Gateway(reading(row), review()))
    assert result['formulas'][0]['computation_status'] == 'verified_relation_requires_solver_not_supported'
    assert not result['formulas'][0]['execution_eligible']


def test_source_formula_number_is_not_a_parameter_value(source):
    row = candidate(); row['label'] = '式(999)'
    result = fv.read_formula_page(source, 1, '读取公式。', Gateway(reading(row), review()))
    assert '999' not in str(result['formulas'][0]['computation_ast'])
    assert all(p['value'] != '999' for p in result['formulas'][0]['parameter_candidates'])


def test_rejected_candidate_audit_resolves_ids_but_redacts_credentials(source):
    row = candidate(); row['label'] = 'api_key=FAKE_CREDENTIAL_FOR_TEST'
    result = fv.read_formula_page(source, 1, '识别公式。', Gateway(reading(row), review(formula=False)))
    assert result['audit']['candidates'][0]['id'] == 'F1'
    assert result['audit']['candidates_redacted'] is True
    assert 'FAKE_CREDENTIAL_FOR_TEST' not in json.dumps(result)
    assert result['audit']['candidates_are_evidence'] is False
