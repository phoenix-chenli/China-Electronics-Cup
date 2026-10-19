"""Import orchestration, recoverable work queue, immutable originals."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
from pathlib import Path

from . import parsers
from .store import Store, identity, now, public_source
from .reliability import BoundedExecutor, QueueFull, import_cancelled
from .platform_io import FileLock

logger = logging.getLogger(__name__)


def filename(value):
    name = str(value or "未命名文件").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ord(ch) >= 32 and ch != "\x7f").strip()
    return (name or "未命名文件")[:240]


def file_format(name):
    suffix = Path(name).suffix.lower().lstrip(".")
    return suffix if re.fullmatch(r"[a-z0-9]{1,16}", suffix) else "unknown"


def file_digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class Service:
    def __init__(self, root, samples, workers=True):
        self.root = Path(root).resolve()
        self.samples = Path(samples).resolve()
        self.store = Store(self.root)
        self.workers = workers
        self.pool = None
        self.instance_lock = None
        self.futures = set()
        self.scheduled = set()
        self.future_lock = threading.Lock()
        self.pump_lock = threading.Lock()
        self.closing = threading.Event()

    def start(self):
        if self.instance_lock:
            raise RuntimeError("此服务实例已启动")
        self.closing.clear()
        self.root.mkdir(parents=True, exist_ok=True)
        self.instance_lock = FileLock(self.root / "instance.lock")
        try:
            self.instance_lock.acquire()
        except BlockingIOError as exc:
            self.instance_lock.close()
            self.instance_lock = None
            raise RuntimeError("此运行数据目录已由另一个知衡进程使用") from exc
        try:
            self.store.initialize()
            pending = self.store.recover()
            if self.workers:
                self.pool = BoundedExecutor(max_workers=2, max_pending=16, thread_name_prefix="zhiheng-import")
                for job_id in pending:
                    self.schedule(job_id)
        except Exception:
            self.close()
            raise

    def close(self):
        self.closing.set()
        try:
            if self.pool:
                self.pool.shutdown(wait=True, cancel_futures=True)
                self.pool = None
        finally:
            if self.instance_lock:
                self.instance_lock.close()
                self.instance_lock = None

    def schedule(self, job_id):
        if not self.pool or self.closing.is_set():return False
        with self.future_lock:
            if job_id in self.scheduled:return False
            self.scheduled.add(job_id)
        try:
            future = self.pool.submit(self.process_job, job_id)
            future.import_job_id=job_id
            with self.future_lock:
                self.futures.add(future)
            future.add_done_callback(self._done)
            return True
        except (QueueFull,RuntimeError):
            with self.future_lock:self.scheduled.discard(job_id)
            # This record remains durably queued and is picked up after a slot
            # becomes free or the service restarts.
            return False

    def _pump(self):
        if self.closing.is_set() or not self.pool or not self.pump_lock.acquire(blocking=False):return
        try:
            with self.store.transaction() as c:
                pending=[r[0] for r in c.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at,id LIMIT 36")]
            for job_id in pending:
                if not self.pool.snapshot()['accepting']:break
                self.schedule(job_id)
        finally:self.pump_lock.release()

    def _done(self, future):
        with self.future_lock:
            self.futures.discard(future)
            self.scheduled.discard(getattr(future,'import_job_id',None))
        if future.cancelled():
            return
        if future.exception():
            logger.error("Unexpected import worker failure", exc_info=future.exception())
        self._pump()

    def process_job(self, job_id):
        if self.closing.is_set():
            return
        job = self.store.claim(job_id)
        if not job:
            return
        source = self.store.get("sources", job["source_id"])
        try:
            path = Path(source["original_path"])
            if not path.is_file() or file_digest(path) != source["sha256"]:
                raise parsers.ParseError("保存的原件缺失或摘要变化，请重新导入原文件")
            destination = self.root / "derived" / source["id"] / job_id / "tables.sqlite"
            destination.parent.mkdir(parents=True, exist_ok=True)
            # A recovered job may have written its derived table before the
            # final status transaction. Rebuild only this unfinished job's
            # scratch database; originals and completed job snapshots differ.
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(str(destination) + suffix).unlink(missing_ok=True)
            def progress(stage, value, message):
                if import_cancelled(self,job_id):
                    raise InterruptedError("服务正在退出，保留导入恢复队列")
                self.store.progress(job_id, stage, value, message)
            result = parsers.parse(path, source["format"], destination, progress,
                checkpoint_dir=self.root/'derived'/source['id']/'.page-checkpoints',cancelled=lambda:import_cancelled(self,job_id),
                pdf_batch_limit=16)
            if import_cancelled(self,job_id):
                return
            result["parser_version"] = parsers.PARSER_VERSION
            notes = source["metadata"].get("quality_notes", [])
            for note in ([notes] if isinstance(notes, str) else notes):
                result["warnings"].append("资料审阅提示：" + str(note))
            self.store.finish(job_id, result=result)
        except InterruptedError:
            # Leave the persisted job pending; Service.start reclaims it once.
            if not self.closing.is_set():
                self.store.finish(job_id, error="解析被中断，请重新处理")
        except parsers.ParseError as exc:
            self.store.finish(job_id, error=str(exc))
        except Exception as exc:
            logger.info("Import %s failed (%s)", source["id"], type(exc).__name__)
            self.store.finish(job_id, error=f"无法解析 {source['format'].upper()} 文件，文件可能损坏、加密或结构不受支持。请确认原文件可正常打开，重新导出后再试")

    def import_staged(self, project_id, staged):
        sources, jobs, duplicates = [], [], []
        try:
            for item in staged:
                name, fmt = item["name"], item["format"]
                source = dict(id=identity(), project_id=project_id, name=name, kind=parsers.KINDS.get(fmt, "document"), format=fmt, bytes=item["bytes"], sha256=item["sha256"], status="queued", stage="等待处理", progress=0, summary="原件已保存，等待解析", error=None, page_count=0, table_count=0, row_count=0, created_at=now(), updated_at=now(), parser_version=parsers.PARSER_VERSION, archived=0)
                saved, job, duplicate = self.store.add_source(source, item["path"], item.get("metadata", {}))
                if duplicate:
                    duplicates.append(public_source(duplicate))
                else:
                    sources.append(public_source(saved))
                    if job:
                        jobs.append(job)
            for job in jobs:
                self.schedule(job["id"])
            return dict(sources=sources, jobs=jobs, duplicates=duplicates)
        finally:
            for item in staged:
                item["path"].unlink(missing_ok=True)

    def demo(self, project_id):
        manifest_path = self.samples / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError("真实示例资料尚未安装，请先上传自己的资料")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise ValueError("示例资料清单不可读取") from exc
        if not isinstance(manifest, list) or len(manifest) > 20:
            raise ValueError("示例资料清单格式有误")
        staged = []
        try:
            for entry in manifest:
                path = (self.samples / entry["file"]).resolve()
                if not path.is_relative_to(self.samples) or not path.is_file():
                    raise ValueError("示例清单包含不存在或超出样本目录的文件")
                if path.stat().st_size > parsers.MAX_BYTES:
                    raise ValueError("示例文件超过 80 MB 导入上限")
                target = self.root / "incoming" / identity()
                shutil.copyfile(path, target)
                metadata = {key: value for key, value in entry.items() if key not in {"file", "name"}}
                metadata["bundled_sample"] = True
                staged.append(dict(path=target, name=filename(entry.get("name") or path.name), format=file_format(path.name), bytes=path.stat().st_size, sha256=file_digest(target), metadata=metadata))
            return self.import_staged(project_id, staged)
        finally:
            for item in staged:
                item["path"].unlink(missing_ok=True)
