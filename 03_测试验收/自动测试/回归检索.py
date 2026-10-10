"""Run 160 frozen questions against real local retrieval, without a paid text model."""
import argparse,hashlib,importlib.util,json,os,shutil,sys,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    args=parser.parse_args();out=Path(args.output).resolve()
    if out.exists():raise ValueError('报告已存在，请使用新文件名')
    spec=importlib.util.spec_from_file_location('regression_manifest',Path(__file__).with_name('评估RAG.py'))
    manifest=importlib.util.module_from_spec(spec);spec.loader.exec_module(manifest)
    data,texts,_=manifest.validate()
    work=Path(tempfile.mkdtemp(prefix='evidenceflow-retrieval-regression-'))
    os.environ['EVIDENCEFLOW_DATA_DIR']=str(work)
    os.environ['EVIDENCEFLOW_RAG_MODEL_DIR']=str(ROOT/'02_数据资料/运行数据/models')
    sys.path.insert(0,str(ROOT/'01_程序代码'))
    from backend import main,store as s,rag_index,rag_retrieval,rag_providers,documents
    main.init_app();selected={}
    for source in data['documents']:
        path=s.UPLOADS/source['file'];path.write_text(texts[source['file']])
        selected[source['file']]=s.index_document(path)['id']
    job=rag_index.submit_index(list(selected.values()),synchronous=True)
    if job['status']!='completed':raise ValueError('本机回归向量索引未完成')
    scope=rag_index.freeze_scope(requested_scope={'scope_type':'corpus','document_ids':list(selected.values())})
    report={'dataset_id':data['dataset_id'],'dataset_sha256':manifest.digest(manifest.DEFAULT_DATASET),
        'isolated_public_data':True,'paid_text_model_calls':0,'providers':rag_providers.capabilities(),'index_job':job,
        'source_sha256':{name:manifest.digest(ROOT/name) for name in ['01_程序代码/backend/rag_index.py','01_程序代码/backend/rag_retrieval.py']},'results':[],
        'boundary':'本机真实检索和固定原文定位回归；参考片段召回不是最终答案、引用语义或业务准确率。'}
    def write():out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    for case in data['cases']:
        started=time.perf_counter();r=rag_retrieval.search(case['question'],scope)
        checks=[]
        for requirement in case['requirements']:
            for reference in requirement['evidence']:
                candidates=[e for e in r['evidence'] if e['source']==reference['source']]
                hit=any(manifest.normalized(reference['quote']) in manifest.normalized(e['snippet']) for e in candidates)
                checks.append({'source':reference['source'],'reference_quote':reference['quote'],'retrieved':hit})
        locations=[documents.resolve(e['locator'],e['snippet'])['status']=='located' for e in r['evidence']]
        report['results'].append({'case_id':case['id'],'category':case['category'],'duration_ms':round((time.perf_counter()-started)*1000,2),
            'reference_checks':checks,'located':sum(locations),'returned_evidence':len(locations),'retrieval':r['retrieval']})
        if len(report['results'])%10==0:write();print('已执行 '+str(len(report['results']))+'/160',flush=True)
    checks=[c for r in report['results'] for c in r['reference_checks']]
    report['summary']={'questions':len(report['results']),'reference_quote_hits':sum(c['retrieved'] for c in checks),'reference_quote_denominator':len(checks),
        'located':sum(r['located'] for r in report['results']),'returned_evidence':sum(r['returned_evidence'] for r in report['results']),
        'retrieval_errors':sum(bool(r['retrieval']['errors']) for r in report['results'])}
    write();print(json.dumps({'output':str(out),'summary':report['summary']},ensure_ascii=False),flush=True)
if __name__=='__main__':main()
