"""Create executable 8x8 development inputs, with physically separate keys.

No model outputs are read, no services are started, and every SQL reference is
executed locally in immutable read-only mode before the key is written.
"""
from __future__ import annotations
import argparse,copy,json,shutil,sqlite3
from collections import Counter
from pathlib import Path
import sqlglot

from .context_cases import context_cases
from .core import digest,load_suite
from .synthetic_sources import build_boundary_database


DEFINITIONS='''合成业务口径说明 V1
本文件与boundary.sqlite是项目作者编写的受控评测资料，不是真实企业记录。本文规则是资料中的规则，不代表使用者在系统内确认过私人语义定义，也不是数据库DDL自动表达的含义。
登记金额：orders.total_cents。到账金额：payments.paid_cents，通过payments.order_id关联orders.id；每笔支付只计一次，不与lines直接联表放大支付或订单头金额。
上述两种指标均只计算orders.status为“已完成”的订单；日期范围按orders.day，ISO日期文本，年份包含1月1日至下一年1月1日之前。
金额原始单位为分。100分=1元。NULL代表未记录或未知，不能替换成零；显式零保留，负值表示该原始记录的负金额，不无据改符号。求和按已记录数值统计，报告缺失边界。
本文未定义利润、成本或收益，不得将登记金额、到账金额、员工工时互相替代。
'''


def build(output,fixtures,multimodal):
    output,fixtures,multimodal=Path(output),Path(fixtures),Path(multimodal)
    if output.exists():raise ValueError('评测资产已存在；请使用新的目录保留全部版本')
    inputs=output/'inputs';inputs.mkdir(parents=True)
    sources=[];originals={}
    def copied(source,path,ident=None):
        path=Path(path);source=copy.deepcopy(source)
        if digest(path)!=source['sha256']:raise ValueError('资料摘要与已保存来源清单不一致')
        destination=inputs/path.name;shutil.copyfile(path,destination)
        originals[str(path)]=source['sha256']
        source['file']=path.name
        if ident:source['id']=ident
        sources.append(source)
    public=json.loads((fixtures/'complex_queries/sources.json').read_text())
    chinook=next(s for s in public if s['id']=='chinook')
    copied(dict(id='chinook',file='chinook.sqlite',sha256=chinook['sha256'],origin=chinook['source_url'],license=chinook['license'],cluster='chinook',kind='public_teaching'),fixtures/'complex_queries/chinook.sqlite')
    shutil.copyfile(fixtures/'complex_queries'/chinook['license_file'],output/chinook['license_file'])
    boundary=build_boundary_database(inputs/'boundary.sqlite')
    sources.append(dict(id='boundary',file=boundary.name,sha256=digest(boundary),origin='evaluation/synthetic_sources.py deterministic builder',license='Project-authored synthetic evaluation fixture',cluster='controlled-boundary-v1',kind='synthetic_controlled'))
    path=inputs/'口径说明V1_合成.txt';path.write_text(DEFINITIONS)
    sources.append(dict(id='definitions',file=path.name,sha256=digest(path),origin='evaluation/build_context_suite.py DEFINITIONS; author-created, not user confirmation',license='Project-authored synthetic evaluation fixture',cluster='controlled-boundary-v1',kind='synthetic_controlled'))
    manifests=json.loads((fixtures/'multimodal/public-sources.json').read_text())
    name='石景山区2024年统计公报.pdf';original=next(x for x in manifests if x['file']==name)
    metadata=original.get('provenance',original.get('metadata',{}))
    copied(dict(id='shijingshan',file=name,sha256=original['sha256'],origin=metadata.get('origin') or metadata.get('source_url') or original.get('url') or 'Official public statistical bulletin; original provenance retained in public-source-manifest.json',license='Public official statistical document; no open license asserted',cluster='shijingshan_stats_2024',kind='official_public_original'),fixtures/'multimodal'/name)
    (output/'public-source-manifest.json').write_text(json.dumps(original,ensure_ascii=False,indent=2)+'\n')
    multimodal_suite=load_suite(multimodal/'inputs/suite.json')
    for ident in ('formula_native','formula_constraints'):
        source=next(s for s in multimodal_suite['sources'] if s['id']==ident)
        copied(source,multimodal/'inputs'/source['file'])
    support=json.loads((multimodal/'context-support/source-manifest.json').read_text())
    for old,new in [('context_formula_register','context_parameters'),('context_formula_chart','context_energy_chart')]:
        source=next(s for s in support['sources'] if s['id']==old)
        copied(source,multimodal/'context-support'/source['file'],new)
    (output/'formula-support-provenance.json').write_text(json.dumps(support,ensure_ascii=False,indent=2)+'\n')
    suite=dict(schema_version='zhiheng.eval-input.v1',name='上下文8×8开发验收 v1',seed=0,
               description='8条独立根链、每链8轮；公开教学/公开公报均已用于开发，所有split=development。合成边界、公式参数及其图表同簇，不宣称独立来源佐证或保留集。侧分支、版本变更和旧revision API探针另计。',sources=sources,cases=[])
    keys={};audit=[]
    for raw in context_cases():
        entry={k:v for k,v in raw.items() if k!='private_key'};key=copy.deepcopy(raw['private_key'])
        if 'reference_sql' in key:
            if len(entry['source_ids'])!=1:raise ValueError('SQL参考只作用于明确的单库')
            source=next(s for s in sources if s['id']==entry['source_ids'][0]);path=inputs/source['file']
            with sqlite3.connect('file:'+str(path.resolve())+'?mode=ro&immutable=1',uri=True) as connection:
                connection.execute('PRAGMA query_only=ON')
                cursor=connection.execute(key['reference_sql']);rows=[list(row) for row in cursor.fetchall()]
                columns=[v[0] for v in cursor.description]
            if len(rows)>200:raise ValueError('参考结果超过产品已声明行数边界')
            key.update(rows=rows,reference_columns=columns,ordered=bool(sqlglot.parse_one(key['reference_sql'],read='sqlite').args.get('order')),tolerance=0.005)
            audit.append(dict(id=entry['id'],row_count=len(rows),local_readonly_reference_executed=True))
        key['source_sha256']={s['id']:s['sha256'] for s in sources if s['id'] in entry['source_ids']}
        suite['cases'].append(entry);keys[entry['id']]=key
    path=inputs/'suite.json';path.write_text(json.dumps(suite,ensure_ascii=False,indent=2)+'\n')
    path.with_suffix('.json.sha256').write_text(digest(path)+'  suite.json\n')
    (output/'private-key.json').write_text(json.dumps(dict(suite_sha256=digest(path),version=1,reference_key_created_without_model_outputs=True,real_model_calls=0,cases=keys),ensure_ascii=False,indent=2)+'\n')
    long_rows=[keys[f'ctx08-t{i}']['rows'] for i in (1,2,3)]
    long_size=len(json.dumps(long_rows,ensure_ascii=False))
    if long_size<=60000:raise ValueError('长上下文原始结果未达到设计的60k阈值，不能宣称触发压缩场景')
    report=dict(status='inputs_and_keys_prepared_not_model_evaluated',case_count=len(keys),chain_counts=dict(Counter(c['chain_id'] for c in suite['cases'])),
                source_counts=dict(Counter(s['kind'] for s in sources)),source_clusters=sorted({s['cluster'] for s in sources}),
                suite_sha256=digest(path),private_key_sha256=digest(output/'private-key.json'),
                source_originals_unchanged=all(digest(p)==sha for p,sha in originals.items()),
                model_calls=0,network_calls=0,reference_queries=audit,first_three_long_chain_reference_rows_json_chars=long_size,
                source_representations_are_not_independent=True,
                limits=['No real model run has occurred for this suite.','Known public and synthetic development sources; no heldout or SOTA claim.','64 turns share sources and ancestors, not 64 independent samples.','Business-negative status requires record-bound factual review.','Explicit patch is executed by the runner; branch/revision probes are separate and are not counted in 64.','Long raw result size exceeds 60k; actual runtime compression/recovery still requires evidence.'])
    (output/'reference-audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    load_suite(path)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--fixtures',type=Path,default=Path(__file__).resolve().parents[1]/'tests/fixtures')
    parser.add_argument('--multimodal',type=Path,required=True)
    args=parser.parse_args();report=build(args.output,args.fixtures,args.multimodal)
    print(json.dumps({k:report[k] for k in ('status','case_count','chain_counts','model_calls','first_three_long_chain_reference_rows_json_chars')},ensure_ascii=False))


if __name__=='__main__':main()
