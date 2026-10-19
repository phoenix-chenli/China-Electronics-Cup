"""Versioned, factual answer projections; never reclassify a saved outcome.

The legacy engine fields remain the source of truth. Derived presentation fields
carry availability and provenance rather than inventing missing historical data.
This module has no model calls and does not expose a model's private reasoning.
"""
from __future__ import annotations

import copy
import hashlib
import json

ANSWER_SCHEMA = 'zhiheng.answer.v6'
RESULT_SCHEMA = 'zhiheng.answer-result.v1'


def revision(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def initial_fields(sources):
    return dict(schema_version=ANSWER_SCHEMA, context_revision=None,
                source_revisions=copy.deepcopy(sources), suggestions=[],
                suggestion_state='hidden')


def _location(evidence):
    """Report the precision actually recorded, not the best possible UI overlay."""
    if evidence.get('localization_scope') == 'original_page':
        return 'page'
    if evidence.get('query_result') or evidence.get('lineage') == 'table' or evidence.get('query'):
        return 'query_scope'
    if evidence.get('row_numbers'):
        return 'table_rows'
    if evidence.get('table_name'):
        return 'table'
    if (type(evidence.get('start')) is int and type(evidence.get('end')) is int
            and evidence['end'] > evidence['start'] and evidence.get('quote')):
        return 'text_span'
    if evidence.get('page') is not None:
        return 'page'
    return 'unspecified'


def evidence_graph(result):
    """Build links from saved identifiers only; dangling references stay visible."""
    nodes = {}; edges = []; unresolved = []

    def node(key, kind, **values):
        nodes.setdefault(key, dict(id=key, kind=kind, **values))

    def edge(start, end, relation):
        edges.append(dict(source=start, target=end, relation=relation))

    evidence_ids = {str(e['id']) for e in result.get('evidence', []) if e.get('id')}
    calculations = result.get('calculations') or []
    calculation_ids = {str(c['id']) for c in calculations if c.get('id')}
    for e in result.get('evidence', []):
        if not e.get('id'):
            unresolved.append(dict(kind='missing_evidence_id'))
            continue
        eid = 'evidence:' + str(e['id'])
        sid = 'source:' + str(e.get('source_id') or 'unknown')
        # A source node includes the original digest and parser version. It does
        # not infer independence from a different file name or converted format.
        node(sid, 'source', source_id=e.get('source_id'), sha256=e.get('sha256'),
             parser_version=e.get('parser_version'))
        node(eid, 'evidence', evidence_id=e['id'], precision=_location(e),
             page=e.get('page'), table_name=e.get('table_name'))
        edge(sid, eid, 'supports')
    for calculation in calculations:
        if not calculation.get('id'):
            continue
        cid = 'calculation:' + str(calculation['id'])
        node(cid, 'calculation', calculation_id=calculation['id'])
        for identifier in calculation.get('evidence_ids', []):
            if str(identifier) in evidence_ids:
                edge('evidence:' + str(identifier), cid, 'input')
            else:
                unresolved.append(dict(kind='calculation_evidence', owner=calculation['id'], reference=identifier))
    for claim in result.get('claims', []):
        if not claim.get('id'):
            unresolved.append(dict(kind='missing_claim_id'))
            continue
        cid = 'claim:' + str(claim['id'])
        # A calculation link proves derivation; lack of a link does not prove an
        # original quotation. Keep unknown origins explicit.
        node(cid, 'claim', claim_id=claim['id'],
             origin='calculated' if claim.get('calculation_ids') else 'evidence_backed' if claim.get('evidence_ids') else 'unspecified')
        for identifier in claim.get('evidence_ids', []):
            if str(identifier) in evidence_ids:
                edge('evidence:' + str(identifier), cid, 'supports')
            else:
                unresolved.append(dict(kind='claim_evidence', owner=claim['id'], reference=identifier))
        for identifier in claim.get('calculation_ids', []):
            if str(identifier) in calculation_ids:
                edge('calculation:' + str(identifier), cid, 'derives')
            else:
                unresolved.append(dict(kind='claim_calculation', owner=claim['id'], reference=identifier))
        node('answer', 'answer')
        edge(cid, 'answer', 'included_in')
    return dict(schema_version='zhiheng.evidence-graph.v1', nodes=list(nodes.values()),
                edges=edges, unresolved=unresolved,
                source_groups=copy.deepcopy(result.get('source_groups', [])))


def prepare_result(result, status):
    """Materialize v6 additions once, while keeping the engine result untouched."""
    value = copy.deepcopy(result)
    value['schema_version'] = RESULT_SCHEMA
    tables = []
    # Rejected execution snapshots are deliberately excluded. They remain only
    # in query.executed_result, never in this presentation table collection.
    if status == 'completed' and isinstance(value.get('columns'), list) and value['columns'] and isinstance(value.get('rows'), list):
        table = dict(id='result-main', columns=value['columns'], rows=value['rows'],
                     provenance='saved_completed_result', verification_status=(value.get('verification') or {}).get('status','not_recorded'),
                     evidence_ids=[e['id'] for e in value.get('evidence', []) if e.get('id')],
                     query=copy.deepcopy(value.get('query')))
        if isinstance(value.get('display_rows'), list):
            table['display_rows'] = value['display_rows']
        tables.append(table)
    value['result_tables'] = tables
    value['coverage'] = copy.deepcopy(value.get('completeness')) if isinstance(value.get('completeness'), dict) else {
        'status': 'not_recorded', 'complete': None, 'items': []}
    value['evidence_graph'] = evidence_graph(value)
    return value


def public_record(item, events=None):
    """Additive read adapter: old statuses and original records are immutable."""
    value = copy.deepcopy(item)
    legacy = value.get('schema_version') != ANSWER_SCHEMA
    value.setdefault('schema_version', 'zhiheng.answer.legacy')
    value.setdefault('context_revision', None)
    value.setdefault('source_revisions', copy.deepcopy(value.get('sources') or []))
    value.setdefault('suggestions', [])
    value.setdefault('suggestion_state', 'hidden')
    value['contract_availability'] = dict(legacy=legacy, context='recorded' if value.get('context_revision') else 'not_recorded',
                                         events='recorded' if events else 'not_recorded',
                                         semantic_scope='recorded' if value.get('sources') and all(s.get('semantic_revision') for s in value['sources']) else 'not_recorded')
    value['events'] = events or []
    if value.get('result') and 'schema_version' not in value['result']:
        value['result'] = prepare_result(value['result'], value['status'])
        value['result']['projection_only'] = True
    if value.get('result'):
        # This additive display audit does not rewrite historical claims,
        # statuses or their stored model verdicts. It prevents an old false
        # positive from continuing to look like a complete verified answer.
        result = value['result']
        if value.get('mode') in {'document', 'multi_source'}:
            from .calculation_requirements import assess_coverage
            _, current_audit = assess_coverage(value.get('question') or '', result.get('claims') or [],
                result.get('calculations') or [], result.get('completeness') or result.get('coverage') or {})
            recorded_audit = result.get('calculation_completeness')
            if (not isinstance(recorded_audit, dict)
                    or any(recorded_audit.get(key) != current_audit[key] for key in ('version','status','requirements','issues'))):
                result['calculation_completeness'] = {**current_audit, 'projection_only': True}
        else:
            result.setdefault('calculation_completeness', {'version':'calculation-coverage-1',
                'status':'not_required', 'requirements':[], 'issues':[], 'scope':'table_or_database_execution_contract'})
    return value
