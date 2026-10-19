"""Read-only aggregation of recorded acceptance attempts; no model calls."""
from __future__ import annotations
from collections import Counter, defaultdict
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
from statistics import median
from .acceptance_budget import AcceptanceBudget
from .core import digest


def summarize(output):
    events = [json.loads(s) for s in (output/'调用与轮次账本.jsonl').read_text().splitlines() if s.strip()]
    attempts = AcceptanceBudget._state(events)
    finished = [e for e in events if e['type'] == 'request_finished']
    groups = defaultdict(list)
    for e in finished:
        groups[(e['request'].get('purpose'), e['request'].get('task_stage', 'unknown'))].append(e['request'])
    def usage(requests):
        result = {}
        for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            values = [r.get('usage', {}).get(key) for r in requests]
            result[key] = {'sum_reported': sum(v for v in values if type(v) is int),
                           'requests_with_value': sum(type(v) is int for v in values)}
        for key, field, child in [('cached_prompt_tokens','prompt_tokens_details','cached_tokens'),
                                  ('reasoning_tokens','completion_tokens_details','reasoning_tokens')]:
            values = []
            for request in requests:
                measured = request.get('usage', {})
                value = (measured.get(field) or {}).get(child)
                if type(value) is not int:
                    value = measured.get('prompt_cache_hit_tokens' if key == 'cached_prompt_tokens' else 'reasoning_tokens')
                values.append(value)
            result[key] = {'sum_reported': sum(v for v in values if type(v) is int),
                           'requests_with_value': sum(type(v) is int for v in values)}
        return result
    records=[]
    for aid, a in attempts.items():
        file = output/'原始证据/真实问答'/f'{aid}.json'
        if not file.exists():
            records.append({'attempt_id':aid,'case_id':a['case_id'],'record_missing':True}); continue
        entry=json.loads(file.read_text()); record=entry.get('record') or {}
        records.append({'attempt_id':aid,'case_id':a['case_id'],'file':str(file.relative_to(output)),
                        'sha256':digest(file),'status':record.get('status'), 'paid':a['started'],
                        'closed':a['closed'], 'elapsed_ms':entry.get('elapsed_ms'),
                        'code_sha256':entry.get('code_sha256'), 'config_sha256':entry.get('config_sha256'),
                        'retry_reason':entry.get('retry_reason'), 'code_changed_during_attempt':entry.get('code_changed_during_attempt')})
    stages=[]
    for (role,stage),reqs in groups.items():
        times=[r['request_elapsed_ms'] for r in reqs if type(r.get('request_elapsed_ms')) is int]
        stages.append({'role':role,'stage':stage,'requests':len(reqs),'usage':usage(reqs),
                       'request_elapsed_ms_sum':sum(times),'request_elapsed_ms_median':median(times) if times else None})
    reqs=[e['request'] for e in finished]
    catalog=json.loads((output/'原始证据/模型选择依据.json').read_text())
    estimate=0.; unsupported=0
    for r in reqs:
        u=r.get('usage') or {}; rate=catalog['models'].get(r.get('requested_model'))
        if not rate or type(u.get('prompt_tokens')) is not int or type(u.get('completion_tokens')) is not int:
            unsupported+=1;continue
        estimate+=(u['prompt_tokens']*rate['prompt']+u['completion_tokens']*rate['completion'])/1_000_000
    data={'schema_version':'acceptance-usage-summary.v1','created_at':datetime.now(timezone.utc).isoformat(),
          'rounds':{'submitted_attempts':len(attempts),
                    'paid_attempts':sum(a['started'] for a in attempts.values()),
                    'closed_without_model_request':sum(not a['started'] and a['closed'] for a in attempts.values()),
                    'unique_paid_case_ids':len({a['case_id'] for a in attempts.values() if a['started']}),
                    'open_reservations':sum(not a['closed'] for a in attempts.values()),
                    'effective_limit':AcceptanceBudget._effective_limit(events)},
          'requests':{'post_started':sum(a['requests'] for a in attempts.values()),'finished':len(finished),
                      'http_statuses':dict(Counter(str(r.get('http_status','unknown')) for r in reqs)),
                      'requested_models':dict(Counter(r.get('requested_model','unknown') for r in reqs)),
                      'reported_models':dict(Counter(r.get('reported_model','unknown') for r in reqs)),
                      'thinking_modes':dict(Counter(
                          (r.get('request_settings',{}).get('thinking') or {}).get('type','not_reported')
                          for r in reqs)),
                      'reasoning_efforts':dict(Counter(
                          r.get('request_settings',{}).get('reasoning_effort','not_set') for r in reqs))},
          'usage':usage(reqs),'stage_breakdown':stages,'attempts':records,
          'cost':{'gateway_catalog_uncached_estimate_cny':round(estimate,6),'unpriced_requests':unsupported,
                  'actual_invoice_cny':None,'boundary':'基于已核实网关目录价和已报告用量、未扣缓存折扣的估算；不是实扣账单，未知用量未冒充零。'},
          'timing_boundary':'每轮elapsed为本地API提交至完整记录导出；请求时间为各实际模型请求，不是首字时间。失败与复测均计入。'}
    target=output/'原始证据/实际调用与耗时汇总.json'
    target.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    return data


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parents[2]/'03_内部资料/提交前功能验收')
    args=parser.parse_args();value=summarize(args.output)
    print(json.dumps({k:value[k] for k in ('rounds','requests','usage','cost')},ensure_ascii=False))
