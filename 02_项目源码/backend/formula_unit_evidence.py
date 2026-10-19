"""Narrow, source-bound normalization of an explicitly dimensionless parameter.

This does not infer a unit from a formula, a question, or a missing unit field.
An independently reviewed visual parameter AND its same-page assignment sentence
are both required. Original transcriptions and evidence are never modified.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re

VERSION = 'explicit-dimensionless-assignment-v2'
MAX_EVIDENCE = 128
MAX_TEXT = 26000
MAX_TOTAL_TEXT = 65000
_POSITION = ('source_id', 'sha256', 'parser_version', 'page')
_NUMBER = r'[-+−]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?'
_SYMBOL = r'[A-Za-z][A-Za-z0-9_]{0,39}'
_ASSIGNMENT = re.compile(r'(?<![A-Za-z0-9_])(?P<symbol>' + _SYMBOL + r')\s*[=＝]\s*(?P<value>' + _NUMBER + r')(?![\d.])')
_TOKEN = _SYMBOL + r'\s*[=＝]\s*' + _NUMBER
_DECLARATION = r'(?:均|都|皆)?(?:为|是)(?:明确)?无量纲(?:数|量|参数)?'
_SENTENCE = re.compile(r'\s*(?:本页(?:参数)?|(?:以下|本组)?参数)?\s*(?P<assignments>'
    + _TOKEN + r'(?:\s*[、,，]\s*' + _TOKEN + r')*)\s*[，,]\s*(?P<declaration>'
    + _DECLARATION + r')\s*')
# A PDF line break is not a semantic boundary: otherwise "仅对照组:\n"
# could be dropped while the following assignment is accepted as unqualified.
_CLAUSE = re.compile(r'[^。！？!?；;]+')


def _reviewed_quote_span(text, span, quote):
    """Match source bytes, allowing one present declarative terminator only.

    A quote that includes punctuation must contain the whole affirmative
    clause. Never strip an internal boundary, qualification, or negation;
    questions and exclamations are not normalized into declarations.
    """
    sentence = span.group()
    if quote in sentence:
        start = span.start() + sentence.index(quote)
        return start, start + len(quote), span.end()
    if (quote[-1:] not in ('。', '；', ';')
            or quote[:-1] != sentence.strip()
            or text[span.end():span.end() + 1] != quote[-1:]):
        return None
    start = span.start() + len(sentence) - len(sentence.lstrip())
    end = span.end() + 1
    if text[start:end] != quote:
        return None
    return start, end, end


def _same_position(left, right):
    return all(left.get(key) == right.get(key) for key in _POSITION)


def _same_number(left, right):
    try:
        return Decimal(left.replace('−', '-')) == Decimal(right.replace('−', '-'))
    except (InvalidOperation, AttributeError):
        return False


def explicit_dimensionless_parameter(record, evidence):
    """Return a bounded provenance receipt, or None when proof is incomplete.

    Accepted grammar is deliberately narrow: an affirmative assignment list such
    as ``本页a=6、b=4，均为明确无量纲数``. Conditional, negative, named-experiment,
    quoted, or otherwise qualified sentences require existing semantic handling;
    they are not shortened to the matching words here.
    """
    if not isinstance(record, dict) or not isinstance(evidence, dict) or len(evidence) > MAX_EVIDENCE:
        return None
    item = record.get('parameter')
    if (record.get('kind') != 'formula_parameter' or record.get('stale') is True
            or record.get('provenance') != 'separate_formula_visual_read_and_review'
            or record.get('visual_checked') is not True or record.get('formula_verified') is not True
            or not isinstance(item, dict) or item.get('verified') is not True
            or item.get('unit') is not None or item.get('unit_status') != 'unknown'
            or item.get('binding_status') != 'visually_supported_candidate'
            or item.get('period') or item.get('scope')
            or any(not isinstance(record.get(key), str) or not record[key]
                   for key in ('formula_id', 'parameter_id'))):
        return None
    position = item.get('source_position')
    if (not isinstance(position, dict) or not _same_position(record, position)
            or any(not isinstance(record.get(key), str) or not record[key] for key in _POSITION[:-1])
            or type(record.get('page')) is not int or record['page'] < 1):
        return None
    symbol, value, quote, declaration = (item.get(key) for key in ('symbol', 'value', 'quote', 'unit_evidence'))
    if (not isinstance(symbol, str) or not re.fullmatch(_SYMBOL, symbol)
            or symbol in record.get('formula_parameter_conflicts', [])
            or not isinstance(value, str) or len(value) > 80 or not re.fullmatch(_NUMBER, value)
            or not isinstance(quote, str) or not 1 <= len(quote) <= 1000
            or not isinstance(declaration, str) or not 1 <= len(declaration) <= 500):
        return None
    # A declared unit of "1" is not invented and never overwrites item.unit.
    # Generic "unit missing" or a bare word cut from a negative clause is not proof.
    texts, total = [], 0
    for identifier, row in evidence.items():
        if not isinstance(row, dict) or row.get('kind') != 'text' or not _same_position(record, row):
            continue
        text = row.get('quote')
        if (row.get('stale') is True or not isinstance(text, str) or len(text) > MAX_TEXT
                or not isinstance(identifier, str) or not identifier or len(identifier) > 160):
            return None
        total += len(text)
        if total > MAX_TOTAL_TEXT:
            return None  # Refuse, never silently scan a prefix and claim full proof.
        texts.append((identifier, row, text))
    candidates = []
    for identifier, row, text in texts:
        assignments = [m for m in _ASSIGNMENT.finditer(text) if m['symbol'] == symbol]
        if not assignments:
            continue
        # A second assignment may be another period/experiment. Do not choose one
        # just because its numeric value happens to equal this parameter.
        if len(assignments) != 1 or not _same_number(assignments[0]['value'], value):
            return None
        assignment = assignments[0]
        matched = False
        for span in _CLAUSE.finditer(text):
            if not span.start() <= assignment.start() < span.end():
                continue
            sentence = span.group()
            grammar = _SENTENCE.fullmatch(sentence)
            quote_span = _reviewed_quote_span(text, span, quote)
            if not grammar or quote_span is None or declaration.strip() not in sentence:
                return None
            # The reviewed unit evidence must contain the complete affirmative
            # declaration, or be the source sentence containing it.
            phrase = grammar['declaration']
            if phrase not in declaration or declaration.strip() not in sentence:
                return None
            listed = list(_ASSIGNMENT.finditer(grammar['assignments']))
            if len({m['symbol'] for m in listed}) != len(listed):
                return None
            matched = True
            candidates.append({'policy': VERSION, 'original_unit': None,
                'original_unit_status': item['unit_status'], 'normalized_unit': '1',
                'meaning': 'dimensionless_explicitly_declared',
                'parameter_id': record.get('parameter_id'), 'formula_id': record.get('formula_id'),
                'symbol': symbol, 'original_value': value, 'reviewed_unit_evidence': declaration,
                'evidence_id': identifier, 'source_position': {k: record[k] for k in _POSITION},
                'quote': text[span.start():quote_span[2]],
                'start': span.start(), 'end': quote_span[2],
                'reviewed_parameter_quote': quote,
                'reviewed_quote_start': quote_span[0], 'reviewed_quote_end': quote_span[1],
                'offset_scope': 'evidence_quote',
                'assignment_start': assignment.start(), 'assignment_end': assignment.end(),
                'independent_visual_review_required': True, 'truncated': False})
        if not matched:
            return None
    if not candidates:
        return None
    # Repeated retrieval of the same sentence is fine; two different declarations
    # are not combined to infer a scope. All surviving proofs are identical text.
    if len({entry['quote'] for entry in candidates}) != 1:
        return None
    return candidates[0]
