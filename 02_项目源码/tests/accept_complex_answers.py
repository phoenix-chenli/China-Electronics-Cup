"""Reproduce complex-query acceptance in a new isolated runtime.

Explicit --run-live is required for model requests. --reassess is offline and
only evaluates saved records. Public databases are teaching samples; stress is
synthetic. First failures and later attempts are retained, never overwritten.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = ROOT / 'tests' / 'fixtures' / 'complex_queries'
FORMAT = 'zhiheng-complex-acceptance-v1'
sys.path.insert(0, str(ROOT))


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def case_digest(case):
    return hashlib.sha256(json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def matching(actual, expected, tolerance):
    if actual is None or expected is None:
        return actual is None and expected is None
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        if isinstance(actual, bool):
            return False
        try:
            a, b = Decimal(str(actual)), Decimal(str(expected))
            return a.is_finite() and b.is_finite() and abs(a - b) <= Decimal(str(tolerance))
        except (ValueError, TypeError, InvalidOperation):
            return False
    return actual == expected


def assess(case, record):
    expected = case['expected_status']
    allowed = expected if isinstance(expected, list) else [expected]
    if record.get('status') not in allowed:
        return False, 'status: ' + str(record.get('status'))
    result = record.get('result') or {}
    actual = result.get('rows') or []
    if case.get('expected') is None:
        return (False, 'unexpected numeric result') if actual else (True, 'expected clarification or missing evidence')
    want = case['expected']['rows']
    if len(actual) != len(want):
        return False, f'row count {len(actual)} != {len(want)}'
    if not want and not result.get('query', {}).get('sql'):
        return False, 'empty-result answer lacks an executed query'
    comparison = case['comparison']
    tolerance = comparison.get('numeric_absolute_tolerance', 0.005)
    def same(a, b):
        return isinstance(a, (list, tuple)) and len(a) == len(b) and all(matching(x, y, tolerance) for x, y in zip(a, b))
    if comparison.get('rows') == 'ordered':
        return (True, 'ordered values match') if all(same(a, b) for a, b in zip(actual, want)) else (False, 'ordered values differ')
    remaining = list(actual)
    for row in want:
        index = next((i for i, value in enumerate(remaining) if same(value, row)), None)
        if index is None:
            return False, 'values differ'
        remaining.pop(index)
    return True, 'value multiset matches'


def load_spec(fixtures, spec_name):
    path = (fixtures / spec_name).resolve()
    if not path.is_relative_to(fixtures) or path.suffix != '.json':
        raise ValueError('--spec must identify a JSON file inside --fixtures')
    spec = json.loads(path.read_text(encoding='utf-8'))
    checksum = path.with_suffix('.sha256')
    if not checksum.is_file() or checksum.read_text().split()[0] != digest(path):
        raise ValueError('Frozen specification SHA-256 does not match')
    if not isinstance(spec.get('cases'), list):
        raise ValueError('Specification has no cases')
    return spec, path


def select_cases(spec, only):
    cases = spec['cases']
    chains = spec.get('conversation_chains', [])
    known = {c['id'] for c in cases + chains}
    if only and set(only) - known:
        raise ValueError('Unknown --only identifiers: ' + ', '.join(sorted(set(only) - known)))
    return ([c for c in cases if not only or c['id'] in only], [c for c in chains if not only or c['id'] in only])


def validate_sources(fixtures, spec, cases, chains):
    manifest = {x['id']: x for x in json.loads((fixtures / 'sources.json').read_text())}
    if spec.get('synthetic'):
        manifest[spec['database']['id']] = spec['database']
    sources = {}
    for database in sorted({c['database'] for c in cases + chains}):
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', database) or database not in manifest:
            raise ValueError('Unknown database in frozen specification')
        # Historical absolute paths in the unmodified frozen JSON are provenance
        # only. Files are always located relative to the selected fixture folder.
        path = fixtures / (database + '.sqlite')
        sha = digest(path)
        if sha != manifest[database]['sha256']:
            raise ValueError('Database SHA-256 mismatch: ' + database)
        sources[database] = {'path': path, 'sha256': sha, 'bytes': path.stat().st_size}
    return sources


def save_report(path, output):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def offline_reassess(spec, input_path, output_path):
    original = json.loads(input_path.read_text())
    if original.get('case_sha256') and original['case_sha256'] != spec['_sha256']:
        raise ValueError('Saved report and frozen specification hashes differ')
    by_id = {case['id']: case for case in spec['cases']}
    for case in spec['cases']:
        if case.get('clarification_resolution'):
            by_id[case['id'] + '_resolution'] = case['clarification_resolution']
    for chain in spec.get('conversation_chains', []):
        by_id.update({f'{chain["id"]}_{i}': case for i, case in enumerate(chain['turns'], 1)})
    entries = []
    for run in original.get('runs', [original]):
        for entry in run.get('cases', []):
            entries.append(entry)
            if entry.get('clarification_resolution'):
                entries.append(entry['clarification_resolution'])
        for chain in run.get('chains', []):
            entries.extend(chain.get('turns', []))
    reassessed = []
    for entry in entries:
        case = by_id.get(entry.get('id'))
        if case is None:
            continue
        passed, reason = assess(case, entry['record']) if entry.get('record') else (False, entry.get('not_run') or entry.get('error') or 'no saved answer')
        reassessed.append({'id': entry['id'], 'previous_passed': entry.get('passed'), 'passed': passed, 'assessment': reason, 'case_sha256': case_digest(case), 'source_sha256': entry.get('source_sha256') or (entry.get('record') or {}).get('source_sha256'), 'model_usage': entry.get('model_usage', (entry.get('record') or {}).get('usage', [])), 'model_attempts': entry.get('model_attempts', (entry.get('record') or {}).get('model_attempts', []))})
    if not reassessed:
        raise ValueError('No matching saved records were found for this specification/selection')
    report = {'format': FORMAT + '-offline-reassessment', 'created_at': now(), 'input_report': str(input_path), 'input_sha256': digest(input_path), 'model_calls': 0, 'results': reassessed, 'sample_notice': spec.get('sample_notice')}
    if output_path.exists():
        raise ValueError('Offline reassessment report already exists; choose a new path')
    save_report(output_path, report)
    print(json.dumps({'offline': True, 'reassessed': len(reassessed), 'passed': sum(r['passed'] for r in reassessed), 'report': str(output_path)}, ensure_ascii=False))
    return 0


def service_failure(record):
    error = str(record.get('error') or '')
    return record.get('status') == 'failed' and any(token in error for token in ('模型响应超时', '无法连接模型', '模型服务', '模型凭据', '模型或接口不可用', 'HTTP '))


def live_run(args, spec, spec_path, fixtures, cases, chains, sources):
    config_root = args.config_runtime.expanduser().resolve()
    config_file = config_root / 'model.json'
    if not config_file.is_file():
        raise ValueError('Model configuration is missing; no request was sent')
    protected = [config_root, ROOT / '.runtime', fixtures]
    report_path = args.report.expanduser().resolve()
    if any(report_path.is_relative_to(p) for p in protected):
        raise ValueError('--report cannot be inside configuration, main runtime or fixtures')
    if report_path.suffix != '.json':
        raise ValueError('--report must be an explicit .json path')
    if args.runtime:
        runtime = args.runtime.expanduser().resolve()
        if runtime.exists() or any(runtime.is_relative_to(p) for p in protected):
            raise ValueError('--runtime must be a new isolated directory outside protected data')
    else:
        runtime = None
    sha = digest(spec_path)
    if report_path.exists():
        output = json.loads(report_path.read_text())
        if output.get('format') != FORMAT or output.get('case_sha256') != sha or not isinstance(output.get('runs'), list):
            raise ValueError('Existing report is a different format/specification; choose a new report path')
    else:
        output = {'format': FORMAT, 'case_sha256': sha, 'sample_notice': spec.get('sample_notice'), 'synthetic': bool(spec.get('synthetic')), 'sources': {k: {a: b for a, b in v.items() if a != 'path'} for k, v in sources.items()}, 'runs': [], 'attempts': [], 'first_failures': {}, 'human_review': 'Numeric/status comparisons do not replace review of explanations, source navigation and clarification wording.'}
    for name, source in sources.items():
        existing = output['sources'].get(name)
        current = {k: v for k, v in source.items() if k != 'path'}
        if existing and existing != current:
            raise ValueError('Existing report has different source hashes')
        output['sources'][name] = current
    # Dependencies and configuration are imported only after explicit live consent.
    from backend.model_gateway import ModelConfig
    config = ModelConfig(config_root).load()
    from fastapi.testclient import TestClient
    from backend.main import create_app
    if runtime is None:
        runtime = Path(tempfile.mkdtemp(prefix='zhiheng-stage3-' + time.strftime('%Y%m%d-%H%M%S-')))
    else:
        runtime.mkdir(parents=True, mode=0o700)
    runtime.chmod(0o700)
    shutil.copyfile(config_file, runtime / 'model.json')
    (runtime / 'model.json').chmod(0o600)
    run = {'id': uuid.uuid4().hex, 'label': args.label, 'started_at': now(), 'runtime': str(runtime), 'models_requested': config['models'], 'selected_cases': [c['id'] for c in cases], 'selected_chains': [c['id'] for c in chains], 'cases': [], 'chains': [], 'circuit_breaker': {'threshold': 3, 'consecutive_external_failures': 0, 'opened': False}}
    output['runs'].append(run)
    save_report(report_path, output)  # Prove writable before any metered request.
    lock = threading.RLock()
    breaker = threading.Event()
    app = create_app(runtime, runtime / 'empty-samples', False)
    with TestClient(app) as client:
        response = client.post('/api/projects', json={'name': '第三阶段隔离验收 ' + args.label})
        response.raise_for_status()
        project = response.json()['id']
        ids = {}
        for name, source in sources.items():
            uploaded = client.post(f'/api/projects/{project}/import', files=[('files', (name + '.sqlite', source['path'].read_bytes()))])
            uploaded.raise_for_status()
            imported = uploaded.json()
            for job in imported['jobs']:
                app.state.service.process_job(job['id'])
            ids[name] = imported['sources'][0]['id']
            stored = app.state.service.store.get('sources', ids[name])
            if stored['sha256'] != source['sha256'] or stored['status'] not in ('ready', 'attention'):
                raise ValueError('Imported source failed hash/status validation: ' + name)

        def execute(case, database, parent=None):
            entry = {'id': case['id'], 'question': case['question'], 'database': database, 'tags': case.get('tags', []), 'case_sha256': case_digest(case), 'source_sha256': sources[database]['sha256'], 'started_at': now(), 'passed': False, 'model_usage': [], 'model_attempts': []}
            started = time.monotonic()
            try:
                with lock:
                    if breaker.is_set():
                        entry['not_run'] = 'Three consecutive external service failures; no further submission'
                        return finish(entry)
                    body = {'question': case['question'], 'source_id': ids[database], 'mode': 'database', 'request_id': str(uuid.uuid4())}
                    if parent:
                        body['parent_answer_id'] = parent
                    submitted = client.post(f'/api/projects/{project}/answers', json=body)
                    submitted.raise_for_status()
                    answer_id = submitted.json()['id']
                while True:
                    fetched = client.get(f'/api/projects/{project}/answers/{answer_id}')
                    fetched.raise_for_status()
                    record = fetched.json()
                    if record['status'] not in ('queued', 'running'):
                        break
                    if time.monotonic() - started > 420:
                        client.post(f'/api/projects/{project}/answers/{answer_id}/cancel')
                        entry['error'] = 'Exceeded 420 seconds; cancellation requested; submission not repeated'
                        return finish(entry)
                    time.sleep(.5)
                entry['record'] = record
                entry['model_usage'] = record.get('usage', [])
                entry['model_attempts'] = record.get('model_attempts', record.get('attempts', []))
                entry['sql_repairs'] = (record.get('result') or {}).get('query', {}).get('repairs', [])
                entry['passed'], entry['assessment'] = assess(case, record)
                if parent:
                    parent_response = client.get(f'/api/projects/{project}/answers/{parent}')
                    parent_response.raise_for_status()
                    if record.get('parent_answer_id') != parent or record.get('conversation_id') != parent_response.json().get('conversation_id'):
                        entry.update(passed=False, assessment='Follow-up did not retain its parent and conversation')
                if record.get('source_sha256') != sources[database]['sha256']:
                    entry.update(passed=False, assessment='Source hash differs from frozen input')
                with lock:
                    circuit = run['circuit_breaker']
                    circuit['consecutive_external_failures'] = circuit['consecutive_external_failures'] + 1 if service_failure(record) else 0
                    if circuit['consecutive_external_failures'] >= 3:
                        circuit.update(opened=True, opened_at=now())
                        breaker.set()
            except Exception as exc:
                # Provider response bodies and config are deliberately not logged.
                entry['error'] = 'Acceptance operation failed: ' + type(exc).__name__
            finally:
                entry['elapsed_ms'] = round((time.monotonic() - started) * 1000)
            return finish(entry)

        def finish(entry):
            with lock:
                attempt_id = uuid.uuid4().hex
                entry['attempt_id'] = attempt_id
                previous = sum(a['case_id'] == entry['id'] and not a.get('not_run') for a in output['attempts'])
                entry['attempt_number'] = previous + (0 if entry.get('not_run') else 1)
                output['attempts'].append({'attempt_id': attempt_id, 'run_id': run['id'], 'case_id': entry['id'], 'attempt_number': entry['attempt_number'], 'passed': entry['passed'], 'not_run': entry.get('not_run'), 'case_sha256': entry['case_sha256'], 'source_sha256': entry['source_sha256'], 'model_usage': entry['model_usage'], 'model_attempts': entry['model_attempts'], 'record': copy.deepcopy(entry.get('record')), 'assessment': entry.get('assessment'), 'error': entry.get('error'), 'elapsed_ms': entry.get('elapsed_ms')})
                if not entry['passed'] and not entry.get('not_run'):
                    output['first_failures'].setdefault(entry['id'], {'attempt_id': attempt_id, 'run_id': run['id'], 'reason': entry.get('assessment') or entry.get('error'), 'status': entry.get('record', {}).get('status')})
                save_report(report_path, output)
            print(json.dumps({'id': entry['id'], 'status': entry.get('record', {}).get('status'), 'passed': entry['passed'], 'assessment': entry.get('assessment'), 'not_run': entry.get('not_run')}, ensure_ascii=False), flush=True)
            return entry

        def single(case):
            entry = execute(case, case['database'])
            resolution = case.get('clarification_resolution')
            if resolution and entry.get('record', {}).get('status') == 'needs_clarification':
                followup = dict(resolution, id=case['id'] + '_resolution')
                resolved = execute(followup, case['database'], entry['record']['id'])
                entry['clarification_resolution'] = resolved
                entry['clarification_flow_passed'] = entry['passed'] and resolved['passed']
            elif resolution:
                entry['clarification_flow_passed'] = False
            return entry

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(single, case): case for case in cases}
            for future in as_completed(futures):
                entry = future.result()
                with lock:
                    run['cases'].append(entry)
                    save_report(report_path, output)

        def chain_run(chain):
            turns, parent = [], None
            for i, turn in enumerate(chain['turns'], 1):
                entry = execute(dict(turn, id=f'{chain["id"]}_{i}'), chain['database'], parent)
                turns.append(entry)
                status = entry.get('record', {}).get('status')
                if status not in ('completed', 'needs_clarification', 'insufficient_evidence'):
                    break
                parent = entry['record']['id']
            return {'id': chain['id'], 'turns': turns, 'passed': len(turns) == len(chain['turns']) and all(t['passed'] for t in turns)}

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(chain_run, chain) for chain in chains]
            for future in as_completed(futures):
                with lock:
                    run['chains'].append(future.result())
                    save_report(report_path, output)
    run['finished_at'] = now()
    save_report(report_path, output)
    print('report:', report_path, flush=True)
    return 1 if any(not c['passed'] or c.get('clarification_flow_passed') is False for c in run['cases']) or any(not c['passed'] for c in run['chains']) else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures', type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument('--config-runtime', type=Path, default=ROOT / '.runtime', help='Read model.json only; never use this directory as evaluation storage')
    parser.add_argument('--runtime', type=Path, help='New isolated directory; omitted means a fresh timestamped temporary directory')
    parser.add_argument('--report', type=Path, help='Explicit JSON output; same frozen live spec can append a new run')
    parser.add_argument('--spec', default='cases.frozen.json', help='Frozen JSON filename within fixtures; stress-cases.frozen.json runs synthetic boundaries')
    parser.add_argument('--only', nargs='+', help='Case or conversation-chain identifiers')
    parser.add_argument('--label', default='first')
    parser.add_argument('--run-live', action='store_true', help='Explicitly permit real configured model requests')
    parser.add_argument('--reassess', type=Path, help='Offline re-evaluation of an existing report; sends no model requests')
    args = parser.parse_args(argv)
    if args.run_live == bool(args.reassess):
        parser.error('Choose --run-live OR --reassess <report>; no model request was sent')
    if args.report is None:
        parser.error('--report is required; no model request was sent')
    try:
        fixtures = args.fixtures.expanduser().resolve()
        report_path = args.report.expanduser().resolve()
        protected = [args.config_runtime.expanduser().resolve(), ROOT / '.runtime', fixtures]
        if report_path.suffix != '.json' or any(report_path.is_relative_to(p) for p in protected):
            raise ValueError('--report must be a JSON path outside configuration, main runtime and fixtures')
        spec, spec_path = load_spec(fixtures, args.spec)
        cases, chains = select_cases(spec, args.only)
        if args.reassess:
            selected_spec = dict(spec, cases=cases, conversation_chains=chains, _sha256=digest(spec_path))
            return offline_reassess(selected_spec, args.reassess.expanduser().resolve(), args.report.expanduser().resolve())
        sources = validate_sources(fixtures, spec, cases, chains)
        return live_run(args, spec, spec_path, fixtures, cases, chains, sources)
    except (ValueError, OSError, KeyError) as exc:
        print('Acceptance not started/completed: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
