"""What-if calculations reuse a published rule; assumptions stay separate."""
from __future__ import annotations
import copy
import hashlib
import json
from decimal import Decimal, localcontext
from .export_service import read_snapshot
from .formula_engine import evaluate_arithmetic
from .store import identity, now


class ScenarioService:
    def __init__(self,store):self.store=store
    def initialize(self):
        with self.store.transaction() as c:
            c.execute('CREATE TABLE IF NOT EXISTS scenarios(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),answer_id TEXT NOT NULL REFERENCES answers(id),created_at TEXT NOT NULL,body TEXT NOT NULL)')

    def create(self,project_id,answer_id,calculation_id,overrides,expected_context_revision):
        if not isinstance(overrides,dict) or not 1<=len(overrides)<=16:raise ValueError('请明确修改至少一个原公式参数')
        with self.store.transaction() as c:
            payload=read_snapshot(self.store.path,project_id,answer_id)
            answer=payload['answers'][0];result=answer.get('result') or {}
            if answer.get('context_revision')!=expected_context_revision:raise ValueError('回答上下文已变化，请重新打开情景计算')
            if answer['status']!='completed' or (result.get('verification') or {}).get('status')!='passed' or payload['record_metadata'][answer_id]['freshness']['scope_stale']:
                raise ValueError('请基于来源版本仍有效的已完成计算创建情景')
            used={i for claim in result.get('claims',[]) for i in claim.get('calculation_ids',[])}
            candidates=[calc for calc in result.get('calculations',[]) if calc.get('id')==calculation_id and calculation_id in used]
            if len(candidates)!=1:raise ValueError('请选择答案实际引用的一项计算')
            original=candidates[0]
            if original.get('rule_origin') not in {'source','user_question'}:raise ValueError('原计算规则出处不明确')
            parameters=original.get('parameters')
            if not isinstance(parameters,list) or not parameters:raise ValueError('原计算缺少可复算参数')
            base={p['name']:{'value':p['value'],'unit':p['unit']} for p in parameters}
            if len(base)!=len(parameters) or set(overrides)-set(base):raise ValueError('只能调整原计算已有的参数')
            assumed=copy.deepcopy(base);changes=[]
            for key,value in overrides.items():
                if not isinstance(value,dict) or set(value)!={'value','unit'}:raise ValueError('每项假设需要明确数值与单位')
                if type(value['value']) not in (str,int,float) or not isinstance(value['unit'],str):raise ValueError('假设参数格式无效')
                # Unit changes are explicit, dimensional conversion is handled
                # by the same Decimal evaluator as evidence-bound computation.
                assumed[key]=copy.deepcopy(value)
                changes.append({'name':key,'before':base[key],'after':copy.deepcopy(value),'origin':'user_assumption','is_observed_fact':False})
            args={'precision':original.get('precision',2),'structure_constants':original.get('visual_rule_constants',[])}
            baseline=evaluate_arithmetic(original['expression'],base,original['unit'],**args)
            if Decimal(baseline['value'])!=Decimal(original['value']):raise ValueError('原公式复算与保存结果不一致，暂不创建情景')
            computed=evaluate_arithmetic(original['expression'],assumed,original['unit'],**args)
            with localcontext() as context:
                context.prec=40
                delta=Decimal(computed['value'])-Decimal(baseline['value'])
                relative=None if Decimal(baseline['value'])==0 else str(delta/abs(Decimal(baseline['value']))*100)
                display_delta=format(delta.quantize(Decimal(1).scaleb(-args['precision'])),f".{args['precision']}f")
                display_relative=None if relative is None else format(Decimal(relative).quantize(Decimal('.01')),'.2f')
            item={'schema_version':'zhiheng.scenario.v1','id':identity(),'project_id':project_id,'answer_id':answer_id,
                  'context_revision':expected_context_revision,'calculation_id':calculation_id,'created_at':now(),
                  'kind':'hypothetical_scenario','title':'假设情景对照','expression':original['expression'],
                  'baseline':baseline,'scenario':computed,'changes':changes,'delta':{'value':str(delta),'display_value':display_delta,'unit':original['unit'],'relative_percent':relative,'relative_display_percent':display_relative},
                  'source_rule_evidence_ids':original.get('rule_evidence_ids',[]),
                  'baseline_evidence_ids':original.get('evidence_ids',[]),'source_revisions':answer.get('sources',[]),
                  'baseline_record_sha256':payload['record_metadata'][answer_id]['stored_body_sha256'],
                  'boundary':'修改项为用户假设；结果是原公式下的条件计算，不是预测或已发生事实。原回答和来源不变。'}
            c.execute('INSERT INTO scenarios VALUES(?,?,?,?,?)',(item['id'],project_id,answer_id,item['created_at'],json.dumps(item,ensure_ascii=False,allow_nan=False)))
            return item
    def listing(self,project_id,answer_id=None):
        with self.store.transaction() as c:
            query='SELECT body FROM scenarios WHERE project_id=?';params=[project_id]
            if answer_id:query+=' AND answer_id=?';params.append(answer_id)
            return [json.loads(r[0]) for r in c.execute(query+' ORDER BY created_at DESC',params)]
