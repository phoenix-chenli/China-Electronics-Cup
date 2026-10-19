"""Isolated Stage 4 acceptance. Frozen expected results are never sent to models.

--prepare-only parses without model calls; --run-live explicitly enables requests.
Every report is new; earlier failures are retained. Private config stays in runtime.
"""
from pathlib import Path
import argparse,hashlib,json,os,shutil,sys,time,uuid
from decimal import Decimal,InvalidOperation
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
FIX=ROOT/'tests/fixtures/multimodal'
from backend.service import Service,file_digest,file_format
from backend.answer_service import AnswerService
from backend.store import identity
from backend import parsers
from backend.model_gateway import ModelGateway


def dump(path,obj):path.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')

def assess(case,answer,sources):
    """Offline structural checks; model semantic review is not independent truth."""
    result=answer.get('result') or {};reasons=[];flat=''.join(result.get('text','').split())
    want=case['expected_status'];want=want if isinstance(want,list) else [want]
    if answer.get('status') not in want:reasons.append('status:'+str(answer.get('status')))
    for term in case.get('required_text',[]):
        if term not in flat:reasons.append('missing text:'+term)
    calculations=result.get('calculations',[])
    def same_value(value,expected):
        try:
            actual,wanted=Decimal(str(value)),Decimal(str(expected))
            return actual.is_finite() and wanted.is_finite() and abs(actual-wanted)<Decimal('.00001')
        except (InvalidOperation,ValueError,TypeError):return False
    units=case.get('expected_units')
    expected_values=case.get('expected_values',[])
    if units is not None and (not isinstance(units,list) or len(units)!=len(expected_values)):
        reasons.append('invalid expected value/unit pairs');units=[]
    for index,expected in enumerate(expected_values):
        unit=units[index] if units is not None and index<len(units) else None
        if not any(same_value(c.get('value'),expected) and (unit is None or c.get('unit')==unit) for c in calculations):
            reasons.append('missing computed value:'+str(expected)+((' '+str(unit)) if unit is not None else ''))
    evidence=result.get('evidence',[]);by_id={};valid_ids=set()
    for e in evidence:
        identifier=e.get('id');source=sources.get(e.get('source_id'));valid=True
        if not isinstance(identifier,str) or not identifier or identifier in by_id:
            reasons.append('invalid or duplicate evidence id');valid=False
            valid_ids.discard(identifier)
        else:by_id[identifier]=e
        if not source or e.get('sha256')!=source.get('sha256') or e.get('parser_version')!=source.get('parser_version'):
            reasons.append('invalid source revision');valid=False
        if source and e.get('kind')=='text':
            page=next((p for p in source.get('pages',[]) if p.get('number')==e.get('page')),None)
            start,end=e.get('start'),e.get('end')
            if (not page or isinstance(start,bool) or isinstance(end,bool) or not isinstance(start,int) or not isinstance(end,int)
                    or start<0 or end<=start or page['text'][start:end]!=e.get('quote')):
                reasons.append('quote position mismatch');valid=False
        if valid:valid_ids.add(identifier)
    claims=result.get('claims',[]);cited_ids=set()
    calculation_ids={c.get('id') for c in calculations}
    for claim in claims:
        ids=claim.get('evidence_ids',[])
        if not isinstance(ids,list) or not ids or any(i not in valid_ids for i in ids):
            reasons.append('invalid claim evidence references');continue
        if any(i not in calculation_ids for i in claim.get('calculation_ids',[])):
            reasons.append('invalid claim calculation references');continue
        if not isinstance(claim.get('text'),str) or not claim['text'].strip():
            reasons.append('empty claim');continue
        cited_ids.update(ids)
    cited=[by_id[i] for i in cited_ids]
    used={e.get('source_id') for e in evidence if e.get('source_id') in sources}
    cited_sources={e['source_id'] for e in cited}
    formats=sorted({sources[s]['format'] for s in used})
    if len(formats)<case.get('min_source_formats',0):reasons.append('insufficient formats used')
    if len(cited_sources)<case.get('min_distinct_sources',0):reasons.append('insufficient cited source coverage')
    if 'expected_source_groups' in case and len(result.get('source_groups',[]))!=case['expected_source_groups']:reasons.append('source grouping differs')
    if case.get('must_not_publish_calculation') and calculations:reasons.append('unexpected calculation')
    checked=[e for e in cited if e.get('visual_checked') is True]
    visual_formats={sources[e['source_id']]['format'] for e in checked}
    visual_pages={(e['source_id'],e['page']) for e in checked if isinstance(e.get('page'),int) and e['page']>0}
    for e in checked:
        visual_pages.update((e['source_id'],q['page']) for q in e.get('table_quality',[]) if q.get('visual_required') and isinstance(q.get('page'),int) and q['page']>0)
    if case.get('requires_visual_review') and not checked:reasons.append('no cited checked image')
    if not set(case.get('required_visual_source_formats',[])).issubset(visual_formats):reasons.append('required source format not visually checked')
    if len(visual_pages)<case.get('min_visual_pages',0):reasons.append('insufficient visually checked pages')
    read_ids={eid for step in result.get('steps',[]) if step.get('kind') in ('search','sql') for eid in step.get('evidence_ids',[]) if eid in valid_ids}
    if case.get('requires_negative_review') and not (result.get('verification',{}).get('negative_review') is True and claims and cited_ids and cited_ids & read_ids):
        reasons.append('no evidence-backed negative review')
    if len({(e['source_id'],e.get('page')) for e in evidence if e.get('page')})<case.get('min_evidence_pages',0):reasons.append('insufficient page coverage')
    for term in case.get('required_evidence_text',[]):
        if not any(term in json.dumps(e,ensure_ascii=False) for e in evidence):reasons.append('missing evidence:'+term)
    depths={}
    for step in result.get('steps',[]):depths[step['id']]=1+max([depths.get(parent,0) for parent in step.get('depends_on',[])] or [0])
    if max(depths.values(),default=0)<case.get('min_dependency_depth',0):reasons.append('dependency depth too shallow')
    return {'passed':not reasons,'reasons':list(dict.fromkeys(reasons)),'used_source_formats':formats,
            'cited_source_count':len(cited_sources),'cited_source_formats':sorted({sources[s]['format'] for s in cited_sources}),
            'visually_checked_pages':[{'source_id':sid,'page':page} for sid,page in sorted(visual_pages)],
            'dependency_depth':max(depths.values(),default=0)}


def main():
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--spec',default='cases.frozen.json');p.add_argument('--only',nargs='*');p.add_argument('--run-live',action='store_true');p.add_argument('--prepare-only',action='store_true');args=p.parse_args()
    if not args.run_live and not args.prepare_only:p.error('Use --prepare-only or explicit --run-live')
    if args.output.exists():p.error('Reports are immutable; choose a new output')
    runtime=args.runtime.resolve()
    if runtime==ROOT/'.runtime' or not runtime.name.startswith('stage4-'):p.error('Use an isolated stage4-* runtime')
    spec=FIX/args.spec
    if spec.parent!=FIX or not spec.with_suffix('.sha256').exists() or spec.with_suffix('.sha256').read_text().split()[0]!=file_digest(spec):p.error('Frozen spec hash mismatch')
    cases=json.loads(spec.read_text())['cases'];cases=[c for c in cases if not args.only or c['id'] in args.only]
    if not cases:p.error('No selected cases')
    runtime.mkdir(parents=True,exist_ok=True)
    if args.run_live and not (runtime/'model.json').exists():
        fd=os.open(runtime/'model.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write((ROOT/'.runtime/model.json').read_text())
    traces=[]
    class RecordedGateway(ModelGateway):
        def __call__(self,messages,purpose):
            response=super().__call__(messages,purpose)
            # Only this explicit public/synthetic benchmark records model output.
            # Credentials and outgoing request bodies are never copied.
            traces.append({'purpose':purpose,'response':response})
            dump(args.output.with_suffix('.trace.json'),traces)
            return response
    service=Service(runtime,ROOT/'samples',False);service.start();answers=AnswerService(service,RecordedGateway);answers.start()
    report={'spec':args.spec,'spec_sha256':file_digest(spec),'parser_version':parsers.PARSER_VERSION,'code_sha256':{str(f.relative_to(ROOT)):file_digest(f) for f in (ROOT/'backend').glob('*.py')},'synthetic':json.loads(spec.read_text()).get('synthetic'),'sources':[],'cases':[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    try:
        projects=service.store.projects();project=projects[0] if projects else service.store.create_project('第四阶段隔离验收','真实公开材料与显式合成夹具分别统计')
        pid=project['id'];report['project_id']=pid
        public={x['file']:x for x in json.loads((FIX/'public-sources.json').read_text())}
        synthetic={x['file']:x for x in json.loads((FIX/'sources.json').read_text())}
        by_name={s['name']:s for s in service.store.sources(pid)}
        for filename in dict.fromkeys(f for c in cases for f in c['files']):
            path=FIX/filename;manifest=public.get(filename) or synthetic[filename]
            if file_digest(path)!=manifest['sha256']:raise ValueError('Fixture changed: '+filename)
            if filename not in by_name:
                staged=runtime/'staging'/(identity()+'.upload');staged.parent.mkdir(exist_ok=True);shutil.copyfile(path,staged)
                metadata=manifest.get('metadata') or {'source_type':'synthetic_fixture','source_group':filename,'note':'人工合成测试资料，不代表真实企业数据'}
                imported=service.import_staged(pid,[{'path':staged,'name':filename,'format':file_format(filename),'bytes':staged.stat().st_size,'sha256':file_digest(path),'metadata':metadata}])
                for job in imported['jobs']:service.process_job(job['id'])
                sid=(imported['sources'] or imported['duplicates'])[0]['id'];by_name[filename]=service.store.get('sources',sid)
            source=by_name[filename]
            report['sources'].append({k:source[k] for k in ('id','name','format','sha256','parser_version','status','error','warnings','page_count','table_count')})
            print('parsed',filename,source['status'],flush=True);dump(args.output,report)
        if args.prepare_only:return
        allsources={s['id']:s for s in by_name.values()}
        for case in cases:
            selected=[by_name[name] for name in case['files']]
            trace_start=len(traces)
            print('running',case['id'],flush=True)
            try:
                item=answers.submit(pid,case['question'],selected[0]['id'],'multi_source',None,str(uuid.uuid4()),source_ids=[s['id'] for s in selected]);deadline=time.monotonic()+900
                while item['status'] in ('queued','running') and time.monotonic()<deadline:
                    time.sleep(.2);item=answers.store.get(pid,item['id'])
                if item['status'] in ('queued','running'):answers.cancel(pid,item['id']);item=answers.store.get(pid,item['id'])
                record={'id':case['id'],'answer':answers.public(item)};record['assessment']=assess(case,record['answer'],allsources)
            except Exception as e:record={'id':case['id'],'error':str(e),'assessment':{'passed':False,'reasons':['exception']}}
            record['model_responses']=traces[trace_start:]
            report['cases'].append(record);dump(args.output,report);print(case['id'],record['assessment'],flush=True)
    finally:answers.close();service.close();dump(args.output,report)

if __name__=='__main__':main()
