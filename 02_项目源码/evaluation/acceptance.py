"""Resume the frozen acceptance plan against cached sources, with one paid ledger.

Only input_payload reaches the product. Gold never enters this module.
Every model POST is observed under a durable question reservation.
"""
from __future__ import annotations
import argparse
import copy
import json
import time
from pathlib import Path

from .acceptance_budget import AcceptanceBudget
from .build_acceptance_plan import IMMUTABLE_FIELDS
from .core import CASE_KEYS, code_digest, digest, input_payload, record_digest
from .runner import now, register_fixture_provenance, wait_answer_cleanup, write_json

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT.parent / '03_内部资料/提交前功能验收'


def load_plan(path):
    plan = json.loads(Path(path).read_text())
    if plan.get('schema_version') != 'acceptance-plan.v1':
        raise ValueError('Unknown frozen acceptance plan')
    if record_digest({k: plan[k] for k in IMMUTABLE_FIELDS}) != plan['plan_sha256']:
        raise ValueError('Frozen inputs changed')
    for source in plan['sources']:
        if digest(source['path']) != source['sha256']:
            raise ValueError('Frozen source changed: ' + source['id'])
    for case in plan['cases']:
        if set(case) - (CASE_KEYS | {'dynamic_suggestion_from'}):
            raise ValueError('Forbidden question input fields')
    return plan


def parent_plan_verified(plan, entry, entry_path, parent_case_id):
    """Reuse only a frozen, reviewed parent; never rewrite its original plan ID."""
    parent = next((c for c in plan['cases'] if c['id'] == parent_case_id), None)
    source_hashes = {s['id']: s['sha256'] for s in plan['sources']}
    if (not parent or any(s not in source_hashes for s in parent['source_ids'])
            or entry.get('case_id') != parent_case_id
            or entry.get('payload', {}).get('question') != parent['question']
            or entry.get('payload', {}).get('mode') != parent['mode']
            or entry.get('source_sha256') != {s: source_hashes[s] for s in parent['source_ids']}):
        return False
    if entry.get('plan_sha256') == plan['plan_sha256']:
        return True
    matches = [a for a in plan.get('baseline_artifacts', [])
               if a.get('kind') == 'verified_parent_answer'
               and a.get('case_id') == parent_case_id]
    if len(matches) != 1:
        return False
    ref = matches[0]
    review_path = Path(ref.get('review_path', ''))
    return bool(ref.get('independently_verified') is True
        and Path(ref.get('path', '')).resolve() == Path(entry_path).resolve()
        and digest(entry_path) == ref.get('sha256')
        and review_path.is_file() and digest(review_path) == ref.get('review_sha256')
        and entry.get('plan_sha256') == ref.get('original_plan_sha256')
        and entry.get('case_id') == parent_case_id
        and entry.get('record', {}).get('id') == ref.get('answer_id')
        and entry.get('payload', {}).get('question') == parent['question']
        and entry.get('payload', {}).get('mode') == parent['mode']
        and entry.get('source_sha256') == {s: source_hashes[s] for s in parent['source_ids']})


def verified_parent_attempt(result):
    """A completed transport record alone is not an independently correct turn."""
    if not (result.get('assessment') or {}).get('passed'):
        return None
    identifier = result.get('verified_attempt_id')
    matches = [a for a in result.get('attempts', []) if a.get('attempt_id') == identifier]
    return matches[0] if identifier and len(matches) == 1 else None


def public_config(config):
    return {k: copy.deepcopy(config[k]) for k in ('provider', 'endpoint', 'models',
             'timeout', 'vision_timeout', 'reasoning_timeout', 'queue_timeout',
             'timeout_seconds', 'request_timeout_seconds') if k in config}


class FrozenConfig:
    def __init__(self, value): self.value = copy.deepcopy(value)
    def load(self): return copy.deepcopy(self.value)
    def public(self): return {'configured': True, **public_config(self.value)}


def setup_sources(app, client, plan, project_id, output):
    store = app.state.service.store
    if not store.get('projects', project_id): raise ValueError('Known corpus project not found')
    existing = {s['sha256']: s for s in store.sources(project_id)}
    ids, records = {}, []
    for manifest in plan['sources']:
        source = existing.get(manifest['sha256'])
        reused = source is not None
        if not reused:
            path = Path(manifest['path'])
            response = client.post(f'/api/projects/{project_id}/import',
                                   files=[('files', (path.name, path.read_bytes()))])
            response.raise_for_status()
            imported = response.json()
            for job in imported['jobs']: app.state.service.process_job(job['id'])
            source = (imported['sources'] + imported['duplicates'])[0]
            register_fixture_provenance(store, source['id'], manifest)
            source = store.get('sources', source['id'])
        if source['sha256'] != manifest['sha256'] or source['status'] not in ('ready', 'attention'):
            raise ValueError('Source unavailable: ' + manifest['id'])
        ids[manifest['id']] = source['id']
        records.append({'input_id': manifest['id'], 'source_id': source['id'],
                        'sha256': source['sha256'], 'reused_parsing': reused,
                        'status': source['status'], 'pages': len(source.get('pages') or [])})
    mapping = '运行来源映射_' + plan['plan_sha256'][:12] + '.json'
    write_json(output / '原始证据' / mapping,
               {'project_id': project_id, 'runtime': str(app.state.service.root), 'sources': records})
    return ids


def real_suggestion(client, pid, parent, rules):
    response = client.post(f"/api/projects/{pid}/answers/{parent['id']}/suggestions", json={})
    response.raise_for_status(); value = response.json()
    deadline = time.monotonic() + 15
    while value['state'] == 'preparing' and time.monotonic() < deadline:
        time.sleep(.05)
        response = client.get(f"/api/projects/{pid}/suggestions/{value['key']}")
        response.raise_for_status(); value = response.json()
    candidates = [s for s in value.get('suggestions', [])
                  if s.get('action') == rules['eligible_action']
                  and s.get('kind') in rules['preferred_kinds']]
    candidates.sort(key=lambda s: rules['preferred_kinds'].index(s['kind']))
    if not candidates: return None, {'generation': value, 'reason': 'no_eligible_actual_suggestion'}
    selected = candidates[0]
    response = client.post(f"/api/projects/{pid}/suggestions/{value['key']}/accept",
                           json={'suggestion_id': selected['id'], 'expected_scope': value['scope']})
    response.raise_for_status(); accepted = response.json()
    if accepted['fill_text'] != selected['text']: raise ValueError('Accepted suggestion changed')
    return accepted['fill_text'], {'generation': value, 'selected': selected,
                                   'accepted': accepted, 'parent_answer_id': parent['id']}


def run(args):
    from fastapi.testclient import TestClient
    from backend.main import create_app
    from backend.model_gateway import ModelConfig, ModelGateway
    from .complex_corpus import make_retrieval
    output = args.output.resolve(); path = args.plan.resolve() if args.plan else output / '用例与结果.json'
    plan = load_plan(path)
    budget = AcceptanceBudget(output / '调用与轮次账本.jsonl')
    if any(not a['closed'] for a in budget.snapshot()['attempts']):
        raise RuntimeError('An earlier reservation is unresolved; inspect its persisted answer before any new paid work')
    config = ModelConfig(args.config_runtime).load()
    config['models'] = {'table': args.table_model, 'document': args.document_model, 'verify': args.verify_model}
    # Freeze effective defaults as well as explicit connection settings.
    from backend.provider_pacing import _queue_timeout
    config['timeout'] = min(120, max(5, int(config.get('timeout', 90))))
    config.setdefault('vision_timeout', max(config['timeout'], 180))
    config['queue_timeout'] = _queue_timeout(config)
    config_hash = record_digest(public_config(config))
    config_file = output / '原始证据' / ('模型配置_' + config_hash[:12] + '.json')
    if not config_file.exists():
        write_json(config_file, {'config_sha256': config_hash, 'frozen_at': now(),
                   'configuration': public_config(config), 'credentials': 'read_only_existing_connection_not_exported'}, exclusive=True)
    active = {'attempt': None}
    def gateway_factory(value, cancelled):
        attempt = active['attempt']
        if not attempt: raise RuntimeError('No durable paid reservation')
        return ModelGateway(value, cancelled, request_observer=lambda event, meta: budget.observe(attempt, event, meta))
    app = create_app(args.runtime, args.runtime / 'no-new-demo-samples', False, gateway_factory,
                     lambda _: make_retrieval(args.runtime, True)[0])
    app.state.answers.config = FrozenConfig(config)
    selected = set(args.cases.split(',')) if args.cases else None
    if selected and selected - {c['id'] for c in plan['cases']}: raise ValueError('Unknown planned case')
    if args.retry and not args.reason: raise ValueError('An explicit reason is required to repeat a question')
    def save():
        plan['budget_snapshot'] = budget.snapshot()
        write_json(path, plan)
        path.with_suffix('.json.sha256').write_text(digest(path) + '  ' + path.name + '\n')
    with TestClient(app) as client:
        ids = setup_sources(app, client, plan, args.project_id, output)
        plan['runtime_project_id'] = args.project_id
        if args.prepare_only:
            save(); print(json.dumps({'prepared_sources': len(ids), 'paid_rounds': budget.snapshot()['used_rounds']})); return
        for frozen_case in plan['cases']:
            cid = frozen_case['id']; result = plan['results'][cid]
            if selected and cid not in selected: continue
            if result['attempts'] and not args.retry: continue
            case = copy.deepcopy(frozen_case)
            parent = None
            if case.get('parent_case_id'):
                parent_attempt = verified_parent_attempt(plan['results'][case['parent_case_id']])
                if parent_attempt:
                    parent_path = output / parent_attempt['file']
                    pentry = json.loads(parent_path.read_text())
                    if (digest(parent_path) != parent_attempt.get('sha256')
                            or not parent_plan_verified(plan, pentry, parent_path, case['parent_case_id'])
                            or pentry.get('config_sha256') != config_hash or not pentry.get('audit_settled')):
                        raise ValueError('Parent evidence is unsettled or belongs to another plan/configuration')
                    parent = pentry.get('record')
                if not parent or parent['status'] not in ('completed', 'needs_clarification', 'insufficient_evidence'):
                    result.update(status='blocked_by_parent'); save(); continue
            suggestion = None
            if case.get('dynamic_suggestion_from'):
                question, suggestion = real_suggestion(client, args.project_id, parent, plan['dynamic_suggestion_rules'])
                if question is None:
                    result.update(status='no_eligible_suggestion', suggestion_evidence=suggestion); save(); continue
                case['question'] = question
            payload = input_payload(case, ids, parent['id'] if parent else None,
                                    parent.get('context_revision') if parent else None)
            code_hash = code_digest(ROOT)
            attempt = budget.reserve(cid, code_sha256=code_hash, config_sha256=config_hash,
                                     parent_answer_id=parent['id'] if parent else None)
            active['attempt'] = attempt
            file = '原始证据/真实问答/' + attempt + '.json'
            entry = {'case_id': cid, 'attempt_id': attempt, 'started_at': now(),
                     'code_sha256': code_hash, 'config_sha256': config_hash,
                     'plan_sha256': plan['plan_sha256'], 'payload': payload,
                     'parent_case_id': case.get('parent_case_id'), 'retry_reason': args.reason,
                     'source_sha256': {s['id']: s['sha256'] for s in plan['sources'] if s['id'] in case['source_ids']}}
            if suggestion: entry['suggestion_evidence'] = suggestion
            result['attempts'].append({'attempt_id': attempt, 'file': file, 'started_at': entry['started_at']})
            result['status'] = 'running'; write_json(output / file, entry, exclusive=True); save()
            started = time.monotonic(); settled = True; record = None
            try:
                response = client.post(f'/api/projects/{args.project_id}/answers', json=payload)
                response.raise_for_status(); aid = response.json()['id']
                entry['answer_id'] = aid; write_json(output / file, entry)
                while True:
                    response = client.get(f'/api/projects/{args.project_id}/answers/{aid}')
                    response.raise_for_status(); record = response.json()
                    if record['status'] not in ('queued', 'running'): break
                    if time.monotonic() - started > args.timeout:
                        client.post(f'/api/projects/{args.project_id}/answers/{aid}/cancel').raise_for_status()
                        entry['timeout_cancel_requested'] = True; break
                    time.sleep(.2)
                settled = wait_answer_cleanup(app.state.answers, aid, timeout=30)
                response = client.get(f'/api/projects/{args.project_id}/answers/{aid}/export?format=json')
                response.raise_for_status(); exported = response.json()
                record = next(a for a in exported['answers'] if a['id'] == aid)
                entry.update(record=record, execution_events=exported.get('execution_events', {}).get(aid, []))
            except Exception as exc:
                entry['runner_error_type'] = type(exc).__name__
                if entry.get('answer_id'):
                    settled = wait_answer_cleanup(app.state.answers, entry['answer_id'], timeout=30)
            status = record['status'] if record else 'runner_failed'
            entry.update(finished_at=now(), elapsed_ms=round(1000*(time.monotonic()-started)),
                         audit_settled=settled, final_code_sha256=code_digest(ROOT))
            entry['code_changed_during_attempt'] = entry['final_code_sha256'] != code_hash
            write_json(output / file, entry)
            if settled: budget.finish(attempt, status)
            active['attempt'] = None
            result.update(status=status, assessment=None)
            result['attempts'][-1].update(status=status, sha256=digest(output / file))
            save()
            print(json.dumps({'case': cid, 'status': status, 'seconds': round(entry['elapsed_ms']/1000, 1),
                              'used_rounds': budget.snapshot()['used_rounds'], 'model_requests': budget.snapshot()['post_started']}, ensure_ascii=False), flush=True)
            http_codes = {m.get('http_status') for m in (record or {}).get('model_attempts', [])}
            stop = ('audit_unsettled' if not settled else 'code_changed_during_attempt' if entry['code_changed_during_attempt']
                    else 'connection_or_compatibility' if http_codes & {400, 401, 402, 403, 404, 429, 500, 502, 503, 504}
                    else 'runner_failure' if entry.get('runner_error_type') else None)
            if stop:
                plan['stopped_reason'] = stop; save(); break
        plan['status'] = 'execution_pending_review'; save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--plan', type=Path, help='Optional separately frozen supplemental plan; shares the same paid ledger')
    parser.add_argument('--runtime', type=Path, default=ROOT / '.runtime-complex-corpus')
    parser.add_argument('--project-id', default='4e6a1f6feae243cbafd3a3de59b8cf4c')
    parser.add_argument('--config-runtime', type=Path, required=True)
    parser.add_argument('--table-model', default='deepseek-flash')
    parser.add_argument('--document-model', default='deepseek-flash')
    parser.add_argument('--verify-model', default='deepseek-flash')
    parser.add_argument('--cases')
    parser.add_argument('--timeout', type=float, default=900)
    parser.add_argument('--retry', action='store_true')
    parser.add_argument('--reason')
    parser.add_argument('--prepare-only', action='store_true')
    run(parser.parse_args())


if __name__ == '__main__': main()
