"""Offline authored scale diagnostics, not real-model answer quality.

--output must be outside the project. Optional --baseline-code points to an
external pre-change backend folder containing documents.py, rag_index.py and
rag_retrieval.py. Without it, use the same-version full-materialization path
as a conservative reference. Each engine runs in a separate fresh process.
"""
from pathlib import Path
import argparse
import ctypes
import hashlib
import importlib.util
import json
import math
import os
import statistics
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types

ROOT=Path(__file__).resolve().parents[2]
DIMENSION=512
_MEMORY_TYPE=None


def memory():
    if sys.platform=='win32':
        global _MEMORY_TYPE
        size=ctypes.c_size_t;dword=ctypes.c_ulong
        if _MEMORY_TYPE is None:
            class Counters(ctypes.Structure):
                _fields_=[('cb',dword),('faults',dword),('peak',size),('working',size),
                    ('pool_peak',size),('pool',size),('nonpool_peak',size),('nonpool',size),('page',size),('page_peak',size)]
            _MEMORY_TYPE=Counters
        body=_MEMORY_TYPE();body.cb=ctypes.sizeof(body)
        current=ctypes.windll.kernel32.GetCurrentProcess;current.restype=ctypes.c_void_p
        read=ctypes.windll.psapi.GetProcessMemoryInfo;read.argtypes=[ctypes.c_void_p,ctypes.c_void_p,dword]
        if not read(current(),ctypes.byref(body),body.cb):raise OSError('无法读取实际进程内存')
        return {'peak_working_set_bytes':body.peak,'working_set_bytes':body.working}
    import resource
    peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {'peak_working_set_bytes':peak if sys.platform=='darwin' else peak*1024,'working_set_bytes':None}


def prepare(output):
    output.mkdir(parents=True,exist_ok=True);cache=output/'cache';cache.mkdir(exist_ok=True)
    os.environ['TMP']=os.environ['TEMP']=str(cache);tempfile.tempdir=str(cache)
    os.environ['EVIDENCEFLOW_DATA_DIR']=str(output/'data')
    os.environ['EVIDENCEFLOW_SKIP_PUBLIC_DEMO']='1'
    sys.path[:0]=[str(ROOT/'01_程序代码/启动与打包'),str(ROOT/'01_程序代码')]
    from runtime_bootstrap import activate
    activate()
    import numpy as np
    class Provider(types.ModuleType):
        def __init__(self):super().__init__('backend.rag_providers');self.targets={};self.calls=0
        def describe(self,role):
            return {'ready':role=='embedding','fingerprint':'scale-geometry-fixture-v1','dimension':DIMENSION,
                'revision':1,'local':True,'model':'seeded-numeric-fixture','max_tokens':512,'reason':'重排在本诊断中停用'}
        def token_count(self,text,role='embedding',strict=False):return len(text)
        def embed(self,texts,**kwargs):
            assert kwargs.get('purpose')=='query','本诊断不调用模型建立向量'
            self.calls+=1
            return {'vectors':[self.targets[text] for text in texts],'provider':self.describe('embedding')}
    provider=Provider();sys.modules['backend.rag_providers']=provider
    import backend
    backend.rag_providers=provider
    return provider,np


def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def build(args):
    output=args.output/str(args.size);provider,np=prepare(output)
    from backend import store as s,rag_index as index,rag_retrieval as retrieval
    s.init();index.initialize();started=time.monotonic();docs=[]
    targets={args.size//5,args.size//3,args.size-1,5};chunks_by_record={}
    for start in range(0,args.size,250):
        records=[]
        for number in range(start,min(start+250,args.size)):
            phrase='DailyDialog retrievel memory score 26.01 ' if number==args.size//3 else 'software revenue region north expense policy '
            text=f'record{number:08d} '+phrase
            records.append((text+'background '*35)[:299]+'.')
        path=s.UPLOADS/f'scale-records-{start:08d}.md';path.write_text('\n\n'.join(records),encoding='utf-8')
        docs.append(s.index_document(path))
    scope=index.freeze_scope(requested_scope={'scope_type':'corpus','document_ids':[d['id'] for d in docs]})
    rng=np.random.default_rng(3172026);count=0
    with s.conn() as db:
        for doc in docs:
            generation=next(g for g in scope['documents'] if g['document_id']==doc['id'])
            cursor=db.execute('SELECT id,text,title_path,body FROM ef_rag_chunks WHERE generation_id=? ORDER BY page,start',(generation['generation_id'],))
            while True:
                rows=cursor.fetchmany(512)
                if not rows:break
                vectors=rng.normal(size=(len(rows),DIMENSION)).astype(np.float32)
                vectors/=np.linalg.norm(vectors.astype(np.float64),axis=1)[:,None]
                entries=[]
                for row,vector in zip(rows,vectors):
                    chunk=json.loads(row['body']);record=int(chunk['text'].split()[0][6:])
                    if record in targets:chunks_by_record[record]=chunk
                    entries.append((row['id'],provider.describe('embedding')['fingerprint'],DIMENSION,
                        hashlib.sha256(retrieval.embedding_input(chunk).encode()).hexdigest(),vector.astype('<f4').tobytes(),s.now()))
                db.executemany('INSERT OR REPLACE INTO ef_rag_embeddings VALUES (?,?,?,?,?,?)',entries);count+=len(rows)
            body=json.loads(db.execute('SELECT body FROM ef_rag_generations WHERE id=?',(generation['generation_id'],)).fetchone()[0])
            body.update(status='ready',vector_status='ready',embedding=provider.describe('embedding'))
            db.execute('UPDATE ef_rag_generations SET status=?,body=? WHERE id=?',('ready',index._json(body),generation['generation_id']))
    assert count==args.size,(count,args.size)
    scope=index.freeze_scope(requested_scope={'scope_type':'corpus','document_ids':[d['id'] for d in docs]})
    cases=[('literal',f'record{args.size//5:08d}',args.size//5,None),
        ('typo','DailyDialog retrieval memory',args.size//3,None),
        ('vector','汽车设备采购的适用说明',args.size-1,None),
        ('filtered','record00000005 revenue policy region',5,[docs[0]['id']])]
    cases=[{'name':name,'query':query,'expected_chunk_id':chunks_by_record[number]['id'],
        'expected_document_id':chunks_by_record[number]['document_id'],'document_ids':ids} for name,query,number,ids in cases]
    write(output/'scope.json',scope);write(output/'cases.json',cases)
    legacy_bytes=None;legacy_build_ms=None
    if args.baseline_code:
        old=reference(args.baseline_code);before_publish=time.monotonic()
        old_manifest=old.index._publish_lexical()
        legacy_build_ms=round((time.monotonic()-before_publish)*1000,3)
        assert old_manifest['generations']==scope['documents'],'对照索引的固定来源发生变化'
        old_scope={**scope,'lexical_generation':old_manifest['lexical_generation'],'lexical_sha256':old_manifest['sha256']}
        old_scope['scope_id']=index._hash({k:v for k,v in old_scope.items() if k not in {'scope_id','created_at'}})
        write(output/'baseline-scope.json',old_scope)
        legacy_bytes=old.index.lexical_path(old_scope).stat().st_size
        index._publish_lexical();old.index.POOL.shutdown(wait=True)
    files=[s.APP_DB,index.lexical_path(scope)]
    write(output/'build.json',{'chunks':count,'documents':len(docs),'dimension':DIMENSION,
        'milliseconds':round((time.monotonic()-started)*1000,3),'database_bytes':sum(p.stat().st_size for p in files),
        'lexical_bytes':files[1].stat().st_size,'legacy_lexical_bytes':legacy_bytes,
        'legacy_publication_build_ms':legacy_build_ms,
        'data_kind':'authored Markdown and seeded numeric vectors; no real embedding model'})
    index.POOL.shutdown(wait=True)


def reference(folder):
    modules={}
    for filename in ('documents','rag_index','rag_retrieval'):
        spec=importlib.util.spec_from_file_location('backend.scale_baseline_'+filename,folder/(filename+'.py'))
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module);modules[filename]=module
    modules['rag_index'].documents=modules['documents']
    modules['rag_retrieval'].documents=modules['documents'];modules['rag_retrieval'].index=modules['rag_index']
    return modules['rag_retrieval']


def measure(args):
    output=args.output/str(args.size);provider,np=prepare(output)
    from backend import store as s,rag_index as index,rag_retrieval as retrieval
    index.initialize();scope_file=output/('baseline-scope.json' if args.worker=='baseline' and (output/'baseline-scope.json').exists() else 'scope.json')
    scope=json.loads(scope_file.read_text(encoding='utf-8'))
    cases=json.loads((output/'cases.json').read_text(encoding='utf-8'))
    with index.reader() as db:
        for case in cases:
            row=db.execute('SELECT vector FROM ef_rag_embeddings WHERE chunk_id=? AND fingerprint=?',(case['expected_chunk_id'],provider.describe('embedding')['fingerprint'])).fetchone()
            provider.targets[case['query']]=np.frombuffer(row['vector'],dtype='<f4').tolist()
    if args.worker=='baseline' and args.baseline_code:
        engine=reference(args.baseline_code);search=engine.search;label='pre-change source'
    elif args.worker=='baseline':
        search=retrieval._search;label='same-version full-materialization reference'
    else:search=retrieval.search;label='optimized'
    publication=index.lexical_path(scope)
    with sqlite3.connect(s.readonly_uri(publication),uri=True) as db:publication_schema=db.execute('PRAGMA user_version').fetchone()[0]
    before=memory();samples=[];highwater=[before['working_set_bytes']];memory_errors=[];stop=threading.Event()
    def sample_memory():
        while not stop.wait(.002):
            try:
                current=memory()['working_set_bytes']
                if current is not None:highwater[0]=max(highwater[0] or 0,current)
            except OSError as exc:memory_errors.append(str(exc));return
    sampler=threading.Thread(target=sample_memory,daemon=True);sampler.start()
    def run(case):
        started=time.monotonic();result=search(case['query'],scope,document_ids=case['document_ids'])
        return {'case':case['name'],'milliseconds':round((time.monotonic()-started)*1000,3),
            'target_recalled':any(e['locator']['document_id']==case['expected_document_id'] and e['retrieval']['chunk_id']==case['expected_chunk_id'] for e in result['evidence']),
            'dense_top30':[r['id'] for r in result['retrieval']['channels'].get('dense',[])],
            'bm25_top30':[r['id'] for r in result['retrieval']['channels'].get('bm25',[])],
            'execution':result['retrieval'].get('execution'),
            'stages':[{k:v for k,v in stage.items() if k in {'stage','duration_ms','status','count'}} for stage in result['stages']]}
    cold=run(cases[0])
    for case in cases:run(case)
    for repeat in range(args.repeats):
        for case in cases:samples.append(run(case))
    stop.set();sampler.join(3)
    if memory_errors:raise RuntimeError('实际内存采样失败')
    latencies=sorted(row['milliseconds'] for row in samples)
    result={'engine':label,'chunks':args.size,'samples':samples,'cold_first_query':cold,
        'median_ms':round(statistics.median(latencies),3),'p95_ms':latencies[max(0,math.ceil(len(latencies)*.95)-1)],
        'sample_count':len(samples),'target_recall_at8':sum(row['target_recalled'] for row in samples)/len(samples),
        'memory_before':before,'memory_after':memory(),'query_embedding_calls':provider.calls,
        'publication_schema':publication_schema,'publication_bytes':publication.stat().st_size,
        'sampled_query_peak_working_set_bytes':highwater[0], 'memory_sample_interval_ms':2,
        'memory_meaning':'OS lifetime process peak plus sampled query working set (Windows, 2ms); first query is process-cold, not flushed OS disk cache'}
    write(output/(args.worker+'.json'),result)
    index.POOL.shutdown(wait=True)
    if args.worker=='baseline' and args.baseline_code:engine.index.POOL.shutdown(wait=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--baseline-code',type=Path)
    parser.add_argument('--sizes',default='1000,10000,50000');parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--worker',choices=['build','baseline','optimized']);parser.add_argument('--size',type=int)
    args=parser.parse_args();args.output=args.output.resolve()
    if args.output==ROOT or ROOT in args.output.parents:parser.error('output必须位于项目外部')
    if args.repeats<1:parser.error('repeats必须为正数')
    if args.baseline_code:args.baseline_code=args.baseline_code.resolve()
    if args.worker:
        return build(args) if args.worker=='build' else measure(args)
    args.output.mkdir(parents=True,exist_ok=True);rows=[]
    source_files=[ROOT/'01_程序代码/backend'/name for name in ('documents.py','rag_index.py','rag_retrieval.py','rag_candidates.py')]+[Path(__file__)]
    hashes=lambda:{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}
    before_hashes=hashes()
    baseline_hashes={name:hashlib.sha256((args.baseline_code/name).read_bytes()).hexdigest() for name in ('documents.py','rag_index.py','rag_retrieval.py')} if args.baseline_code else None
    for size in [int(value) for value in args.sizes.split(',')]:
        if size<10:parser.error('每组至少10个切片')
        for worker in ('build','baseline','optimized'):
            command=[sys.executable,'-X','utf8','-B',str(Path(__file__).resolve()),'--output',str(args.output),
                '--worker',worker,'--size',str(size),'--repeats',str(args.repeats)]
            if args.baseline_code:command+=['--baseline-code',str(args.baseline_code)]
            result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=1800)
            (args.output/(str(size)+'-'+worker+'.log')).write_text(result.stdout+result.stderr,encoding='utf-8')
            if result.returncode:raise RuntimeError(worker+'失败：'+(result.stdout+result.stderr)[-3000:])
        before=json.loads((args.output/str(size)/'baseline.json').read_text(encoding='utf-8'))
        after=json.loads((args.output/str(size)/'optimized.json').read_text(encoding='utf-8'))
        assert len(before['samples'])==len(after['samples'])
        exact=all(a['case']==b['case'] and a['dense_top30']==b['dense_top30'] and a['bm25_top30']==b['bm25_top30'] for a,b in zip(before['samples'],after['samples']))
        row={'chunks':size,'build':json.loads((args.output/str(size)/'build.json').read_text(encoding='utf-8')),
            'baseline':before,'optimized':after,'dense_and_bm25_order_identical':exact,
            'median_speedup':round(before['median_ms']/after['median_ms'],3),
            'peak_process_memory_reduction':round(1-after['memory_after']['peak_working_set_bytes']/before['memory_after']['peak_working_set_bytes'],4)}
        if before['sampled_query_peak_working_set_bytes'] and after['sampled_query_peak_working_set_bytes']:
            row['sampled_query_memory_reduction']=round(1-after['sampled_query_peak_working_set_bytes']/before['sampled_query_peak_working_set_bytes'],4)
        rows.append(row);write(args.output/'summary.json',{'kind':'authored scale diagnostic, not answer accuracy',
            'source_sha256':hashes(),'baseline_sha256':baseline_hashes,'source_unchanged':before_hashes==hashes(),'scales':rows})
        print(json.dumps({k:row[k] for k in ('chunks','dense_and_bm25_order_identical','median_speedup','peak_process_memory_reduction')}) ,flush=True)
        if not exact or after['target_recall_at8']<before['target_recall_at8']:raise RuntimeError('排序或目标召回发生回退')
    if before_hashes!=hashes():raise RuntimeError('诊断期间源码发生变化，请对最终版本重测')


if __name__=='__main__':main()
