"""Freeze the final 33-turn development acceptance plan without model calls.

Original files are referenced by absolute path and hash, never re-imported or
copied here. Answers remain in a separate local-only key. The controlled
boundary fixture is reproduced with the existing builder, not claimed as an
additional real business document.
"""
from __future__ import annotations

import argparse
import ast
import copy
import json
import sqlite3
from pathlib import Path

from .build_context_suite import DEFINITIONS
from .context_cases import context_cases
from .core import CASE_KEYS, digest, record_digest
from .synthetic_sources import build_boundary_database


IMMUTABLE_FIELDS = (
    'schema_version', 'name', 'sources', 'cases', 'scoring_sets',
    'dynamic_suggestion_rules', 'selection_reasons', 'round_budget', 'baseline_artifacts',
)


def _load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')


def _reference(path, sql):
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro&immutable=1', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        cur = db.execute(sql)
        return {'reference_sql': sql, 'reference_columns': [x[0] for x in cur.description],
                'rows': [list(x) for x in cur.fetchall()], 'ordered': False, 'tolerance': 0.005}


def _sql_basics(path):
    """Read only the existing literal basic cases; no missing legacy fixtures."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name=='sql_cases')
    calls = [n.value for n in fn.body if isinstance(n, ast.Expr)
             and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name)
             and n.value.func.id=='add']
    return [[ast.literal_eval(a) for a in call.args] for call in calls[:3]]


def _source(raw, base):
    path = (base/raw['file']).resolve()
    if digest(path)!=raw['sha256']:
        raise ValueError('Source hash changed: '+raw['id'])
    return {'id': raw['id'], 'path': str(path), 'sha256': raw['sha256'],
            **{k: raw[k] for k in ('origin','kind','cluster')}}


def validate(plan, key):
    if plan['schema_version']!='acceptance-plan.v1' or len(plan['cases'])!=33:
        raise ValueError('Expected the frozen 33-turn acceptance plan')
    canonical={k:plan[k] for k in IMMUTABLE_FIELDS}
    if plan['plan_sha256']!=record_digest(canonical) or key['plan_sha256']!=plan['plan_sha256']:
        raise ValueError('Frozen plan identity mismatch')
    sources={s['id']:s for s in plan['sources']}
    if len(sources)!=len(plan['sources']):raise ValueError('Duplicate source')
    for source in sources.values():
        if not Path(source['path']).is_absolute() or digest(source['path'])!=source['sha256']:
            raise ValueError('Source path or hash mismatch')
    seen={}
    for case in plan['cases']:
        if set(case)-(CASE_KEYS|{'dynamic_suggestion_from'}) or case['id'] in seen:
            raise ValueError('Case key leak or duplicate ID')
        if not case.get('question') or any(s not in sources for s in case['source_ids']):
            raise ValueError('Invalid question or source')
        parent=seen.get(case.get('parent_case_id'))
        if case.get('parent_case_id') and not parent:raise ValueError('Unknown parent')
        if parent and (set(parent['source_ids'])!=set(case['source_ids'])
                       or parent['mode']!=case['mode']):raise ValueError('Changed chain source scope')
        if 'chain_id' in case:
            if case['turn_index']==1 and parent:raise ValueError('Unexpected root parent')
            if case['turn_index']>1 and (not parent or parent.get('chain_id')!=case['chain_id']
                                        or parent.get('turn_index')!=case['turn_index']-1):
                raise ValueError('Broken sequential chain')
        if case.get('dynamic_suggestion_from') and case['dynamic_suggestion_from']!=case.get('parent_case_id'):
            raise ValueError('Dynamic suggestion must come from real parent')
        if case['id'] not in key['cases']:raise ValueError('Missing independent key')
        seen[case['id']]=case
    if len(plan['scoring_sets']['single_table_sql'])!=10 or len(plan['scoring_sets']['single_document_rag'])!=10:
        raise ValueError('Basic scoring sets must each have 10 cases')
    if set(key['cases'])!=set(seen):raise ValueError('Key/input set mismatch')


def build(root, output):
    root,output=Path(root).resolve(),Path(output).resolve()
    target=output/'用例与结果.json'; key_path=output/'原始证据/冻结答案键.json'
    if target.exists() or key_path.exists():
        raise ValueError('Frozen acceptance assets already exist; refusing to overwrite results or keys')
    original=root/'评测材料/初赛20题/输入/suite.json'
    original_key=root/'评测材料/初赛20题/离线评分/参考答案.json'
    complex_input=root/'知识库样本/真实业务文档/专项输入.json'
    complex_key=root/'评测材料/复杂文档专项/离线评分/参考答案.json'
    suite,gold=_load(original),_load(original_key)
    complex_suite,complex_gold=_load(complex_input),_load(complex_key)
    if gold['suite_sha256']!=digest(original) or complex_gold['suite_sha256']!=digest(complex_input):
        raise ValueError('Baseline key does not match its frozen input')
    sources=[_source(s, original.parent) for s in suite['sources']]
    cases=copy.deepcopy(suite['cases']); keys=copy.deepcopy(gold['cases'])
    # The original twenty question records remain exactly unchanged.
    original_ids=[c['id'] for c in cases]
    for ident in original_ids:
        keys[ident]['key_provenance']={'path':str(original_key),'sha256':digest(original_key),'case_id':ident}
    chinook=next(s for s in sources if s['id']=='chinook')
    definitions_path=root/'evaluation/sql_cases.py'
    basics=_sql_basics(definitions_path)
    for number,entry in ((2,basics[1]),(3,basics[2])):
        sid,question,sql,tags=entry; ident=f'sql-{number:03d}'
        cases.append({'id':ident,'question':question,'source_ids':[sid],'mode':'database',
                      'split':'development','tags':['sql_basic',*tags.split(),'public_teaching']})
        keys[ident]={'status':'completed','require_evidence':True,'require_text_review':True,
                     **_reference(chinook['path'],sql),'source_sha256':chinook['sha256'],
                     'review_requirements':{'facts':['实际SQL结果与参考结果一致，最终发布答案不丢字段、单位或条件。'],
                                            'forbidden':['将客户数当国家数','凭空推断日期或币种']},
                     'key_provenance':{'path':str(definitions_path),'sha256':digest(definitions_path),
                                       'literal_basic_case_index':number,'method':'AST literal extraction + readonly SQL'}}
    chosen=['RD-B08-01','RD-B04-01','工程A01-Q1']
    for ident in chosen:
        case=next(c for c in complex_suite['cases'] if c['id']==ident)
        cases.append(copy.deepcopy(case));keys[ident]=copy.deepcopy(complex_gold['cases'][ident])
        keys[ident]['key_provenance']={'path':str(complex_key),'sha256':digest(complex_key),'case_id':ident}
        for sid in case['source_ids']:
            if sid not in {s['id'] for s in sources}:
                sources.append(_source(next(s for s in complex_suite['sources'] if s['id']==sid),complex_input.parent))
    controlled=output/'受控输入';controlled.mkdir(parents=True,exist_ok=True)
    boundary=controlled/'boundary.sqlite';definition=controlled/'口径说明V1_合成.txt'
    if not boundary.exists():build_boundary_database(boundary)
    if definition.exists() and definition.read_text(encoding='utf-8')!=DEFINITIONS:
        raise ValueError('Existing controlled definitions do not match builder')
    definition.write_text(DEFINITIONS,encoding='utf-8')
    for ident,path,builder in [('boundary',boundary,root/'evaluation/synthetic_sources.py'),
                               ('definitions',definition,root/'evaluation/build_context_suite.py')]:
        sources.append({'id':ident,'path':str(path),'sha256':digest(path),
                        'origin':str(builder)+'; existing deterministic controlled fixture',
                        'kind':'synthetic_controlled','cluster':'controlled-boundary-v1'})
    amount_sql="SELECT SUM(total_cents) FROM orders WHERE status='已完成' AND day>=? AND day<?"
    amounts={}
    with sqlite3.connect(boundary.as_uri()+'?mode=ro&immutable=1',uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        for year in (2024,2025):
            amounts[year]=db.execute(amount_sql,(f'{year}-01-01',f'{year+1}-01-01')).fetchone()[0]
    if amounts!={2024:10000,2025:56000}:raise ValueError('Controlled independent reference changed')
    cross_questions=[
        '按口径说明V1统计2025年登记金额，先给出原始单位下的合计，再依照文档换算为元；说明来源字段、订单状态、日期范围和NULL处理边界。',
        '把年份改成2024年，其余口径不变，仍分别给出原始金额和换算为元的金额。',
        '仍针对2024年，仅假设刚才的登记金额减少10%，按原金额乘以(1-10%)计算假设金额，分别用分和元表示。明确这不是实际登记结果，不修改原始数据。',
        '将当前假设金额与第1轮2025年的实际登记金额比较，给出两者金额及相差多少元，明确各自年份、实际或假设性质；不要把它说成实际同比变化。',
        '撤销刚才减少10%的假设，恢复2024年的实际登记金额，分别用分和元表示，说明已经撤销什么、其他哪些口径仍然保留。',
        '【动态建议占位，禁止直接发给模型】从上一轮系统真实产生的合格下一问中，按冻结选择规则选取一条继续分析。',
    ]
    cross_facts=[
        ['2025年已完成订单登记金额56000分，即560元','登记金额=orders.total_cents，日期按orders.day','100分=1元；NULL未知不补零'],
        ['2024年已完成订单登记金额10000分，即100元','继承V1登记金额定义、已完成、原始分及换算元'],
        ['仅在用户假设下：10000×0.9=9000分，即90元','2024年实际仍10000分；没有修改原始数据'],
        ['首轮2025实际560元；当前2024假设90元；相差470元','不同年份且性质不同，不能声称实际同比下降'],
        ['撤销减少10%的假设，2024实际恢复10000分，即100元','保留2024、V1登记金额、已完成、NULL未知和元换算口径'],
        ['依据真实选中建议逐项独立核验；不能预设模型一定生成某个问题','当前为2024实际登记口径，已撤销10%假设'],
    ]
    cross_cases=[]
    for number,(question,facts) in enumerate(zip(cross_questions,cross_facts),1):
        ident=f'acceptance-cross-t{number}'
        case={'id':ident,'question':question,'source_ids':['boundary','definitions'],'mode':'multi_source',
              'split':'development','tags':['context','six_turn_chain','cross_source','synthetic_controlled'],
              'chain_id':'acceptance-cross','turn_index':number}
        if number>1:case['parent_case_id']=f'acceptance-cross-t{number-1}'
        if number==6:case['dynamic_suggestion_from']=case['parent_case_id']
        cross_cases.append(case)
        keys[ident]={'status':'completed','require_evidence':True,'require_text_review':True,
                     'require_context_review':True,'review_requirements':{'facts':facts,
                     'context_conditions':['同一实际父会话、资料版本和来源范围','保留时期、口径及假设性质'],
                     'forbidden':['把推测或模型结论称为用户确认','把合成数据说成真实企业记录','原始数据被修改']},
                     'independent_reference':{'amount_cents_by_year':amounts,
                         'reference_sql':amount_sql,'parameters_by_year':{str(y):[f'{y}-01-01',f'{y+1}-01-01'] for y in amounts},
                         'conversion':'yuan = cents / 100','hypothesis':'2024_cents * (1 - 0.10)',
                         'difference':'2025_actual_yuan - 2024_hypothetical_yuan',
                         'source_sha256':{s['id']:s['sha256'] for s in sources if s['id'] in case['source_ids']}}}
    cases.append(cross_cases[0])
    for raw in [c for c in context_cases() if c['chain_id']=='ctx03'][:2]:
        case={k:copy.deepcopy(v) for k,v in raw.items() if k in CASE_KEYS}
        case['tags']=['context','clarification','synthetic_controlled','two_turn_clarification']
        cases.append(case); key=copy.deepcopy(raw['private_key'])
        if 'reference_sql' in key:key.update(_reference(boundary,key['reference_sql']))
        if case['turn_index']==1:
            key['status']='needs_clarification'
            key['review_requirements']['facts']=['原件有东店员工1与西店员工2两位张伟，必须提出可回答的对象澄清，不随机选取或合并']
        key['key_provenance']={'path':str(root/'evaluation/context_cases.py'),'sha256':digest(root/'evaluation/context_cases.py'),'case_id':case['id']}
        key['source_sha256']={'boundary':digest(boundary)};keys[case['id']]=key
    cases.extend(cross_cases[1:])
    # Recompute all original SQL keys independently, without changing gold.
    for case in suite['cases']:
        k=keys[case['id']]
        if 'reference_sql' in k:
            recomputed=_reference(chinook['path'],k['reference_sql'])
            if recomputed['rows']!=k['rows']:raise ValueError('Original SQL gold changed: '+case['id'])
    sql_ids=[c['id'] for c in suite['cases'] if c['id'].startswith('sql-') and c['id'] not in {'sql-051','sql-074'}]+['sql-002','sql-003']
    rag_ids=[c['id'] for c in suite['cases'] if c['id'].startswith('rag-')]
    dynamic_rule={
        'case_id':'acceptance-cross-t6','source_case_id':'acceptance-cross-t5',
        'eligible_action':'ask','require_live_saved_parent_suggestion':True,
        'preferred_kinds':['explain_calculation','explain_units','explain_result_scope','inspect_metric_scope','inspect_fact_limits'],
        'tie_break':'上述kind顺序；同kind按系统返回顺序，取首条合格候选，不以生成答案是否成功选择。',
        'eligibility':'资料范围不变；只询问当前已核验金额、计算、单位或适用边界；不要求新增资料或未定义成本/利润。',
        'binding':'保存建议key、generation、selected_id、原始text、action、payload、expected_scope和真实父answer_id；输入question替换为原始text，冻结占位仍保留。',
        'no_candidate':'标为未满足建议能力，不生成理想建议、不偷偷换题。',
        'independent_grading':'从V1原文和只读SQL自行核对真实建议索取的事实；数值按实际参数Decimal计算，继承实际父轮有效状态；计算/单位/范围建议逐项核验所需事实与来源。预期数值仅见独立答案键。',
        'input_is_not_gold':'上述选取与证据规则供调度/核验，不发送答案键、预期数值或评判信息给模型。',
    }
    artifacts=[original,original_key,complex_input,complex_key,definitions_path,
               root/'evaluation/context_cases.py',root/'evaluation/synthetic_sources.py',root/'evaluation/build_context_suite.py']
    plan={'schema_version':'acceptance-plan.v1','name':'知衡提交前33轮代表性开发验收',
          'sources':sources,'cases':cases,
          'scoring_sets':{'original20':original_ids,'single_table_sql':sql_ids,'single_document_rag':rag_ids,
                          'multi_table_sql':['sql-051','sql-074'],'complex_specialty':chosen,
                          'clarification':['ctx03-t1','ctx03-t2'],
                          'six_turn_context':[c['id'] for c in cross_cases]},
          'dynamic_suggestion_rules':dynamic_rule,
          'selection_reasons':{'RD-B08-01':'已有失败风险；200页后复杂表格、设计与实测、跨页关系、研究期',
                               'RD-B04-01':'两页不同图；曲线排序、两种测量原理，曾缺证据页',
                               '工程A01-Q1':'200页后公式、用户假设计算、经验关系适用边界',
                               'controlled_sources':'沿用已有builder的受控边界样本，不增加12份真实资料计数，不宣称独立真实业务泛化。'},
          'round_budget':{'planned':33,'maximum_new_attempts':45,'reserve':12,
                          'includes_probe_retry_model_switch':True,'model_requests_are_not_turns':True},
          'baseline_artifacts':[{'path':str(p),'sha256':digest(p)} for p in artifacts]}
    plan['plan_sha256']=record_digest({k:plan[k] for k in IMMUTABLE_FIELDS})
    # Gold is never embedded in the executable input plan.
    key={'schema_version':'acceptance-gold.v1','plan_sha256':plan['plan_sha256'],
         'visibility':'local_independent_grading_only_never_send_to_model',
         'reference_created_without_model_outputs':True,'cases':keys}
    plan.update(status='frozen_pending_real_execution',
                results={c['id']:{'status':'pending','attempts':[],'assessment':None} for c in cases},
                provenance={'split':'development','real_model_calls':0,
                            'plan_sha256_method':'SHA256 of canonical JSON of IMMUTABLE_FIELDS; excludes mutable results/status and the hash field itself.',
                            'answers_file':'原始证据/冻结答案键.json'})
    validate(plan,key)
    if cases[:20]!=suite['cases']:raise ValueError('Original twenty cases changed')
    frozen={k:copy.deepcopy(plan[k]) for k in IMMUTABLE_FIELDS}
    frozen['plan_sha256']=plan['plan_sha256']
    frozen_path=output/'原始证据/冻结计划.json'
    _write(frozen_path,frozen)
    frozen_path.with_suffix('.json.sha256').write_text(digest(frozen_path)+'  '+frozen_path.name+'\n',encoding='utf-8')
    _write(key_path,key);_write(target,plan)
    (target.with_suffix('.json.sha256')).write_text(digest(target)+'  '+target.name+'\n',encoding='utf-8')
    (key_path.with_suffix('.json.sha256')).write_text(digest(key_path)+'  '+key_path.name+'\n',encoding='utf-8')
    return {'cases':len(cases),'sources':len(sources),'plan_sha256':plan['plan_sha256'],
            'input_path':str(target),'gold_path':str(key_path),'real_model_calls':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    output=args.output or args.root.parent/'03_内部资料/提交前功能验收'
    print(json.dumps(build(args.root,output),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
