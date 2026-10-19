"""Local library retrieval jobs; separate from model-backed answering."""
from __future__ import annotations
import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from pathlib import Path
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from .retrieval.library_search import search_library
from .retrieval.service import StaleIndexError
from .source_snapshot import snapshot
from .source_guard import StoreSourceGuard
from .service import file_digest


class LibrarySourceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=80)
    revision: str = Field(min_length=64, max_length=64)
    sha256: str = Field(min_length=64, max_length=64)
    parser_version: str = Field(min_length=1, max_length=120)


class LibrarySearchInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    question: str = Field(min_length=1, max_length=2000)
    sources: list[LibrarySourceInput] = Field(min_length=1, max_length=128)
    limit: int = Field(default=10, ge=1, le=30, strict=True)

    @field_validator('question')
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError('请输入检索内容')
        return value.strip()

    @field_validator('sources')
    @classmethod
    def unique(cls, value):
        if len({v.id for v in value}) != len(value): raise ValueError('所选资料不能重复')
        return value


class LibrarySearchJobs:
    def __init__(self, service, answers):
        self.service, self.answers = service, answers
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='zhiheng-library')
        self.jobs = {}
        self.closed = False

    def _sources(self, project_id, records, *, check_bytes=False):
        if not self.service.store.get('projects', project_id):
            raise LookupError('项目不存在')
        sources = []
        for record in records:
            source = self.service.store.get('sources', record['id'])
            if not source or source['project_id'] != project_id:
                raise LookupError('资料不存在或不属于当前项目')
            if source['archived'] or source['status'] not in ('ready', 'attention'):
                raise StaleIndexError('所选资料已归档或尚未完成解析，请重新选择')
            if not any(p.get('text', '').strip() for p in source.get('pages', [])):
                raise ValueError('所选资料没有可检索正文，请选择文档或已识别图片')
            current = snapshot(source)
            if any(current[k] != record[k] for k in ('revision', 'sha256', 'parser_version')):
                raise StaleIndexError('资料或解析版本已改变，请刷新资料列表后重新检索')
            if check_bytes and file_digest(Path(source['original_path'])) != record['sha256']:
                raise StaleIndexError('原件内容已变化，本次结果失效')
            sources.append(source)
        return sources

    def submit(self, project_id, body):
        records = [v.model_dump() for v in body.sources]
        sources = self._sources(project_id, records)
        with self.lock:
            if self.closed: raise ValueError('检索服务正在关闭')
            active = [j for j in self.jobs.values() if j['status'] in ('queued', 'running')]
            if any(j['project_id'] == project_id for j in active):
                raise ValueError('当前项目已有检索任务，请等待或取消后再试')
            if len(active) >= 4: raise ValueError('本地检索队列已满，请稍后重试')
            # Keep a bounded, session-local result history; no answer history is changed.
            for old in sorted(self.jobs.values(), key=lambda j:j['created_at']):
                if len(self.jobs) < 40: break
                if old['status'] not in ('queued', 'running'): self.jobs.pop(old['id'])
            identifier = uuid4().hex
            job = dict(id=identifier, project_id=project_id, status='queued', stage='等待本地检索',
                       completed_sources=0, total_sources=len(sources), created_at=time.time(),
                       question=body.question, records=records, result=None, error=None,
                       event=threading.Event(), elapsed_ms=0)
            self.jobs[identifier] = job
            self.pool.submit(self._run, job, sources, body.limit)
            return self._public(job)

    def _public(self, job):
        return copy.deepcopy({k:v for k,v in job.items() if k not in ('event','records')})

    def _run(self, job, sources, limit):
        start = time.monotonic()
        event = job['event']
        guards = {}
        try:
            with self.lock:
                if event.is_set(): return
                job.update(status='running',stage='检查资料版本')
            self._sources(job['project_id'], job['records'], check_bytes=True)
            for source in sources:
                guards[source['id']] = StoreSourceGuard(self.service.store, source)
            retrieval = self.answers.retrieval_service()
            handles = []
            for i,source in enumerate(sources):
                if event.is_set(): raise InterruptedError()
                with self.lock: job.update(stage=f'准备本地索引 {i+1}/{len(sources)}：{source["name"]}',completed_sources=i)
                handles.append(retrieval.prepare(source,job['project_id'],event.is_set,is_current=guards[source['id']]))
                with self.lock: job['completed_sources'] = i+1
            with self.lock: job['stage'] = '检索已选资料的原文片段'
            result = search_library(handles,job['question'],limit=limit,mode='hybrid')
            self._sources(job['project_id'],job['records'],check_bytes=True)
            by_id={s['id']:s for s in sources}
            for row in result['results']:
                row['parser_version']=by_id[row['source_id']]['parser_version']
                row['source_revision']=snapshot(by_id[row['source_id']])['revision']
            with self.lock:
                if event.is_set(): raise InterruptedError()
                job.update(status='completed',stage='检索完成',result=result)
        except InterruptedError:
            with self.lock: job.update(status='cancelled',stage='已取消',result=None)
        except (LookupError,ValueError,StaleIndexError) as exc:
            with self.lock:
                if not event.is_set(): job.update(status='failed',stage='检索未完成',error=str(exc),result=None)
        except Exception:
            with self.lock:
                if not event.is_set(): job.update(status='failed',stage='检索未完成',error='本地索引未能完成，请检查离线依赖、原件或运行诊断后重试。',result=None)
        finally:
            for guard in guards.values():
                guard.close()
            with self.lock: job['elapsed_ms']=round((time.monotonic()-start)*1000,2)

    def get(self, project_id, identifier):
        with self.lock:
            job=self.jobs.get(identifier)
            if not job or job['project_id'] != project_id: raise LookupError('检索任务不存在或不属于当前项目')
            if job['status']=='completed':
                try: self._sources(project_id,job['records'],check_bytes=True)
                except (LookupError,ValueError,StaleIndexError,OSError):
                    job.update(status='failed',stage='结果已失效',error='资料或原件已变化，请重新检索。',result=None)
            return self._public(job)

    def cancel(self, project_id, identifier):
        with self.lock:
            job=self.jobs.get(identifier)
            if not job or job['project_id'] != project_id: raise LookupError('检索任务不存在或不属于当前项目')
            if job['status'] in ('queued','running'):
                job['event'].set(); job.update(status='cancelled',stage='已取消',result=None)
            return self._public(job)

    def close(self):
        with self.lock:
            self.closed=True
            for job in self.jobs.values(): job['event'].set()
        self.pool.shutdown(wait=False,cancel_futures=True)


def register_library_routes(app, jobs):
    def call(fn,*args):
        try:return fn(*args)
        except LookupError as exc:raise HTTPException(404,str(exc)) from exc
        except (ValueError,StaleIndexError) as exc:raise HTTPException(409,str(exc)) from exc
    @app.post('/api/projects/{project_id}/library-search',status_code=202)
    def submit(project_id:str,body:LibrarySearchInput):return call(jobs.submit,project_id,body)
    @app.get('/api/projects/{project_id}/library-search/{identifier}')
    def get(project_id:str,identifier:str):return call(jobs.get,project_id,identifier)
    @app.post('/api/projects/{project_id}/library-search/{identifier}/cancel')
    def cancel(project_id:str,identifier:str):return call(jobs.cancel,project_id,identifier)
