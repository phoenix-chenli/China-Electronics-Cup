"""Versioned local scenarios; immutable baselines and server-calculated snapshots.

The four-parameter form expands the existing evidenced bonus rule with an
explicit growth assumption. It does not claim to infer arbitrary formulae.
"""
from __future__ import annotations
import copy
import json
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Literal
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, ConfigDict
from . import store as s, engine

router = APIRouter(prefix="/api/scenarios")
EXPRESSION = "max(0, sales * (1 + growth) - threshold) * rate"
DISPLAY = "奖金 = max(0, B × (1 + g) − T) × r"
ORDER = ["sales", "growth", "threshold", "rate"]
LABELS = {"sales": "基础净销售额", "growth": "增长率", "threshold": "起奖门槛", "rate": "奖金比例"}
UNITS = {"sales": "CNY", "growth": "ratio", "threshold": "CNY", "rate": "ratio"}
LIMITS = {"sales": ("0", "1000000000000"), "growth": ("-1", "2"), "threshold": ("0", "1000000000000"), "rate": ("0", "1")}
SYMBOLS = dict(zip(ORDER, ["B", "g", "T", "r"]))
D = Decimal
def uid(): return str(uuid.uuid4())
def encode(v): return json.dumps(v, ensure_ascii=False)
def rounded(v): return v.quantize(D('.01'), rounding=ROUND_HALF_UP)


def initialize():
    with s.conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS ef_scenario_baselines(id TEXT PRIMARY KEY,body TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ef_scenario_calculations(id TEXT PRIMARY KEY,baseline_id TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ef_scenario_plans(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,name TEXT NOT NULL,
          calculation_id TEXT NOT NULL,revision INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,deleted_at TEXT);
        CREATE TABLE IF NOT EXISTS ef_scenario_revisions(plan_id TEXT NOT NULL,revision INTEGER NOT NULL,name TEXT NOT NULL,
          calculation_id TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(plan_id,revision));
        CREATE INDEX IF NOT EXISTS ef_scenario_project ON ef_scenario_plans(project_id,updated_at);
        """)


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')
class Prepare(Strict):
    run_id: str
    project_id: str = 'default'
    session_id: str | None = None
    refresh: bool = False
class Parameter(Strict):
    value: str = Field(max_length=40)
    unit: str
class Compute(Strict):
    baseline_id: str
    rule_mode: Literal['historical', 'current'] = 'historical'
    parameters: dict[str, Parameter]
class Save(Strict):
    calculation_id: str
    name: str = Field(min_length=1, max_length=120)
    expected_revision: int | None = None
class Name(Strict):
    name: str = Field(min_length=1, max_length=120)
    expected_revision: int


def body(table, identifier):
    with s.conn() as c:
        row = c.execute(f'SELECT body FROM {table} WHERE id=?', (identifier,)).fetchone()
    if not row: raise HTTPException(404, '情景记录不存在')
    return json.loads(row['body'])


def validate_parameters(params):
    if set(params) != set(ORDER): raise ValueError('需要完整的四项参数')
    values = {}
    for key in ORDER:
        p = params[key]
        if p['unit'] != UNITS[key]: raise ValueError(LABELS[key] + '单位不一致')
        try: value = D(p['value'])
        except (InvalidOperation, ValueError): raise ValueError(LABELS[key] + '必须是有效数字') from None
        if not value.is_finite(): raise ValueError(LABELS[key] + '必须是有限数字')
        lo, hi = map(D, LIMITS[key])
        if not lo <= value <= hi: raise ValueError(LABELS[key] + '超出允许范围')
        quantum = D('.01') if UNITS[key] == 'CNY' else D('.000001')
        if value != value.quantize(quantum): raise ValueError(LABELS[key] + ('最多保留两位小数' if quantum == D('.01') else '百分比最多保留四位小数'))
        values[key] = value
    return values


def template(result):
    if not result.get('formulas'): return None
    f = result['formulas'][0]
    if f['expression'] != 'max(0, sales - threshold) * rate' or f['unit'] != 'CNY':
        raise ValueError('当前仅支持有来源的净销售额奖金公式')
    engine.check_parameters(f['parameters'])
    ps = {p['name']: copy.deepcopy(p) for p in f['parameters']}
    ids = {e['id'] for e in result['evidence']}
    for key in ['sales', 'threshold', 'rate']:
        if not ps.get(key) or not ps[key].get('evidence_ids') or any(e not in ids for e in ps[key]['evidence_ids']):
            raise ValueError('基准参数缺少可定位的来源')
    ps['sales']['value'] = str(f['actual_sales'])
    ps['growth'] = {'value': '0', 'unit': 'ratio', 'evidence_ids': [], 'definition': '基准不作增长调整'}
    for key in ORDER:
        ps[key].update(name=key, label=LABELS[key], symbol=SYMBOLS[key], unit=UNITS[key], assumption=False,
                       minimum=LIMITS[key][0], maximum=LIMITS[key][1])
    validate_parameters(ps)
    return {'parameters': ps, 'rule_name': f['rule_name'], 'effective_from': f['effective_from'],
            'effective_to': f['effective_to'], 'evidence': result['evidence']}


@router.post('/baselines')
def prepare(req: Prepare):
    with s.conn() as c:
        if not c.execute('SELECT 1 FROM ef_projects WHERE id=?', (req.project_id,)).fetchone():
            raise HTTPException(404, '项目不存在')
        if req.session_id:
            session = c.execute('SELECT * FROM ef_sessions WHERE id=? AND project_id=?', (req.session_id, req.project_id)).fetchone()
            messages = c.execute('SELECT result_json FROM ef_messages WHERE session_id=? AND result_json IS NOT NULL', (req.session_id,)).fetchall()
            if not session or req.run_id not in {json.loads(m[0]).get('run_id') for m in messages}:
                raise ValueError('基准结果不属于当前项目与会话')
    origin = s.get_run(req.run_id)
    if not origin.get('formulas'): raise ValueError('这条分析没有可用的奖金公式，请先完成奖金查询')
    spec = origin['query_spec']
    overrides = {k: v for k, v in spec.items() if k in {'year', 'region', 'product', 'metric', 'asks_top', 'needs_formula', 'rule_id'}}
    overrides.update(needs_formula=True, needs_experience=False, scenario_pct=0, rule_mode='historical')
    base_result = origin
    if req.refresh or origin['formulas'][0].get('simulation'):
        base_result = engine.execute(origin['question'], overrides=overrides)
    historic = template(base_result)
    if not historic: raise ValueError('当时规则缺失或冲突，无法建立基准')
    current_spec = {**overrides, 'rule_mode': 'current'}
    current_spec.pop('rule_id', None)
    current_result = engine.execute(origin['question'], overrides=current_spec, persist=False)
    current = template(current_result)
    if current:
        # A new rule must never silently change the frozen base sales or its SQL source.
        current['parameters']['sales'] = copy.deepcopy(historic['parameters']['sales'])
        current['evidence'] = list({e['id']: e for e in current['evidence'] + historic['evidence']}.values())
    baseline = {'id': uid(), 'origin_run_id': req.run_id, 'run_id': base_result['run_id'],
                'project_id': req.project_id, 'session_id': req.session_id, 'created_at': s.now(),
                'data_version': base_result['data_version'], 'rule_snapshot_version': s.data_version(),
                'query_spec': base_result['query_spec'], 'question': origin['question'],
                'expression': EXPRESSION, 'display': DISPLAY, 'unit': 'CNY', 'order': ORDER,
                'rules': {'historical': historic, 'current': current},
                'current_rule_gap': '' if current else next((c['text'] for c in current_result.get('claims', []) if c.get('kind') == 'gap'), '当前规则无法核验'),
                'result': base_result}
    with s.conn() as c: c.execute('INSERT INTO ef_scenario_baselines VALUES (?,?,?)', (baseline['id'], encode(baseline), baseline['created_at']))
    return baseline_view(baseline)


def baseline_view(b):
    current = s.data_version()
    return {**b, 'stale': b['data_version'] != current or b['rule_snapshot_version'] != current}


@router.get('/baselines/{bid}')
def get_baseline(bid: str): return baseline_view(body('ef_scenario_baselines', bid))


@router.post('/calculate')
def compute(req: Compute):
    b = body('ef_scenario_baselines', req.baseline_id)
    rule = b['rules'][req.rule_mode]
    if not rule: raise ValueError(b['current_rule_gap'] or '当前规则不可用')
    values = validate_parameters({k: p.model_dump() for k, p in req.parameters.items()})
    bp = b['rules']['historical']['parameters']
    before = {k: D(bp[k]['value']) for k in ORDER}
    value = lambda p: rounded(engine.calculate(EXPRESSION, p))
    initial, final = value(before), value(values)
    working = before.copy(); previous = initial; contributions = []
    params = []
    stamp = s.now()
    for key in ORDER:
        working[key] = values[key]; after = value(working)
        contributions.append({'name': key, 'label': LABELS[key], 'delta': str(after - previous),
                              'before_result': str(previous), 'after_result': str(after)})
        previous = after
        rp = rule['parameters'][key]
        assumption = values[key] != D(rp['value'])
        params.append({**rp, 'value': str(values[key]), 'baseline_value': bp[key]['value'],
                       'reference_value': rp['value'], 'assumption': assumption,
                       'changed': values[key] != before[key], 'modified_by': '本地用户' if assumption else None,
                       'modified_at': stamp if assumption else None,
                       'change_percentage_points': str((values[key] - before[key]) * 100) if UNITS[key] == 'ratio' else None})
    delta = final - initial
    result = {'id': uid(), 'baseline_id': b['id'], 'created_at': stamp, 'expression': EXPRESSION, 'display': DISPLAY,
              'unit': 'CNY', 'rule_mode': req.rule_mode, 'rule_name': rule['rule_name'],
              'effective_from': rule['effective_from'], 'effective_to': rule['effective_to'],
              'parameters': params, 'baseline_value': str(initial), 'value': str(final), 'delta': str(delta),
              'relative_percent': str(rounded(delta / initial * 100)) if initial else None,
              'adjusted_sales': str(values['sales'] * (1 + values['growth'])),
              'contributions': contributions, 'contribution_order': ORDER,
              'contribution_note': '依参数顺序逐项替换；交互影响计入后替换项。顺序改变时分项会改变，总差额不变。',
              'rounding': '中间乘法保留精度，奖金按人民币分四舍五入；分项为相邻舍入结果之差。',
              'evidence': rule['evidence'], 'data_version': b['data_version'], 'kind': 'scenario',
              'checks': {'units': True, 'finite': True, 'contribution_sum': sum(D(x['delta']) for x in contributions) == delta}}
    with s.conn() as c: c.execute('INSERT INTO ef_scenario_calculations VALUES (?,?,?,?)', (result['id'], b['id'], encode(result), stamp))
    return {**result, 'stale': baseline_view(b)['stale']}


def plan_row(c, pid):
    r = c.execute('SELECT * FROM ef_scenario_plans WHERE id=?', (pid,)).fetchone()
    if not r: raise HTTPException(404, '方案不存在')
    return dict(r)
def plan_view(p, detail=False):
    calc = body('ef_scenario_calculations', p['calculation_id'])
    base = body('ef_scenario_baselines', calc['baseline_id'])
    view = {**p, 'baseline_id': base['id'], 'session_id': base['session_id'], 'baseline_value': calc['baseline_value'],
            'value': calc['value'], 'delta': calc['delta'], 'rule_mode': calc['rule_mode'], 'stale': baseline_view(base)['stale']}
    if detail: view.update(calculation=calc, baseline=baseline_view(base))
    return view


@router.get('/plans')
def plans(project_id: str = 'default', deleted: bool = False):
    with s.conn() as c:
        rows = c.execute('SELECT * FROM ef_scenario_plans WHERE project_id=? AND deleted_at IS ' + ('NOT NULL' if deleted else 'NULL') + ' ORDER BY updated_at DESC', (project_id,)).fetchall()
    return [plan_view(dict(r)) for r in rows]


@router.get('/plans/{pid}')
def get_plan(pid: str):
    with s.conn() as c: p = plan_row(c, pid)
    return plan_view(p, True)


def save_plan(req, pid=None):
    name = req.name.strip()
    if not name: raise ValueError('方案名称不能为空')
    calc = body('ef_scenario_calculations', req.calculation_id)
    base = body('ef_scenario_baselines', calc['baseline_id'])
    stamp = s.now()
    with s.conn() as c:
        c.execute('BEGIN IMMEDIATE')
        if pid:
            p = plan_row(c, pid)
            if p['deleted_at']: raise HTTPException(409, '请先恢复已删除的方案')
            if req.expected_revision != p['revision']: raise HTTPException(409, '方案已有新版本，请打开最新版本或另存副本')
            if p['project_id'] != base['project_id']: raise ValueError('不能覆盖其他项目的方案')
            rev = p['revision'] + 1
            c.execute('UPDATE ef_scenario_plans SET name=?,calculation_id=?,revision=?,updated_at=? WHERE id=?', (name, calc['id'], rev, stamp, pid))
        else:
            pid, rev = uid(), 1
            c.execute('INSERT INTO ef_scenario_plans VALUES (?,?,?,?,?,?,?,NULL)', (pid, base['project_id'], name, calc['id'], rev, stamp, stamp))
        c.execute('INSERT INTO ef_scenario_revisions VALUES (?,?,?,?,?)', (pid, rev, name, calc['id'], stamp))
    return get_plan(pid)


@router.post('/plans')
def create_plan(req: Save): return save_plan(req)
@router.put('/plans/{pid}')
def update_plan(pid: str, req: Save): return save_plan(req, pid)
@router.patch('/plans/{pid}')
def rename(pid: str, req: Name):
    with s.conn() as c: p = plan_row(c, pid)
    return save_plan(Save(calculation_id=p['calculation_id'], name=req.name, expected_revision=req.expected_revision), pid)
@router.delete('/plans/{pid}')
def trash(pid: str):
    with s.conn() as c:
        plan_row(c, pid)
        c.execute('UPDATE ef_scenario_plans SET deleted_at=? WHERE id=?', (s.now(), pid))
    return {'ok': True, 'recoverable': True}
@router.post('/plans/{pid}/restore')
def restore(pid: str):
    with s.conn() as c:
        plan_row(c, pid)
        c.execute('UPDATE ef_scenario_plans SET deleted_at=NULL WHERE id=?', (pid,))
    return get_plan(pid)
@router.get('/plans/{pid}/versions')
def versions(pid: str):
    with s.conn() as c:
        plan_row(c, pid)
        return [dict(r) for r in c.execute('SELECT * FROM ef_scenario_revisions WHERE plan_id=? ORDER BY revision DESC', (pid,))]
@router.get('/plans/{pid}/versions/{revision}')
def version(pid: str, revision: int):
    with s.conn() as c:
        p = plan_row(c, pid)
        r = c.execute('SELECT * FROM ef_scenario_revisions WHERE plan_id=? AND revision=?', (pid, revision)).fetchone()
        if not r: raise HTTPException(404, '版本不存在')
    return plan_view({**p, **dict(r), 'revision': revision}, True)
@router.get('/calculations/{cid}/export')
def export(cid: str):
    calc = body('ef_scenario_calculations', cid)
    base = body('ef_scenario_baselines', calc['baseline_id'])
    payload = {'format': 'evidenceflow-scenario-v1', 'note': '情景假设计算，不是已实现业绩。',
               'baseline': base, 'calculation': calc, 'stale': baseline_view(base)['stale']}
    return Response(json.dumps(payload, ensure_ascii=False, indent=2), media_type='application/json',
                    headers={'Content-Disposition': 'attachment; filename="EvidenceFlow-scenario.json"'})
