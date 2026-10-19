"""Local reliability primitives and public diagnostics; never replay a model POST."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import importlib.metadata
import json
import math
import os
import platform
import re
import subprocess
import signal
import threading
import time
from pathlib import Path


class QueueFull(RuntimeError):
    pass


def run_cancellable(args, *, timeout, cancelled=lambda: False, **kwargs):
    """Bounded local child; cancellation kills only the child we started."""
    if cancelled(): raise InterruptedError('已取消本地处理')
    deadline=time.monotonic()+timeout
    with subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,**kwargs) as process:
        try:
            while True:
                if cancelled(): raise InterruptedError('已取消本地处理')
                remaining=deadline-time.monotonic()
                if remaining<=0:raise subprocess.TimeoutExpired(args,timeout)
                try:
                    out,err=process.communicate(timeout=min(.1,remaining))
                    if cancelled():raise InterruptedError('已取消本地处理')
                    return subprocess.CompletedProcess(args,process.returncode,out,err)
                except subprocess.TimeoutExpired:
                    if time.monotonic()>=deadline:raise
        except BaseException:
            try:
                if os.name=='posix':os.killpg(process.pid,signal.SIGKILL)
                else:process.kill()
            except ProcessLookupError:pass
            process.communicate()
            raise


class BoundedExecutor:
    """Bound running + pending work. Callers retain durable queued records."""
    def __init__(self, max_workers=2, max_pending=16, thread_name_prefix='zhiheng'):
        if not 1 <= max_workers <= 16 or not 0 <= max_pending <= 256: raise ValueError('invalid queue limits')
        self.pool=ThreadPoolExecutor(max_workers=max_workers,thread_name_prefix=thread_name_prefix)
        self.slots=threading.BoundedSemaphore(max_workers+max_pending)
        self.capacity=max_workers+max_pending; self.active=0; self.closed=False; self.lock=threading.Lock()
    def submit(self, function, *args, **kwargs):
        with self.lock:
            if self.closed: raise RuntimeError('executor closed')
            if not self.slots.acquire(blocking=False): raise QueueFull('处理队列已满，任务保持等待；请稍后重试')
            self.active+=1
        try: future=self.pool.submit(function,*args,**kwargs)
        except BaseException:
            self._release(None); raise
        future.add_done_callback(self._release)
        return future
    def _release(self, future):
        with self.lock: self.active-=1
        self.slots.release()
    def snapshot(self):
        with self.lock: return {'capacity':self.capacity,'running_or_pending':self.active,'accepting':not self.closed and self.active<self.capacity}
    def shutdown(self, wait=True, cancel_futures=True):
        with self.lock:self.closed=True
        self.pool.shutdown(wait=wait,cancel_futures=cancel_futures)


class CircuitBreaker:
    """Per service/provider scope, blocking new calls after transport failures.

    No retry inside. One caller may probe after cooldown; a probe is a new
    explicitly submitted request, never an unknown previous POST.
    """
    def __init__(self, threshold=3, cooldown=30, clock=time.monotonic):
        self.threshold=threshold;self.cooldown=cooldown;self.clock=clock
        self.failures=0;self.until=0.;self.probing=False;self.lock=threading.Lock()
    def before(self):
        from .model_gateway import ModelError
        with self.lock:
            if self.until:
                if self.clock()<self.until or self.probing:
                    raise ModelError('模型连接短期不可用，请稍后发起新任务；未重复提交旧请求','circuit_open')
                self.probing=True
    def after(self, code=None):
        with self.lock:
            self.probing=False
            if code in {'cancelled','circuit_open','config_failed','failed','pending'}:
                return
            if code in {'http_401','http_402','http_403'}:
                self.failures += 1
                self.until = self.clock() + max(self.cooldown, 300)
            elif code in {'transport_failed','connect_timeout','read_timeout','timeout','http_429','http_500','http_502','http_503','http_504'}:
                self.failures+=1
                if self.failures>=self.threshold:self.until=self.clock()+self.cooldown
            else:
                self.failures=0;self.until=0.
    def snapshot(self):
        with self.lock:return {'state':'open' if self.until>self.clock() else 'half_open' if self.until else 'closed',
                               'consecutive_transport_failures':self.failures,'retry_after_seconds':max(0,round(self.until-self.clock(),1)),
                               'automatic_post_replay':False}


class GatewayCircuits:
    """Owned by one AnswerService, keyed by project + endpoint + selected model."""
    def __init__(self, max_scopes=512, **breaker_options):
        self.lock=threading.Lock();self.scopes={};self.max_scopes=max_scopes;self.options=breaker_options
    def get(self, project_id, endpoint, model):
        if not project_id:raise ValueError('circuit project scope required')
        key=(str(project_id),str(endpoint),str(model))
        with self.lock:
            if key not in self.scopes:
                if len(self.scopes)>=self.max_scopes:
                    # Evict only closed breakers; never reset an open failure scope.
                    victim=next((k for k,v in self.scopes.items() if v.snapshot()['state']=='closed'),None)
                    if victim is None:
                        from .model_gateway import ModelError
                        raise ModelError('模型故障作用域已达保护上限，请稍后再试','circuit_open')
                    del self.scopes[victim]
                self.scopes[key]=CircuitBreaker(**self.options)
            return self.scopes[key]


def retry_policy(status=None, retry_after=None, *, post_started=False, error_code=None, wall_clock=None):
    delay=None
    try:
        value=float(retry_after)
        if math.isfinite(value) and value>=0:delay=min(86400,value)
    except (ValueError,TypeError):
        try:
            target=parsedate_to_datetime(str(retry_after))
            if target.tzinfo is None:target=target.replace(tzinfo=timezone.utc)
            delay=max(0,min(86400,(target-(wall_clock or datetime.now(timezone.utc))).total_seconds()))
        except (ValueError,TypeError,OverflowError):pass
    category='authentication_or_billing' if status in (401,402,403) else 'rate_limited' if status==429 else 'temporarily_unavailable' if status in (500,502,503,504) else 'protocol' if error_code in {'schema_failed','content_json_failed','response_json_failed'} else 'transport' if error_code in {'timeout','connect_timeout','read_timeout','transport_failed'} else 'other'
    return {'category':category,'http_status':status,'retry_after_seconds':delay,'post_started':bool(post_started),
            'automatic_retry':False,'strategy':'explicit_new_request_only' if post_started else 'caller_may_resubmit',
            'reason':'已发出的POST结果可能未知，不自动重放；完整格式响应的协议修复由上层单独记录。'}


def locked_dependencies(lock_path):
    rows=[]
    for raw in Path(lock_path).read_text().splitlines():
        line=raw.strip()
        if not line or line.startswith('#'):continue
        match=re.fullmatch(r'([A-Za-z0-9_.-]+)==([^;\s]+)',line)
        if not match:raise ValueError('依赖锁必须为精确 name==version')
        name,expected=match.groups()
        try:actual=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:actual=None
        rows.append({'name':name,'expected':expected,'actual':actual,'matches':actual==expected})
    return {'ok':all(x['matches'] for x in rows),'packages':rows}


_PROCESSING_ACTIONS = {'native_only', 'blank_no_ocr', 'ocr_primary', 'native_with_ocr_comparison',
    'manual_source_check', 'targeted_high_resolution_review', 'native_extraction', 'retain_ocr_and_verify_selected_evidence'}
_PROCESSING_STATUSES = {'planned', 'executed', 'skipped_ocr_page_limit', 'ocr_failed',
    'ocr_primary_empty', 'comparison_empty_native_retained'}
_PROCESSING_REASONS = {
    'measured_near_uniform_page':'实际测量为近空白页',
    'no_native_text_nonblank_page':'非空白页没有原生正文',
    'marginal_native_over_page_image':'满页图像仅伴有边缘页码或文字',
    'isolated_header_over_page_image':'满页图像仅伴有顶部标题',
    'explicit_watermark_over_page_image':'满页图像仅伴有明确水印',
    'substantial_embedded_image':'含较大嵌入图像',
    'image_coverage_uncertain_or_sparse_native':'图像覆盖范围不明或原生文字稀少',
    'measured_two_column_candidate':'实际布局存在分栏候选',
    'native_table_relationship_check':'原生表格关系需对照',
    'native_formula_structure_check':'公式结构需对照',
    'measured_low_contrast':'测得低对比度', 'measured_low_sharpness':'测得低清晰度',
    'native_text_without_observed_ocr_trigger':'有原生正文且未观察到补充OCR触发条件',
    'blank_or_near_uniform':'近空白页', 'low_contrast':'低对比度', 'possible_blur':'可能模糊',
    'orientation_corrected':'已纠正方向', 'skew_detected':'检测到倾斜',
    'ocr_uncertain':'文字识别存在不确定性', 'preprocessing_disagreement':'识别或预处理结果存在差异',
    'unreadable':'尚未取得可读正文', 'native':'原生文字', 'ocr':'本机OCR文字', 'unknown':'未确定',
}


def public_processing_plan(raw):
    """Small typed projection; free-form metadata/path/credentials never pass."""
    if not isinstance(raw,list):return [],False
    rows=[]
    for item in raw[:500]:
        if not isinstance(item,dict) or type(item.get('page')) is not int or item['page']<1:continue
        row={'page':item['page']}
        for key in ('requested_action','suggested_action','suggested_followup'):
            if key in item:row[key]=item[key] if isinstance(item[key],str) and item[key] in _PROCESSING_ACTIONS else 'unknown'
        if 'status' in item:row['status']=item['status'] if isinstance(item['status'],str) and item['status'] in _PROCESSING_STATUSES else 'unknown'
        if 'selected_extraction' in item:
            value=item['selected_extraction'];row['selected_extraction']=value if isinstance(value,str) and value in {'native','ocr','unreadable'} else 'unknown'
        for key in ('executed','followup_executed'):
            if type(item.get(key)) is bool:row[key]=item[key]
        value=item.get('elapsed_ms')
        if type(value) in (int,float) and 0<=value<=86_400_000 and math.isfinite(value):row['elapsed_ms']=value
        if item.get('verification_status')=='not_verified':row['verification_status']='not_verified'
        basis=item.get('basis') if isinstance(item.get('basis'),list) else []
        row['basis']=[v for v in basis[:32] if isinstance(v,str) and v in _PROCESSING_REASONS]
        row['basis_labels']=[_PROCESSING_REASONS[v] for v in row['basis']]
        row['basis_omitted']=len(basis)-len(row['basis'])
        rows.append(row)
    return rows,len(raw)>500


def source_diagnostics(source):
    from .parsers import PARSER_VERSION
    meta=source.get('metadata',{});batch=meta.get('import_batches',{})
    quality=meta.get('document_profiles',{}).get('quality',{})
    processing_plan,processing_plan_truncated=public_processing_plan(meta.get('document_profiles',{}).get('processing_plan'))
    # No original/data path, model endpoint, prompts or credentials are public.
    return {'source_id':source['id'],'status':source['status'],'parser_version':source.get('parser_version'),
        'error':source.get('error'),'warnings':source.get('warnings',[])[:100],
        'page_count':source.get('page_count',0),'parsed_page_count':len(source.get('pages',[])),
        'batch':{k:batch[k] for k in ('status','completed_pages','pending_pages','reused_pages','resumable','out_of_policy_pages','model_requests_replayed') if k in batch},
        'quality':quality,'original_retained':Path(source.get('original_path','')).is_file(),
        'processing_plan':processing_plan,'processing_plan_truncated':processing_plan_truncated,
        'retry_available':not source.get('archived') and (source['status'] in {'failed','cancelled','attention'} or
            source['status']=='ready' and source.get('parser_version')!=PARSER_VERSION),
        'normalization':{'candidate_count':len(meta.get('normalization_candidates',{}).get('items',[])),'source_modified':False},
        'continuation_links':meta.get('table_continuations',{}),
        'note':'质量诊断不是答案正确率；重试只恢复原件解析，不会重发历史模型请求。'}


def cancel_import(service, source_id, project_id):
    from .store import now
    with service.store.transaction() as c:
        source=c.execute('SELECT id,status FROM sources WHERE id=? AND project_id=?',(source_id,project_id)).fetchone()
        if not source:raise KeyError(source_id)
        if source['status'] not in {'queued','processing'}:return False
        c.execute("UPDATE jobs SET status='cancelled',stage='已取消',message='用户取消，已完成页可在明确重试时恢复',updated_at=? WHERE source_id=? AND status IN ('queued','processing')",(now(),source_id))
        c.execute("UPDATE sources SET status='cancelled',stage='已取消',summary='已取消；原件和已完成页保留，重启不会自动恢复',updated_at=? WHERE id=?",(now(),source_id))
    return True


def import_cancelled(service, job_id):
    job=service.store.get('jobs',job_id)
    return service.closing.is_set() or not job or job['status']=='cancelled'


def retry_import(service, source_id, project_id):
    source=service.store.get('sources',source_id)
    if not source or source['project_id']!=project_id:raise KeyError(source_id)
    saved,job=service.store.retry(source_id)
    service.schedule(job['id'])
    return {'source':__import__('backend.store',fromlist=['public_source']).public_source(saved),'job':job}


def runtime_diagnostics(service, answers=None):
    from .ocr_adapter import capabilities
    with service.store.transaction() as c:
        counts={r[0]:r[1] for r in c.execute('SELECT status,COUNT(*) FROM jobs GROUP BY status')}
    result={'platform':platform.system(),'python':platform.python_version(),'imports':counts,'ocr':capabilities(),
            'automatic_model_replay_on_restart':False,'closing':service.closing.is_set()}
    if hasattr(service.pool,'snapshot'):result['queue']=service.pool.snapshot()
    return result
