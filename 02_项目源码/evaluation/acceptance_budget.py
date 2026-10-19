"""Durable, cross-process acceptance budget. No network or secrets are stored."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import threading
import uuid


class BudgetExceeded(RuntimeError):
    pass


class AcceptanceBudget:
    """Reserve before starting an answer; all internal requests share its lease.

    Journal entries are append-only. A reservation left by a crashed process
    continues occupying capacity until inspected, never silently reset.
    """
    _thread_lock = threading.RLock()

    def __init__(self, path, limit=45):
        if type(limit) is not int or not 1 <= limit <= 45:
            raise ValueError('本任务轮数上限必须为1至45')
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.limit = limit
        with self._locked() as handle:
            events = self._read(handle)
            if not events:
                self._append(handle, {'type': 'budget_created', 'limit': limit})
            elif events[0].get('type') != 'budget_created' or events[0].get('limit') != limit:
                raise ValueError('现有账本的任务上限不匹配，禁止重置')

    @contextmanager
    def _locked(self):
        with self._thread_lock:
            with self.path.open('a+', encoding='utf-8') as handle:
                if os.name == 'nt':
                    import msvcrt
                    lock_path = self.path.with_suffix(self.path.suffix + '.lock')
                    with lock_path.open('a+b') as lock:
                        lock.seek(0)
                        if not lock.read(1): lock.write(b'0'); lock.flush()
                        lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                        try: yield handle
                        finally:
                            lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    try: yield handle
                    finally: fcntl.flock(handle, fcntl.LOCK_UN)

    @staticmethod
    def _read(handle):
        handle.seek(0)
        try:
            return [json.loads(line) for line in handle if line.strip()]
        except json.JSONDecodeError as exc:
            raise RuntimeError('账本包含未完成记录，需核实后恢复；禁止忽略或重置') from exc

    @staticmethod
    def _append(handle, event):
        handle.seek(0, os.SEEK_END)
        event = {'at': datetime.now(timezone.utc).isoformat(), **event}
        handle.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n')
        handle.flush(); os.fsync(handle.fileno())

    @staticmethod
    def _state(events):
        attempts = {}
        for event in events:
            aid = event.get('attempt_id')
            if event['type'] == 'reserved':
                if aid in attempts: raise RuntimeError('账本预留标识重复')
                attempts[aid] = {**event, 'started': False, 'closed': False, 'requests': 0}
            elif aid in attempts:
                item = attempts[aid]
                if event['type'] == 'post_started':
                    item['started'] = True; item['requests'] += 1
                elif event['type'] == 'finished':
                    item['closed'] = True; item['outcome'] = event['outcome']
        return attempts

    @staticmethod
    def _occupied(attempts):
        return sum(v['started'] or not v['closed'] for v in attempts.values())

    @staticmethod
    def _effective_limit(events):
        limit = events[0]['limit']
        for event in events:
            if event['type'] == 'limit_removed_by_user':
                if not event.get('authorization'):
                    raise RuntimeError('预算变更缺少用户授权记录')
                limit = None
        return limit

    def remove_limit_by_user(self, authorization):
        """Explicit user amendment; preserve all past reservations and usage."""
        if not isinstance(authorization, str) or not authorization.strip():
            raise ValueError('取消上限必须保留用户明确授权')
        with self._locked() as handle:
            events = self._read(handle)
            old = self._effective_limit(events)
            if old is not None:
                self._append(handle, {'type': 'limit_removed_by_user', 'previous_limit': old,
                                      'authorization': authorization.strip()})

    def reserve(self, case_id, *, code_sha256, config_sha256, parent_answer_id=None):
        if not isinstance(case_id, str) or not case_id or len(case_id) > 160:
            raise ValueError('测试问题标识无效')
        with self._locked() as handle:
            events = self._read(handle)
            attempts = self._state(events)
            limit = self._effective_limit(events)
            if limit is not None and self._occupied(attempts) >= limit:
                raise BudgetExceeded('已达到本任务授权轮数上限，禁止开始新问答或独立探测')
            aid = uuid.uuid4().hex
            self._append(handle, {'type': 'reserved', 'attempt_id': aid, 'case_id': case_id,
                                 'code_sha256': code_sha256, 'config_sha256': config_sha256,
                                 'parent_answer_id': parent_answer_id})
            return aid

    def observe(self, attempt_id, event_type, request):
        if event_type not in {'post_started', 'request_finished'}:
            raise ValueError('未知调用审计类型')
        # Only ModelGateway's metadata is accepted, never prompts or headers.
        allowed = {'purpose', 'requested_model', 'reported_model', 'status', 'post_started',
                   'started_at', 'finished_at', 'elapsed_ms', 'request_elapsed_ms',
                   'queue_wait_ms', 'cooldown_wait_ms', 'http_status', 'usage',
                   'request_settings', 'retry_policy', 'timeout_kind', 'effective_timeout_seconds'}
        allowed.update({'task_stage', 'input_modalities', 'image_count'})
        clean = {k: v for k, v in request.items() if k in allowed}
        with self._locked() as handle:
            attempts = self._state(self._read(handle)); current = attempts.get(attempt_id)
            if not current or current['closed']:
                raise BudgetExceeded('真实请求没有有效的问答预留，已阻止发送')
            self._append(handle, {'type': event_type, 'attempt_id': attempt_id, 'request': clean})

    def finish(self, attempt_id, outcome):
        with self._locked() as handle:
            attempts = self._state(self._read(handle)); current = attempts.get(attempt_id)
            if not current: raise ValueError('测试预留不存在')
            if current['closed']: return
            self._append(handle, {'type': 'finished', 'attempt_id': attempt_id,
                                 'outcome': str(outcome)[:80], 'released_unused': not current['started']})

    def snapshot(self):
        with self._locked() as handle:
            events = self._read(handle); attempts = self._state(events)
        occupied = self._occupied(attempts)
        requests = [e for e in events if e['type'] == 'request_finished']
        tokens = {}
        for entry in requests:
            for key, value in entry['request'].get('usage', {}).items():
                if type(value) is int: tokens[key] = tokens.get(key, 0) + value
        limit = self._effective_limit(events)
        return {'limit': limit, 'used_rounds': sum(v['started'] for v in attempts.values()),
                'pending_reservations': sum(not v['started'] and not v['closed'] for v in attempts.values()),
                'occupied_rounds': occupied, 'remaining_rounds': None if limit is None else limit - occupied,
                'post_started': sum(v['requests'] for v in attempts.values()),
                'requests_finished': len(requests), 'usage_reported': tokens,
                'attempts': list(attempts.values())}
