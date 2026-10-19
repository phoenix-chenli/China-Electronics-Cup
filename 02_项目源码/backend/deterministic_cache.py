"""Instance-local bounded mechanical result cache, never a fact/answer cache.

No active task scope => original execution. A hit rechecks current source scope,
original bytes and imported SQLite bytes. Model review and provenance validation
are not cached. ContextVar isolates concurrent worker tasks and finally resets.
"""
from __future__ import annotations
import copy
import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from .source_snapshot import snapshot

_ACTIVE = ContextVar('zhiheng_deterministic_cache', default=None)
CACHE_POLICY = 'mechanical-v1-original-and-database-byte-digest'


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False, default=lambda v: {'decimal': str(v)} if isinstance(v, Decimal) else _unsupported(v))


def _unsupported(value):
    raise TypeError(type(value).__name__)


def _digest(path, cancelled):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while True:
            if cancelled():
                raise InterruptedError('已取消缓存来源检查')
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


@dataclass
class TaskScope:
    cache: object
    project_id: str
    sources: dict
    cancelled: object
    is_current: object = None

    def set_current_check(self, callback):
        self.is_current = callback

    def check(self, cancelled):
        if cancelled() or self.cancelled():
            raise InterruptedError('任务已取消')
        if self.is_current is None:
            return False
        try:
            if self.is_current() is not True:
                raise ValueError('资料版本已变化，缓存结果未使用')
        except ValueError:
            for identifier in self.sources:
                self.cache.invalidate_source(self.project_id, identifier)
            raise
        if cancelled() or self.cancelled():
            raise InterruptedError('任务已取消')
        return True

    def fingerprints(self, source_ids, cancelled):
        if not self.check(cancelled):
            return None
        records = []
        for identifier in sorted(set(source_ids)):
            source = self.sources.get(identifier)
            if not source or source.get('project_id') != self.project_id:
                return None
            expected = source.get('sha256')
            original = source.get('original_path')
            if not isinstance(expected, str) or not re.fullmatch('[0-9a-fA-F]{64}', expected) or not original:
                return None
            path = Path(original)
            stopped = lambda: cancelled() or self.cancelled()
            if not path.is_file() or _digest(path, stopped) != expected.lower():
                self.cache.invalidate_source(self.project_id, identifier)
                raise ValueError('原件摘要发生变化，缓存结果未使用')
            data = source.get('data_path')
            data_hash = None
            if data:
                database = Path(data)
                if not database.is_file():
                    self.cache.invalidate_source(self.project_id, identifier)
                    raise ValueError('数据库副本不存在，缓存结果未使用')
                # A live WAL is not a stable imported single-file snapshot.
                if Path(str(database) + '-wal').exists():
                    return None
                data_hash = _digest(database, stopped)
            records.append({'scope': snapshot(source), 'data_sha256': data_hash})
        return records


class DeterministicCache:
    def __init__(self, max_entries=64, max_bytes=16 * 1024 * 1024, max_entry_bytes=4 * 1024 * 1024):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_entry_bytes = min(max_entry_bytes, max_bytes)
        self.entries = OrderedDict()
        self.versions = OrderedDict()
        self.bytes = 0
        self.lock = threading.RLock()

    @contextmanager
    def scope(self, project_id, sources, cancelled=lambda: False, is_current=None):
        if not isinstance(project_id, str) or not project_id:
            raise ValueError('缓存必须限定项目')
        current = TaskScope(self, project_id, {s['id']: s for s in sources}, cancelled, is_current)
        token = _ACTIVE.set(current)
        try:
            yield current
        finally:
            _ACTIVE.reset(token)

    def invalidate_source(self, project_id, source_id):
        with self.lock:
            self.versions.pop((project_id, source_id), None)
            for key, entry in list(self.entries.items()):
                if entry['project'] == project_id and source_id in entry['sources']:
                    self.bytes -= entry['bytes']
                    del self.entries[key]

    def clear(self):
        with self.lock:
            self.entries.clear()
            self.versions.clear()
            self.bytes = 0

    def observe_sources(self, project, records):
        with self.lock:
            for record in records:
                key = (project, record['scope']['id'])
                version = hashlib.sha256(_canonical(record).encode()).hexdigest()
                if key in self.versions and self.versions[key] != version:
                    self.invalidate_source(*key)
                self.versions[key] = version
                self.versions.move_to_end(key)
            while len(self.versions) > max(1, self.max_entries * 4):
                self.versions.popitem(last=False)

    def get(self, key):
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                return None
            self.entries.move_to_end(key)
            return copy.deepcopy(entry['value'])

    def put(self, key, result, project, sources):
        try:
            size = len(_canonical(result).encode())
        except (TypeError, ValueError):
            return False
        if size > self.max_entry_bytes or self.max_entries < 1:
            return False
        with self.lock:
            previous = self.entries.pop(key, None)
            if previous:
                self.bytes -= previous['bytes']
            self.entries[key] = {'value': copy.deepcopy(result), 'bytes': size, 'project': project, 'sources': tuple(sources)}
            self.bytes += size
            while len(self.entries) > self.max_entries or self.bytes > self.max_bytes:
                _, entry = self.entries.popitem(last=False)
                self.bytes -= entry['bytes']
        return True


def mechanical_call(kind, payload, compute, *, source_ids=None, cancelled=lambda: False, cacheable=True):
    """Return (mechanical value, audit); exceptions and model results never stored."""
    started = time.monotonic()
    scope = _ACTIVE.get()
    if cancelled():
        raise InterruptedError('任务已取消')
    if scope is None or not cacheable:
        return compute(), {'status': 'bypass', 'reason': 'no_task_scope' if scope is None else 'non_deterministic_or_unsupported'}
    ids = list(scope.sources) if source_ids is None else list(source_ids)
    before = scope.fingerprints(ids, cancelled)
    if not before:
        return compute(), {'status': 'bypass', 'reason': 'current_source_guard_or_stable_files_unavailable'}
    scope.cache.observe_sources(scope.project_id, before)
    try:
        material = _canonical({'policy': CACHE_POLICY, 'kind': kind, 'project_id': scope.project_id,
                               'sources': before, 'payload': payload})
    except (TypeError, ValueError):
        return compute(), {'status': 'bypass', 'reason': 'uncacheable_payload'}
    if len(material) > 2 * 1024 * 1024:
        return compute(), {'status': 'bypass', 'reason': 'key_budget'}
    key = hashlib.sha256(material.encode()).hexdigest()
    result = scope.cache.get(key)
    hit = result is not None
    if not hit:
        result = compute()  # Exceptions are deliberately not memoized.
    after = scope.fingerprints(ids, cancelled)
    if before != after:
        for identifier in ids:
            scope.cache.invalidate_source(scope.project_id, identifier)
        raise ValueError('执行期间资料或数据库副本发生变化，缓存结果未发布')
    stored = False if hit else scope.cache.put(key, result, scope.project_id, ids)
    return copy.deepcopy(result), {'status': 'hit' if hit else ('miss_stored' if stored else 'miss_not_stored'),
        'kind': kind, 'policy': CACHE_POLICY, 'source_checks': 'current_scope_and_actual_bytes_before_and_after',
        'elapsed_ms': round((time.monotonic()-started)*1000, 3), 'fact_verification_cached': False}


def sql_source_in_scope(source):
    scope = _ACTIVE.get()
    if scope is None:
        return False
    original = scope.sources.get(source.get('id'))
    return bool(original and all(source.get(key) == original.get(key) for key in
        ('id', 'project_id', 'sha256', 'parser_version', 'original_path', 'data_path', 'semantic_revision')))
