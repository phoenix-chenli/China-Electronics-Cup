"""Build 100 executable SQL inputs and private, source-executed reference keys.

This performs only local read-only SQL and fixture creation. It never imports a
model gateway, reads saved model answers, or starts an API evaluation run.
"""
from __future__ import annotations
import argparse,json,shutil,sqlite3
import sqlglot
from collections import Counter
from pathlib import Path

from .core import digest,load_suite
from .sql_cases import sql_cases
from .synthetic_sources import build_boundary_database


def build(output,fixtures):
    output=Path(output);fixtures=Path(fixtures)
    if output.exists():raise ValueError('评测资产输出已存在，拒绝覆盖；请使用新版本目录')
    inputs=output/'inputs';inputs.mkdir(parents=True)
    originals={name:fixtures/(name+'.sqlite') for name in ('chinook','northwind')}
    original_hashes={name:digest(path) for name,path in originals.items()}
    provenance={s['id']:s for s in json.loads((fixtures/'sources.json').read_text())}
    sources=[]
    for name,original in originals.items():
        path=inputs/(name+'.sqlite');shutil.copyfile(original,path)
        info=provenance[name]
        if digest(path)!=info['sha256']:raise ValueError('公开原件与来源清单不一致')
        shutil.copyfile(fixtures/info['license_file'],output/info['license_file'])
        sources.append(dict(id=name,file=path.name,sha256=digest(path),origin=info['source_url'],
                            license=info['license'],cluster=name,kind='public_teaching'))
    boundary=build_boundary_database(inputs/'boundary.sqlite')
    sources.append(dict(id='boundary',file=boundary.name,sha256=digest(boundary),origin='evaluation/synthetic_sources.py deterministic builder',
                        license='Project-authored synthetic evaluation fixture',cluster='controlled-boundary-v1',kind='synthetic_controlled'))
    suite={'schema_version':'zhiheng.eval-input.v1','name':'SQL100开发回归与合成边界集 v1','seed':0,
           'description':'40基础+60复杂，3个资料簇。公开教学库已参与开发，全部split=development；合成样本单列，不宣称保留集或SOTA。参考SQL/答案与输入物理隔离。',
           'sources':sources,'cases':[]}
    keys={};audit=[]
    for case in sql_cases(fixtures):
        tags=['sql_'+case['level'],*case['tags'],'synthetic_controlled' if case['source']=='boundary' else 'public_teaching']
        suite['cases'].append(dict(id=case['id'],question=case['question'],source_ids=[case['source']],mode='database',split='development',tags=tags))
        path=inputs/(case['source']+'.sqlite')
        with sqlite3.connect('file:'+str(path.resolve())+'?mode=ro&immutable=1',uri=True) as c:
            c.execute('PRAGMA query_only=ON')
            cursor=c.execute(case['reference_sql']);rows=[list(row) for row in cursor.fetchall()]
            columns=[v[0] for v in cursor.description]
        if len(rows)>200:raise ValueError(f"{case['id']} reference output exceeds the stated product row cap")
        ordered=bool(sqlglot.parse_one(case['reference_sql'],read='sqlite').args.get('order'))
        keys[case['id']]={'status':'completed','rows':rows,'ordered':ordered,'tolerance':0.005,
                         'reference_sql':case['reference_sql'],'reference_parameters':[],
                         'reference_columns':columns,'require_evidence':True,
                         'require_text_review':True,
                         'grading_scope':'exact result rows with duplicate/null/order preservation; professional text/semantic description scored separately',
                         'source_sha256':digest(path),'tags':tags,
                         'review_requirements':{'sql_scope':'Read the actual question and reference SQL; aliases may differ but columns/values may not be omitted.',
                                                'provenance':'Selected database and source revision must match; no invented units or business definitions.'}}
        if case.get('legacy_reference_id'):keys[case['id']]['legacy_reference_id']=case['legacy_reference_id']
        audit.append({'id':case['id'],'source':case['source'],'row_count':len(rows),'columns':columns,'local_reference_executed':True})
    path=inputs/'suite.json';path.write_text(json.dumps(suite,ensure_ascii=False,indent=2)+'\n')
    path.with_suffix('.json.sha256').write_text(digest(path)+'  suite.json\n')
    key={'suite_sha256':digest(path),'version':1,'reference_key_created_without_model_outputs':True,
         'real_model_calls':0,'sources':sources,'cases':keys}
    (output/'private-key.json').write_text(json.dumps(key,ensure_ascii=False,indent=2)+'\n')
    manifest={'status':'inputs_and_reference_queries_validated_not_model_evaluated','suite_sha256':digest(path),
              'private_key_sha256':digest(output/'private-key.json'),'counts':dict(Counter(c['level'] for c in sql_cases(fixtures))),
              'sources':sources,'source_originals_unchanged':all(digest(originals[n])==h for n,h in original_hashes.items()),
              'model_calls':0,'network_calls':0,'reference_queries':audit,
              'limits':['These databases have been used during development; no heldout generalization or SOTA claim.','100 unique case IDs do not mean 100 independent sources.','Execution accuracy is separate from natural-language correctness and evidence-review quality.']}
    (output/'reference-audit.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    load_suite(path)
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--fixtures',type=Path,default=Path(__file__).resolve().parents[1]/'tests/fixtures/complex_queries')
    args=parser.parse_args();report=build(args.output,args.fixtures)
    print(json.dumps({k:report[k] for k in ('status','counts','source_originals_unchanged','model_calls')},ensure_ascii=False))


if __name__=='__main__':main()
