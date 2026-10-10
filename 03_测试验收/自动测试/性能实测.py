"""Measure the production local pipeline on isolated public synthetic data.

No model key or paid text call is used. First-use means this process's first
operation, not a flushed OS disk cache. Small samples are diagnostic timings.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]


def percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered)-1)*fraction
    lower, upper = math.floor(position), math.ceil(position)
    return round(ordered[lower]+(ordered[upper]-ordered[lower])*(position-lower), 2)


def timings(values):
    return {'samples_ms':[round(v,2) for v in values], 'samples':len(values),
            'p50_ms':percentile(values,.5), 'p95_ms':percentile(values,.95)}


def hardware():
    result={'os':platform.platform(),'architecture':platform.machine(),
            'logical_cpus':os.cpu_count(),'python':platform.python_version()}
    if platform.system()=='Darwin':
        for field,key in [('cpu','machdep.cpu.brand_string'),('memory_bytes','hw.memsize')]:
            read=subprocess.run(['sysctl','-n',key],capture_output=True,text=True)
            if read.returncode==0:result[field]=read.stdout.strip()
    result['libraries']={name:importlib.metadata.version(name) for name in
                         ['PyMuPDF','onnxruntime','tokenizers','sqlglot','numpy']}
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    parser.add_argument('--repeats',type=int,default=7)
    parser.add_argument('--pages',type=int,nargs='+',default=[10,100])
    parser.add_argument('--background-note',default='未检查其他进程负载')
    args=parser.parse_args()
    if not 3<=args.repeats<=20 or any(n not in [10,100,500] for n in args.pages):
        parser.error('重复数应为3至20，页数只能选择10、100、500')
    output=Path(args.output).resolve()
    with output.open('x',encoding='utf-8') as handle:handle.write('{}\n')
    work=Path(tempfile.mkdtemp(prefix='evidenceflow-performance-'))
    os.environ['EVIDENCEFLOW_DATA_DIR']=str(work)
    os.environ['EVIDENCEFLOW_RAG_MODEL_DIR']=str(ROOT/'02_数据资料/运行数据/models')
    sys.path.insert(0,str(ROOT/'01_程序代码'))
    report={'phase':'preparing','started_at':datetime.now(timezone.utc).isoformat(),
        'hardware':hardware(),'background_note':args.background_note,
        'public_synthetic_data':True,'isolated_runtime':True,'paid_text_model_calls':0,
        'database':[],'documents':[],'providers':{},'cancellations':[],
        'boundary':'组件实测，不含远程文本生成、浏览器绘制或Windows实机；首次使用未清空操作系统缓存；小样本P95仅描述此次观测。'}
    def checkpoint():
        temporary=output.with_suffix(output.suffix+'.tmp')
        temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        temporary.replace(output)
    def measured(action):
        start=time.perf_counter();value=action()
        return value,(time.perf_counter()-start)*1000
    def cancelled_measure(label,action):
        try:
            _,duration=measured(action)
            report['cancellations'].append({'operation':label,'status':'unexpected_return','duration_ms':round(duration,2)})
        except InterruptedError:
            report['cancellations'].append({'operation':label,'status':'cancelled'})
    try:
        from backend import main as app,store as s,connections,database_query as dbq
        from backend import documents,rag_index,rag_providers,rag_retrieval
        import pymupdf
        app.init_app();report['phase']='running';checkpoint()
        report['source_sha256']={name:s.digest(ROOT/name) for name in [
            '01_程序代码/backend/database_query.py','01_程序代码/backend/sql_guard.py',
            '01_程序代码/backend/documents.py','01_程序代码/backend/rag_index.py',
            '01_程序代码/backend/rag_retrieval.py','01_程序代码/backend/rag_providers.py']}
        sql='SELECT sites.name, SUM(metrics.amount_cny) AS amount_cny, COUNT(metrics.id) AS item_count FROM sites JOIN devices ON devices.site_id = sites.id JOIN metrics ON metrics.device_id = devices.id GROUP BY sites.id, sites.name ORDER BY sites.name'
        for count in [1000,10000,100000]:
            path=work/('public-'+str(count)+'.sqlite')
            with sqlite3.connect(path) as db:
                db.executescript('CREATE TABLE sites(id INTEGER PRIMARY KEY,name TEXT);CREATE TABLE devices(id INTEGER PRIMARY KEY,site_id INTEGER REFERENCES sites(id));CREATE TABLE metrics(id INTEGER PRIMARY KEY,device_id INTEGER REFERENCES devices(id),amount_cny INTEGER);')
                db.executemany('INSERT INTO sites VALUES (?,?)',[(i,'Site '+str(i)) for i in range(1,11)])
                db.executemany('INSERT INTO devices VALUES (?,?)',[(i,(i-1)%10+1) for i in range(1,101)])
                db.executemany('INSERT INTO metrics VALUES (?,?,?)',((i,(i-1)%100+1,i%1000) for i in range(1,count+1)))
            before=s.digest(path)
            connection=connections.create(connections.Config(name='公开性能库'+str(count),kind='sqlite',path=str(path)))
            if not connections.test(connection['id'])['last_test']['ok']:raise ValueError('公开性能库只读探针失败')
            connections.enabled(connection['id'],connections.Enabled(enabled=True))
            with sqlite3.connect(s.readonly_uri(path),uri=True) as db:
                expected=[{'name':r[0],'amount_cny':r[1],'item_count':r[2]} for r in db.execute(sql)]
            snapshot,schema_ms=measured(lambda:dbq.inspect(connection['id'],query='metrics amount_cny sites'))
            action=lambda:dbq.execute(sql,connection_id=connection['id'],snapshot=snapshot)
            first,first_ms=measured(action);samples=[]
            if first['rows']!=expected:raise ValueError('生产查询与独立参考结果不一致')
            for _ in range(args.repeats):
                result,duration=measured(action)
                if result['rows']!=expected:raise ValueError('重复查询结果不一致')
                samples.append(duration)
            with ThreadPoolExecutor(max_workers=2) as pool:
                concurrent=list(pool.map(lambda _:measured(action),range(2)))
            if any(value['rows']!=expected for value,_ in concurrent):raise ValueError('并发查询结果不一致')
            stop=threading.Event();stop.set()
            cancelled_measure('database-'+str(count)+'-pre-cancelled',lambda:dbq.execute(sql,connection_id=connection['id'],snapshot=snapshot,cancelled=stop))
            item={'metric_rows':count,'tables':3,'reference_rows':10,'schema_first_ms':round(schema_ms,2),
                  'query_first_ms':round(first_ms,2),'warm':timings(samples),'concurrency_2':timings([v for _,v in concurrent]),
                  'independent_result_match':True,'file_unchanged':before==s.digest(path)}
            report['database'].append(item);checkpoint();print(json.dumps({'database_rows':count,'p50_ms':item['warm']['p50_ms']},ensure_ascii=False),flush=True)
        # Capture model initialization separately from indexed retrieval, whose
        # embedding session is already warm after building document vectors.
        embedded,embed_first=measured(lambda:rag_providers.embed(['公开性能样本：检修分钟与服务费用。']))
        ranked,rerank_first=measured(lambda:rag_providers.rerank('检修费用',['服务费为300元。','库存数量为10件。']))
        embed_warm=[];rerank_warm=[]
        for _ in range(args.repeats):
            _,elapsed=measured(lambda:rag_providers.embed(['公开性能样本：检修分钟与服务费用。']));embed_warm.append(elapsed)
            _,elapsed=measured(lambda:rag_providers.rerank('检修费用',['服务费为300元。','库存数量为10件。']));rerank_warm.append(elapsed)
        report['providers']={'embedding':{'provider':embedded['provider'],'process_first_ms':round(embed_first,2),'warm':timings(embed_warm)},
                             'rerank':{'provider':ranked['provider'],'process_first_ms':round(rerank_first,2),'passages':2,'warm':timings(rerank_warm)}}
        checkpoint()
        for pages in args.pages:
            path=s.UPLOADS/('公开性能样本-'+str(pages)+'.pdf')
            with pymupdf.open() as pdf:
                for n in range(1,pages+1):
                    page=pdf.new_page()
                    page.insert_text((40,50),'Public synthetic maintenance manual - Page '+str(n),fontsize=14)
                    page.insert_text((40,90),'Site '+str((n-1)%10+1)+': warranty 12 months; basic service fee CNY 300.',fontsize=11)
                    page.insert_text((40,120),'Only billable labor minutes are used for the service calculation.',fontsize=11)
                    page.insert_text((40,150),'Policy reference page '+str(n)+'. Blank values require clarification.',fontsize=11)
                pdf.save(path)
            document,parse_ms=measured(lambda:s.index_document(path))
            if document['status']=='failed' or document['pages']!=pages:raise ValueError('生产PDF解析未完成')
            first_page,view_first=measured(lambda:documents.get_page(document['id'],1))
            view_samples=[]
            for _ in range(args.repeats):
                viewed,elapsed=measured(lambda:documents.get_page(document['id'],pages))
                if viewed['number']!=pages:raise ValueError('返回页不一致')
                view_samples.append(elapsed)
            job,index_ms=measured(lambda:rag_index.submit_index([document['id']],synchronous=True))
            if job['status']!='completed':raise ValueError('本机文档向量索引未完成')
            scope=rag_index.freeze_scope(requested_scope={'scope_type':'document','document_ids':[document['id']]})
            query='What is the basic service fee for Site 1?'
            action=lambda:rag_retrieval.search(query,scope)
            first,first_ms=measured(action)
            samples=[];stages=[];errors=[];located=0;returned=0
            for _ in range(args.repeats):
                result,elapsed=measured(action);samples.append(elapsed);stages.append(result['stages'])
                errors.extend(result['retrieval']['errors'])
                for evidence in result['evidence']:
                    returned+=1
                    located+=documents.resolve(evidence['locator'],evidence['snippet'])['status']=='located'
            with ThreadPoolExecutor(max_workers=2) as pool:
                concurrent=list(pool.map(lambda _:measured(action),range(2)))
            for result,_ in concurrent:errors.extend(result['retrieval']['errors'])
            stop=threading.Event();stop.set()
            cancelled_measure('retrieval-'+str(pages)+'-pre-cancelled',lambda:rag_retrieval.search(query,scope,cancelled=stop))
            # Stage callback is production progress. Set cancellation after scope
            # loading so this also exercises an active pipeline, without a race.
            during=threading.Event()
            def trace(event):
                if event.get('stage')=='scope' and event.get('status')=='completed':during.set()
            start=time.perf_counter()
            try:
                rag_retrieval.search(query,scope,cancelled=during,trace=trace)
                report['cancellations'].append({'operation':'retrieval-'+str(pages)+'-after-scope','status':'unexpected_return','duration_ms':round((time.perf_counter()-start)*1000,2)})
            except InterruptedError:
                report['cancellations'].append({'operation':'retrieval-'+str(pages)+'-after-scope','status':'cancelled','duration_ms':round((time.perf_counter()-start)*1000,2)})
            item={'pages':pages,'native_parse_ms':round(parse_ms,2),'page_first_ms':round(view_first,2),'page_warm':timings(view_samples),
                  'index_ms':round(index_ms,2),'retrieval_first_after_index_ms':round(first_ms,2),'warm':timings(samples),
                  'warm_stages':stages,'concurrency_2':timings([v for _,v in concurrent]),'retrieval_errors':errors,
                  'citations_located':located,'citations_returned':returned,'native_text_only':True}
            report['documents'].append(item);checkpoint();print(json.dumps({'pages':pages,'p50_ms':item['warm']['p50_ms']},ensure_ascii=False),flush=True)
        report['phase']='completed'
    except BaseException as exc:
        report['phase']='failed';report['error']={'type':type(exc).__name__,'message':str(exc)}
        raise
    finally:
        report['finished_at']=datetime.now(timezone.utc).isoformat();checkpoint()
        shutil.rmtree(work)
    print(json.dumps({'output':str(output),'phase':report['phase']},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
