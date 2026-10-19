"""Cancellable provider admission; never retries or retains request bodies.

One in-flight request per origin/credential, shared across projects/models.
By default locks live in this process. An explicit private coordination folder
enables OS advisory locks across local processes, including fresh event loops.
Locks are released by the OS after process exit; lock files must not be removed.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from .platform_io import ensure_private_directory, secure_open, try_lock_fd, unlock_fd

DIRECTORY_ENV = 'ZHIHENG_PROVIDER_PACING_DIR'
QUEUE_TIMEOUT_ENV = 'ZHIHENG_PROVIDER_QUEUE_TIMEOUT_SECONDS'
DEFAULT_QUEUE_TIMEOUT = 600
DEFAULT_COOLDOWN = 30
MAX_COOLDOWN = 86400
POLL_SECONDS = .05
_registry_lock = threading.Lock()
_registry = {}


class PacingError(ValueError):
    def __init__(self, message, code='pacing_failed'):
        super().__init__(message); self.code = code


def _scope(config):
    try:
        if not isinstance(config.get('endpoint'), str) or not isinstance(config.get('api_key', ''), str):raise ValueError
        parts = urlsplit(config['endpoint'])
        if not parts.hostname or parts.scheme.lower() not in {'https','http'}:raise ValueError
        port = parts.port or (443 if parts.scheme.lower() == 'https' else 80)
    except (ValueError, TypeError):
        raise PacingError('模型提供商排队作用域配置无效', 'config_failed') from None
    # Paths and model names do not split a provider credential's shared quota.
    origin = (parts.scheme.lower(), (parts.hostname or '').lower(), port)
    value = json.dumps([origin, config.get('api_key', '')], separators=(',', ':'))
    return hashlib.sha256(value.encode()).hexdigest()


def _queue_timeout(config):
    raw = config.get('queue_timeout', os.environ.get(QUEUE_TIMEOUT_ENV, DEFAULT_QUEUE_TIMEOUT))
    try:
        value = float(raw)
        if isinstance(raw, bool) or not math.isfinite(value) or not 0 < value <= 86400:raise ValueError
        return value
    except (TypeError, ValueError, OverflowError):
        raise PacingError('模型排队等待时间配置无效', 'config_failed') from None


class _LocalState:
    def __init__(self):
        self.lock = threading.Lock()
        self.until = 0.


class ProviderLease:
    def __init__(self, config, cancelled, attempt):
        self.scope = _scope(config)
        self.cancelled = cancelled
        self.attempt = attempt
        self.timeout = _queue_timeout(config)
        self.fd = None
        self.held = False
        self.local = None
        self.directory = os.environ.get(DIRECTORY_ENV)
        self.mode = 'shared_processes' if self.directory else 'process'
        if not self.directory:
            with _registry_lock:
                self.local = _registry.setdefault(self.scope, _LocalState())

    def _open(self):
        if not self.directory:return
        try:
            path = ensure_private_directory(self.directory)
            self.state_path = path / (self.scope + '.json')
            self.fd = secure_open(path / (self.scope + '.lock'), os.O_RDWR | os.O_CREAT)
        except PacingError:raise
        except (ImportError, OSError, ValueError):
            raise PacingError('无法建立模型共享排队锁；尚未发送请求') from None

    def _try_lock(self):
        if self.local:
            self.held = self.local.lock.acquire(blocking=False)
            return self.held
        self.held = try_lock_fd(self.fd)
        return self.held

    def _write_cooldown(self, seconds):
        record = {'version':1, 'written_wall':time.time(), 'written_monotonic':time.monotonic(),
                  'delay_seconds':seconds}
        temporary = self.state_path.with_suffix('.tmp')
        fd = secure_open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            with os.fdopen(fd, 'w') as handle:
                json.dump(record, handle); handle.flush(); os.fsync(handle.fileno())
            temporary.replace(self.state_path)
        except BaseException:
            with contextlib.suppress(OSError):temporary.unlink()
            raise

    def _remaining(self):
        if self.local:return max(0., self.local.until - time.monotonic())
        try:
            fd = secure_open(self.state_path, os.O_RDONLY)
        except FileNotFoundError:return 0.
        try:
            with os.fdopen(fd) as handle:
                data = json.loads(handle.read(4097))
            if not isinstance(data, dict) or data.get('version') != 1:raise ValueError
            values = [data.get(k) for k in ('written_wall','written_monotonic','delay_seconds')]
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in values):raise ValueError
            wall, mono, delay = values
            if not 0 <= delay <= MAX_COOLDOWN:raise ValueError
            elapsed = time.monotonic() - mono
            if elapsed < 0:
                # A reboot/invalid monotonic epoch cannot create an endless wait.
                remaining = min(delay, DEFAULT_COOLDOWN)
                self._write_cooldown(remaining)
                self.attempt['pacing_clock_recovered'] = True
                return remaining
            if abs((time.time() - wall) - elapsed) > 5:
                self.attempt['pacing_wall_clock_changed'] = True
            return max(0., delay - elapsed)
        except (ValueError, TypeError, json.JSONDecodeError):
            # Recover the corrupt record once while holding its OS lock.
            self._write_cooldown(DEFAULT_COOLDOWN)
            self.attempt['pacing_state_recovered'] = True
            return float(DEFAULT_COOLDOWN)

    async def acquire(self):
        started = time.monotonic(); deadline = started + self.timeout
        self.attempt.update(pacing_scope=self.mode, pacing_max_in_flight=1,
                            queue_timeout_seconds=self.timeout, queue_wait_ms=0,
                            cooldown_wait_ms=0, request_elapsed_ms=0)
        self._open()
        cooling_started = None
        try:
            while True:
                if self.cancelled():raise InterruptedError('已取消模型排队')
                if time.monotonic() >= deadline:
                    raise PacingError('等待模型出站队列超时；尚未发送请求', 'queue_timeout')
                if self.held or self._try_lock():
                    remaining = self._remaining()
                    if remaining <= 0:
                        if self.cancelled():raise InterruptedError('已取消模型排队')
                        self.attempt['provider_cooldown_remaining_seconds'] = 0
                        return self
                    if cooling_started is None:cooling_started = time.monotonic()
                    self.attempt['provider_cooldown_remaining_seconds'] = round(remaining, 3)
                await asyncio.sleep(min(POLL_SECONDS, max(0., deadline-time.monotonic())))
        except BaseException:
            self.release(); raise
        finally:
            self.attempt['queue_wait_ms'] = round((time.monotonic()-started)*1000)
            self.attempt['cooldown_wait_ms'] = round((time.monotonic()-cooling_started)*1000) if cooling_started else 0

    def defer(self, seconds):
        """Called only for a received 429, while the request owns the lease."""
        if not self.held:raise PacingError('模型冷却记录缺少出站锁')
        seconds = DEFAULT_COOLDOWN if seconds is None else seconds
        if type(seconds) not in (int,float) or not math.isfinite(seconds) or seconds < 0:
            seconds = DEFAULT_COOLDOWN
        seconds = min(MAX_COOLDOWN, seconds)
        if self.local:self.local.until = max(self.local.until, time.monotonic()+seconds)
        else:self._write_cooldown(max(seconds, self._remaining()))
        self.attempt['provider_cooldown_seconds'] = seconds

    def release(self):
        if self.held:
            if self.local:self.local.lock.release()
            else:unlock_fd(self.fd)
            self.held = False
        if self.fd is not None:
            os.close(self.fd); self.fd = None


@contextlib.asynccontextmanager
async def provider_slot(config, cancelled, attempt):
    lease = ProviderLease(config, cancelled, attempt)
    try:
        await lease.acquire()
        yield lease
    except InterruptedError:
        raise
    except OSError:
        raise PacingError('模型出站协调状态不可读取；未重发请求') from None
    finally:lease.release()
