"""Typed visual tool results. Model transcriptions never masquerade as OCR text."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass

from .chart_reading import read_chart_page, PROVENANCE
from .formula_vision import read_formula_page

TOOL_INSTRUCTIONS='''
新增统一原页工具（与search/sql/calculate使用同一真实依赖图）：
chart_read:{id,kind:"chart_read",source_id,page,query:"只问该原页需读取的指标/图/时期",depends_on:[]}；
formula_read:{id,kind:"formula_read",source_id,page,query,depends_on:[]}。
多页资料必须先search取得真实页依据，并将该search步骤写进depends_on；单页图片可直接读取。
视觉工具outputs提供真实已核验项的columns/rows：chart_read为evidence_id/item_id/type/category/series/period/value/unit/text；formula_read为evidence_id/formula_id/parameter_id/symbol/value/unit/text。可用{from_step,row,column}将真实对象传给后续search或参数化SQL；这些行是工具转录结果，不是数据库源行。关系项的value为null，不可取参考阈值代替。
chart_read执行独立视觉转录与原页复核，只返回受支持的chart_observation。它不是OCR原文，也不是完整答案。
引用图表转录事实时给visual_bindings:[{evidence_id:"E3",item_id:"V1",statement:"本条text中逐字一致的事实子句"}]。
每个statement只引用对应item的实际事实与数字。需要多个转录项时拆成多条claim；不得互换对象/时期/轴或把参考线当实测点值。
只有printed_value可作为精确计算输入，绑定{evidence_id,visual_item_id:"V1",unit:"原项单位"}，绝不填写value。
visual_relation只支持定性关系，missing_precision只支持已核验的精度限制；不能根据二者生成精确数字。索要明确精确值时，拒绝说明不算回答完整；询问能否读取则可如实解释限制。
formula_read返回已核验formula_structure及formula_parameter，结构本身不可直接执行。公式参数绑定{evidence_id,formula_id:"F1",parameter_id:"P1",unit:"原单位",symbol?:"原公式符号"}。
计算仍用calculate，rule_evidence_ids引用formula_structure；表达式须忠实原结构，参数来自实际原件或已执行SQL，缺参数/单位/符号映射则不能算。不要将公式编号当参数。
标准单位换算由calculate的有限单位白名单自动执行：保持原业务表达式与参数原单位，仅把output_unit设为用户所需单位；例如kWh/h转W时仍用(a+b)/c，output_unit="W"，绝不再乘1000或3600。计算器已输出的值可以直接引用；仅有kW结果不能自行心算成W。视觉reader的limitations是未核验的读取建议，原页未印单位换算系数不表示白名单标准换算不可执行。换算与来源公式适用范围是不同检查。
kind为chart_observation/formula_structure/formula_parameter的quote是明确标注的独立复核视觉转录，不能声称逐字OCR或已验证精准框。正式定位仅原页。
未产生可引用项时仅能说明本次核验未确认，不能推定源图不存在答案。不要对相同页和问题重复调用工具投票；只有新图、页、时期、明确缺项或更清晰原件才构成有意义后续读取。
最终仍须逐claim独立复核，图表相关（含计算输入）必须在chart_relation_claim_ids中返回chart_relations逐项true；不以工具已核验代替整答口径与完整性核验。
业务事实必须保留其适用限定：初步核算/修订数、现价/不变价格、名义/实际增速、同比/环比、截至时点、仅本部/不含某范围、草案/正式版本等。原文对该指标明确给出的必要限定不得在最终句中省略；复核时缺失影响含义的限定须false并说明缺项。不能把附在另一个指标上的限定移用本指标，也不能自行补来源未写的口径。
本轮真实工具预算以payload.tool_limits为准；达到上限是处理未完成，不等于没有答案。
'''


@dataclass(frozen=True)
class ReasoningLimits:
    max_actions: int = 10
    max_no_progress_rounds: int = 3
    max_image_pages: int = 3
    max_visual_reads: int = 4

    def __post_init__(self):
        for value,maximum in ((self.max_actions,64),(self.max_no_progress_rounds,6),
                              (self.max_image_pages,8),(self.max_visual_reads,12)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('跨源工具资源配置无效')


def _base(source,page):
    return {'source_id':source['id'],'source_name':source['name'],'sha256':source['sha256'],
            'parser_version':source['parser_version'],'page':page,'page_label':f'第 {page} 页',
            'bbox':[0,0,1,1],'localization_scope':'original_page','stale':False}


def read_visual_tool(kind,source,page,query,gateway,cancelled,progress):
    """Only independently supported typed items can enter the evidence registry."""
    if kind not in ('chart_read','formula_read'):
        raise ValueError('视觉工具类型无效')
    if source.get('format') not in ('pdf','png','jpg','jpeg'):
        raise ValueError('该资料没有可用原页视觉通路')
    if type(page) is not int or page < 1 or not any(p.get('number')==page for p in source.get('pages',[])):
        raise ValueError('原页不在实际解析资料中')
    reader=read_chart_page if kind=='chart_read' else read_formula_page
    reading=reader(source,page,query,gateway,cancelled,progress)
    records=[];base=_base(source,page)
    if reading.get('status')=='reviewed' and reading.get('review',{}).get('valid') is True:
        if kind=='chart_read':
            for item in reading.get('items',[]):
                if (item.get('review',{}).get('supported') is not True or item.get('provenance')!=PROVENANCE
                    or any(item.get(key)!=base[key] for key in ('source_id','sha256','parser_version','page'))):
                    raise ValueError('图表工具的支持状态或来源版本不一致')
                records.append({**base,'kind':'chart_observation','quote':item['text'],
                    'quote_origin':'independently_reviewed_visual_transcription',
                    'provenance':PROVENANCE,'visual_item':copy.deepcopy(item),'visual_checked':True})
        else:
            for formula in reading.get('formulas',[]):
                if formula.get('verified') is not True or formula.get('review',{}).get('supported') is not True:
                    raise ValueError('公式工具包含未经支持的结构')
                if any((formula.get('source_position') or {}).get(key)!=base[key] for key in ('source_id','sha256','parser_version','page')):
                    raise ValueError('公式结构来源版本不一致')
                structure={**base,'kind':'formula_structure','formula_id':formula['id'],
                    'quote':('公式 '+formula['label'].strip()+'：' if formula.get('label','').strip() else '')+formula['latex'],'quote_origin':'independently_reviewed_formula_transcription',
                    'formula':copy.deepcopy(formula),'provenance':reading['provenance'],'visual_checked':True}
                records.append(structure)
                for parameter in formula.get('parameter_candidates',[]):
                    if parameter.get('verified') is not True:
                        continue
                    records.append({**base,'kind':'formula_parameter','formula_id':formula['id'],
                        'parameter_id':parameter['id'],'quote':parameter['quote'],
                        'quote_origin':'independently_reviewed_formula_parameter','parameter':copy.deepcopy(parameter),
                        'formula_verified':True,'formula_parameter_conflicts':formula.get('parameter_conflicts',[]),
                        'provenance':reading['provenance'],'visual_checked':True})
    return {'tool_type':kind,'status':reading.get('status'),'records':records,'reading':reading,
            'coverage':{'source_ids':[source['id']],'pages':[page],'localization_scope':'original_page'},
            'unresolved':reading.get('limitations',[]) + ([] if records else ['本次原页读取没有形成可引用的已核验项，不能据此断言原件没有答案。'])}


def visual_output(kind, records, status):
    """Referencable scalar cells copied only from accepted tool records."""
    if kind == 'chart_read':
        columns=['evidence_id','item_id','type','category','series','period','value','unit','text']
        rows=[]
        for record in records:
            item=record['visual_item']
            rows.append([record['id'],item['id'],item['type'],item.get('category'),item.get('series'),
                         item.get('period'),item.get('value') if item['type']=='printed_value' else None,
                         item.get('unit'),item['text']])
    else:
        columns=['evidence_id','formula_id','parameter_id','symbol','value','unit','text']
        rows=[[r['id'],r['formula_id'],r['parameter_id'],r['parameter'].get('symbol'),
               r['parameter'].get('value'),r['parameter'].get('unit'),r['quote']]
              for r in records if r.get('kind')=='formula_parameter']
    return {'evidence_ids':[r['id'] for r in records],'result_type':kind,'status':status,
            'columns':columns,'rows':rows,'origin':'independently_reviewed_visual_transcription'}


def validate_visual_bindings(text,bindings,records,sources,*,calculation_evidence_ids=()):
    """Bind each directly asserted visual clause to reviewed item IDs and source.

    Original-page semantic review is still mandatory. This only checks typed
    provenance, exact clause/ID and source-bound numeric support.
    """
    from .chart_answer_engine import _claims
    visual={r['id']:r for r in records if r.get('kind')=='chart_observation'}
    if not visual and not bindings:return []
    if not isinstance(bindings,list) or len(bindings)>16:
        if not bindings and set(visual).issubset(calculation_evidence_ids):return []
        raise ValueError('直接图表事实须给visual_bindings，绑定已独立核验的具体item和精确statement')
    normalized=[];used=set()
    for binding in bindings:
        if not isinstance(binding,dict):raise ValueError('图表读取绑定须为对象')
        record=visual.get(binding.get('evidence_id'));statement=binding.get('statement')
        if record is None or binding.get('item_id')!=record['visual_item']['id']:
            raise ValueError('图表读取绑定不属于本条实际引用的已核验item')
        if not isinstance(statement,str) or not statement or statement not in text:
            raise ValueError('图表读取statement必须逐字出现在本条结论中')
        item=record['visual_item']
        if any(item.get(key)!=record.get(key) for key in ('source_id','sha256','parser_version','page')):
            raise ValueError('图表读取项与证据的来源位置不一致')
        if item.get('review',{}).get('supported') is not True or record.get('provenance')!=PROVENANCE:
            raise ValueError('未经独立原页复核的图表项不能绑定')
        source=sources.get(record['source_id'])
        if not source or any(record.get(key)!=source.get(key) for key in ('sha256','parser_version')):
            raise ValueError('图表读取证据版本已失效')
        checked,errors=_claims({'claims':[{'id':'C1','text':statement,'item_ids':[item['id']]}]},
                               {item['id']:item},source)
        if errors or not checked:raise ValueError('；'.join(errors) or '图表读取绑定无效')
        normalized.append({**binding,'provenance':PROVENANCE,'source_id':record['source_id'],
                           'page':record['page'],'bbox':[0,0,1,1],'localization_scope':'original_page',
                           'observation_type':item['type']})
        used.add(record['id'])
    if set(visual)-used-set(calculation_evidence_ids):
        raise ValueError('所引图表读取项缺少逐项绑定；不得附无关已核验图表来授权数字')
    return normalized


def precision_coverage(coverage,claims,evidence):
    from .chart_answer_engine import _precision_coverage
    items={eid:r['visual_item'] for eid,r in evidence.items() if r.get('kind')=='chart_observation'}
    adapted=[{**c,'item_ids':[eid for eid in c.get('evidence_ids',[]) if eid in items]} for c in claims]
    return _precision_coverage(coverage,adapted,items)
