"""API-free scale evidence and preliminary-score coverage, not model scoring.

The benchmark calls the real read-only SQL executor on isolated controlled
SQLite files. It never starts a model gateway or imports a business workspace.
Retrieval evidence is summarized from an existing final run when supplied.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from backend.parsers import sqlite_tables
from backend.sql_engine import execute_sql
from evaluation.core import digest


def write(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def stamp():
    return datetime.now(timezone.utc).isoformat()


def hardware():
    item={'os':platform.platform(),'machine':platform.machine(),'python':platform.python_version(),
          'sqlite':sqlite3.sqlite_version,'logical_cpu_count':os.cpu_count()}
    if platform.system()=='Darwin':
        for key in ('machdep.cpu.brand_string','hw.memsize','hw.physicalcpu'):
            try:
                item[key]=subprocess.check_output(['/usr/sbin/sysctl','-n',key],text=True,timeout=2).strip()
            except (OSError,subprocess.SubprocessError):item[key]=None
    return item


def sql_scale(root, output):
    asset=output/'受控输入/性能尺度';asset.mkdir(parents=True,exist_ok=True)
    report={'schema_version':'acceptance-sql-scale.v1','created_at':stamp(),'environment':hardware(),
            'model_api_calls':0,'endpoint_calls':0,'measurement_kind':'measured_local_sql_executor',
            'executor':'backend.sql_engine.execute_sql','executor_sha256':digest(root/'backend/sql_engine.py'),
            'script_sha256':digest(Path(__file__)),
            'timing_boundary':'execute_sql调用前至结果返回，包括AST校验、只读连接、SQLite权限校验、查询、来源记录及序列化准备；不含数据构造、Schema导入、模型规划/复核和UI。',
            'cache_boundary':'没有活动TaskScope，机械结果缓存绕过。每次新SQLite连接；不清空操作系统页缓存，不能称冷盘基准。首次调用另列，后5次报告中位数。',
            'concurrency_boundary':'本地开发机器的单进程顺序测量；未隔离其他系统工作负载，不是生产并发压测。',
            'scales':[],'status':'running'}
    queries=[('full_aggregate','SELECT COUNT(*),SUM(amount_cents) FROM events',[]),
             ('indexed_filter','SELECT COUNT(*),SUM(amount_cents) FROM events WHERE region=? AND status=?',['区域01','已完成'])]
    for size in (1000,10000):
        dbpath=asset/f'性能受控记录_{size}.sqlite'
        records=[(i,f'区域{i%20:02d}','已完成' if i%4 else '取消',(i*37)%100000-5000) for i in range(1,size+1)]
        if not dbpath.exists():
            with sqlite3.connect(dbpath) as db:
                db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,region TEXT NOT NULL,status TEXT NOT NULL,amount_cents INTEGER NOT NULL)')
                db.executemany('INSERT INTO events VALUES(?,?,?,?)',records)
                db.execute('CREATE INDEX idx_events_region_status ON events(region,status)')
        with sqlite3.connect(dbpath.as_uri()+'?mode=ro&immutable=1',uri=True) as db:
            db.execute('PRAGMA query_only=ON')
            if db.execute('SELECT id,region,status,amount_cents FROM events ORDER BY id').fetchall()!=records:
                raise ValueError('Controlled scale input has changed')
        sha=digest(dbpath);tables,warnings=sqlite_tables(dbpath)
        source={'id':f'offline-scale-{size}','name':dbpath.name,'format':'sqlite','data_path':str(dbpath),
                'sha256':sha,'parser_version':'controlled-scale-v1','tables':tables,'warnings':warnings,'metadata':{}}
        entry={'rows':size,'path':str(dbpath),'sha256':sha,'bytes':dbpath.stat().st_size,
               'origin':'Deterministic synthetic performance input; no real business claim','queries':[]}
        for name,sql,params in queries:
            selected=records if name=='full_aggregate' else [r for r in records if r[1]==params[0] and r[2]==params[1]]
            expected=[[len(selected),sum(r[3] for r in selected)]]
            samples=[]
            for repeat in range(6):
                start=time.perf_counter_ns();result=execute_sql(source,sql,params);elapsed=time.perf_counter_ns()-start
                if result['rows']!=expected or result['truncated']:
                    raise ValueError('Measured SQL result did not match independent Python reference')
                if (result.get('cache') or {}).get('status')=='hit':
                    raise ValueError('Mechanical cache result was not eligible as a new SQL execution')
                samples.append({'repeat':repeat,'elapsed_ns':elapsed,'elapsed_ms':elapsed/1_000_000,
                                'executor_reported_ms':result['elapsed_ms'],'rows':result['rows'],
                                'query_plan':result['query_plan'],'result_truncated':result['truncated']})
            warm=[s['elapsed_ms'] for s in samples[1:]]
            entry['queries'].append({'name':name,'sql':sql,'parameters':params,'independent_python_rows':expected,
                                     'samples':samples,'first_invocation_ms':samples[0]['elapsed_ms'],
                                     'warm_median_ms':statistics.median(warm),'warm_min_ms':min(warm),'warm_max_ms':max(warm),
                                     'checks':['all_six_results_match_independent_python','read_only_product_executor','no_truncation','no_model_call']})
        entry['input_unchanged']=digest(dbpath)==sha
        if not entry['input_unchanged']:raise ValueError('Read-only benchmark changed input')
        report['scales'].append(entry)
    report['theory']={
        'classification':'理论分析，不是测得复杂度或延迟保证',
        'full_aggregate':'COUNT+SUM无条件聚合需要读取全部金额，SQLite执行部分随n近似O(n)，结果仅1行。',
        'indexed_filter':'复合B-tree索引匹配region,status，定位约O(log n)，累加匹配m行约O(m)；需以实际EXPLAIN QUERY PLAN确认索引采用。',
        'pipeline':'总耗时还包含AST解析、连接、安全校验和来源构造等固定成本；不能由10倍行数推断整体必为10倍。',
        'rag':'当前精确向量扫描需读取选中n个d维向量，计算量约O(n*d)，分批扫描控制峰值内存，不等于ANN。切片邻页规则与候选重排还带来额外成本。',
        'end_to_end':'问答端到端=规划+查询/检索+必要视觉+生成/复核+发布；本文件未测模型阶段，不报端到端毫秒级结论。'}
    report['bottlenecks_and_strategy']=[
        '本次小规模查询固定开销可能主导。大表筛选使用真实有用索引，禁止为测试硬编码答案。',
        '复杂JOIN先校验粒度与连接关系，必要时先聚合再连接，不能为加速静默LIMIT或删条件。',
        '复用已解析页面与索引；仅受影响部分重建，不反复OCR全库。',
        '生产机械缓存仅复用来源版本一致的实际结果，模型语义复核仍需执行。',
        '真实模型阶段耗时由本轮付费问答账本补充；目前不使用历史API失败耗时估计正常回答速度。']
    report.update(status='complete',completed_at=stamp())
    write(output/'原始证据/离线两规模SQL性能.json',report)
    return report


def summarize_retrieval(path, output):
    raw=json.loads(path.read_text(encoding='utf-8'))
    if raw.get('status')!='complete' or len(raw.get('cases',[]))!=24:
        raise ValueError('Only a complete 24-case retrieval run can be summarized')
    scopes={}
    for name in ('all_documents','same_series'):
        rows=[]
        for case in raw['cases']:
            value=case['scopes'][name];audit=value.get('audit',{})
            rows.append({'case_id':case['id'],'elapsed_ms':value.get('elapsed_seconds',0)*1000,
                         'sources':audit.get('source_count'),'indexed_chunks':audit.get('indexed_chunks'),
                         'vector_rows_scanned':audit.get('vector_rows_scanned'),'library_complete':audit.get('library_complete'),
                         'mode_used':audit.get('mode_used'),'error_type':value.get('error_type')})
        valid=[r['elapsed_ms'] for r in rows if not r['error_type']]
        scopes[name]={'samples':rows,'measured_queries':len(rows),'successful_queries':len(valid),
                      'median_ms':statistics.median(valid) if valid else None,'min_ms':min(valid) if valid else None,
                      'max_ms':max(valid) if valid else None,'summary':raw.get('summary',{}).get(name)}
    result={'schema_version':'acceptance-retrieval-scale.v1','created_at':stamp(),
            'source_result':str(path.resolve()),'source_result_sha256':digest(path),
            'model_api_calls_added':0,'measurement_kind':'summary_of_existing_local_retrieval_measurements',
            'indexed_document_count':raw.get('indexed_document_count'),'required_document_count':raw.get('required_document_count'),
            'backend_code_sha256':raw.get('backend_code_sha256'),'scopes':scopes,
            'boundaries':['同一24题在全库与预知同系列范围的已有测量，不额外跑模型或Embedding。',
                          'same_series是已知资料范围，不能当系统自动选准系列能力。',
                          '此处是检索耗时，含原runner计时范围，不含模型生成或UI。',
                          '各题小范围的文档数可能不同，须按samples逐题报告，不能笼统写成统一的1份对12份。',
                          '序列运行与查询缓存可能影响耗时；不是隔离冷/热缓存重复实验。']}
    write(output/'原始证据/离线检索范围性能.json',result)
    return result


def document_inventory(root):
    from pypdf import PdfReader
    folder=root.parent/'01_初赛提交';items=[]
    for p in sorted(folder.iterdir()):
        if not p.is_file():continue
        row={'name':p.name,'path':str(p),'bytes':p.stat().st_size,'sha256':digest(p)}
        if p.suffix.lower()=='.pdf':row['pages']=len(PdfReader(p).pages)
        items.append(row)
    return {'checked_at':stamp(),'read_only':True,'items':items,
            'boundary':'只读存在性、页数与文件摘要；没有修改或完成版式、图表、事实一致性和最终演示验收。'}


def coverage_matrix(root, output):
    plan=json.loads((output/'原始证据/冻结计划.json').read_text(encoding='utf-8'))
    groups=plan['scoring_sets'];evidence=output/'原始证据'
    entries=[
        ('基础单表NL2SQL',10,groups['single_table_sql'],'真实问答与参考SQL/最终答案核验；当前等待执行记录。','pending_real_answers'),
        ('基础单文档RAG',10,groups['single_document_rag'],'真实答案逐事实、单位、条件及引用核验。','pending_real_answers'),
        ('可解释性',5,groups['original20']+groups['complex_specialty'],'保存实际SQL、计算、原页引用与工具事实；不要求模型隐藏推理。','pending_actual_trace_review'),
        ('交互Demo',5,groups['original20'],'本阶段只读现有界面流程；API成功不等于UI交互通过。界面完善留下一阶段。','pending_ui_readonly_acceptance'),
        ('SQL执行准确率',10,groups['single_table_sql'],'固定10题分母，区分执行成功、结果正确、最终答案正确；按官方档位。','pending_real_answers'),
        ('RAG忠实度',10,groups['single_document_rag'],'固定10题分母，核对实际回答与原文支持；服务失败保留。','pending_real_answers'),
        ('系统效率',5,[],'实际产品SQL执行器1千/1万行，理论复杂度与实测分开；模型端到端耗时待账本。','offline_sql_evidence_ready'),
        ('中级任务',15,groups['multi_table_sql']+groups['clarification']+['工程A01-Q1'],'多表、澄清闭环、公式识别计算完整链；至少一项为有效参赛门槛，不能靠代码存在计满。','pending_real_answers'),
        ('鲁棒性',10,groups['clarification']+groups['six_turn_context'],'缺条件/假设撤销真实案例，既有OCR锚点及离线异常模拟分别标注；不是全库多模态准确率。','partially_offline_pending_real'),
        ('多轮上下文',5,groups['six_turn_context'],'同一会话连续6轮；每轮与整链分开记录，失败父状态不能补造。','pending_real_chain'),
        ('文档质量',10,[],'只读检查现有文件与页数，最终数据回填/格式一致性由下一阶段完成。','final_stage_reserved'),
        ('线上功能演示',5,groups['complex_specialty']+groups['six_turn_context'],'本轮可留真实日志/截图；最终截图选择与评审HTML编排留下一阶段。','final_stage_reserved'),
    ]
    rows=[{'item':name,'max_score':maximum,'case_ids':ids,'evidence_requirement':requirement,
           'status':status,'self_score':None,'score_boundary':'待实际证据逐项评判；不预支后续改进分。'}
          for name,maximum,ids,requirement,status in entries]
    assert sum(row['max_score'] for row in rows)==100
    sources=[{'name':p.name,'path':str(p),'sha256':digest(p)} for p in [
        evidence/'离线两规模SQL性能.json',evidence/'离线检索范围性能.json',
        evidence/'上下文完整性离线修复记录.json',evidence/'离线回归.txt'] if p.exists()]
    matrix={'schema_version':'acceptance-score-matrix.v1','created_at':stamp(),'plan_sha256':plan['plan_sha256'],
            'official_requirement':{'path':str(root.parent/'03_内部资料/赛题八官方要求.pdf'),'pages':[10,11]},
            'max_total':100,'self_score_total':None,'classification':'按官方规则的本地自评证据映射，非官方软件得分',
            'rows':rows,'offline_evidence':sources,'document_inventory':document_inventory(root),
            'corpus_boundaries':{'real_business_corpus_documents':12,'real_complex_sources_in_33turn_plan':3,
                                'all12_retrieval':'需最终24题全库结果证明，不能由33轮所选3份外推。',
                                'all12_answer_api':'需后端12来源范围校验/接口离线测试；library支持128本身不证明问答接口已接通。'},
            'scoring_policy':{'fixed_denominator':True,'api_failures_not_silently_dropped':True,
                              'thresholds':{'70_percent':6,'80_percent':8,'90_percent':10},
                              'coverage_not_full_score':True,'no_final_stage_credit_in_advance':True}}
    target=output/'评分证据矩阵.json'
    if target.exists():
        previous=json.loads(target.read_text(encoding='utf-8'))
        if previous.get('plan_sha256')!=matrix['plan_sha256']:
            raise ValueError('Existing matrix belongs to a different frozen plan')
        # Refreshing offline evidence must not erase a subsequently recorded
        # real-answer judgement or a separately supplied API-scope receipt.
        for key in ('source_scope_offline_validation','self_score_total','final_review'):
            if key in previous:matrix[key]=previous[key]
        by_name={row['item']:row for row in previous.get('rows',[])}
        for row in matrix['rows']:
            old=by_name.get(row['item'],{})
            if old.get('review') or old.get('self_score') is not None:
                for key in ('status','self_score','score_boundary','review','actual_evidence'):
                    if key in old:row[key]=old[key]
    write(target,matrix)
    return matrix


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output',type=Path)
    parser.add_argument('--retrieval-result',type=Path)
    parser.add_argument('--summary-only',action='store_true',help='Reuse existing SQL measurements; do not rerun')
    args=parser.parse_args();root=args.root.resolve();output=(args.output or root.parent/'03_内部资料/提交前功能验收').resolve()
    if not args.summary_only:
        target=output/'原始证据/离线两规模SQL性能.json'
        if target.exists():raise ValueError('SQL measurements already exist; use --summary-only to reuse them')
        result=sql_scale(root,output)
        print(json.dumps({'sql_scales':[{'rows':s['rows'],'queries':[{'name':q['name'],'median_ms':q['warm_median_ms']} for q in s['queries']]} for s in result['scales']]},ensure_ascii=False))
    if args.retrieval_result:summarize_retrieval(args.retrieval_result.resolve(),output)
    coverage_matrix(root,output)
    print(json.dumps({'output':str(output),'model_api_calls':0,'matrix_total':100,'score':'not_yet_assessed'},ensure_ascii=False))


if __name__=='__main__':main()
