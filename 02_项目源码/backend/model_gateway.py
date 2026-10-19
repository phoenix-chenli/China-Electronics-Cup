"""Bounded OpenAI-compatible JSON calls; credentials never enter public responses."""
from __future__ import annotations
import asyncio
import contextlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
import httpx

ROLES = ('table', 'document', 'verify')

def public_usage(usage):
    """Retain provider usage detail without arbitrary provider text or secrets."""
    if not isinstance(usage, dict):
        return {}
    integer = lambda value: type(value) is int and value >= 0
    result = {k: v for k, v in usage.items() if k in
              ('prompt_tokens', 'completion_tokens', 'total_tokens',
               'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens', 'reasoning_tokens') and integer(v)}
    for name, allowed in (
        ('prompt_tokens_details', ('cached_tokens', 'audio_tokens', 'image_tokens')),
        ('completion_tokens_details', ('reasoning_tokens', 'audio_tokens', 'accepted_prediction_tokens', 'rejected_prediction_tokens')),
    ):
        detail = usage.get(name)
        if isinstance(detail, dict):
            result[name] = {k: v for k, v in detail.items() if k in allowed and integer(v)}
    return result

class ModelError(ValueError):
    def __init__(self, message, code='failed'):
        super().__init__(message)
        self.code = code

class ModelConfig:
    def __init__(self, root):
        self.path = Path(root) / 'model.json'
        self.lock = threading.RLock()
    def load(self):
        with self.lock:
            if not self.path.exists(): raise ModelError('尚未配置模型连接')
            try: config = json.loads(self.path.read_text())
            except (ValueError, OSError): raise ModelError('模型配置无法读取') from None
        endpoint = urlsplit(config.get('endpoint', ''))
        if not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or not (endpoint.scheme == 'https' or endpoint.scheme == 'http' and endpoint.hostname in ('127.0.0.1', 'localhost', '::1')):
            raise ModelError('模型服务地址无效')
        if not all(isinstance(config.get('models', {}).get(role), str) and config['models'][role].strip() for role in ROLES):
            raise ModelError('请配置问数、文档和复核模型')
        return config
    def public(self):
        try:
            c = self.load()
            return dict(configured=True, provider=c.get('provider', '模型服务'), endpoint=c['endpoint'], models=c['models'], available_models=c.get('available_models', []))
        except ModelError as e:
            return dict(configured=False, provider='', endpoint='', models={r:'' for r in ROLES}, available_models=[], error=str(e))
    def update(self, models):
        with self.lock:
            c = self.load()
            if set(models) != set(ROLES) or any(v not in c.get('available_models', []) for v in models.values()):
                raise ModelError('请选择服务已登记的模型')
            c['models'] = models
            temporary = self.path.with_suffix('.tmp')
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f: json.dump(c, f, ensure_ascii=False, indent=2)
            os.chmod(temporary, 0o600); temporary.replace(self.path)
        return self.public()

class ModelGateway:
    _pace_lock = threading.Lock()
    _next_start = 0.0
    def __init__(self, config, cancelled=lambda:False, *, circuit_registry=None, project_id=None, request_observer=None):
        self.config = config
        self.cancelled = cancelled
        self.models = {}
        self.usage = []
        self.attempts = []
        self.circuit_registry = circuit_registry
        self.project_id = project_id
        self.request_observer = request_observer
        self.task_stage = '回答准备'
        self._reasoning_effort = None

    def call_with_reasoning(self, messages, purpose, effort='high'):
        """Escalate one DeepSeek planning call without changing saved roles.

        The caller owns the bounded recovery policy. No request is retried here,
        and a failed call must not leak its mode into a later ordinary call.
        """
        if effort not in ('low', 'high', 'max'):
            raise ValueError('Unsupported reasoning effort')
        previous = self._reasoning_effort
        self._reasoning_effort = effort
        try:
            return self(messages, purpose)
        finally:
            self._reasoning_effort = previous

    def set_request_stage(self, stage):
        self.task_stage = str(stage)[:160]

    def set_circuit_scope(self, registry, project_id):
        self.circuit_registry, self.project_id = registry, project_id
    def __call__(self, messages, purpose):
        if purpose not in ROLES: raise ModelError('未知模型用途')
        requested = self.config['models'][purpose]
        attempt = {'purpose': purpose, 'requested_model': requested,
                   'task_stage': self.task_stage,
                   'reported_model': None, 'status': 'pending', 'post_started': False,
                   'started_at': datetime.now(timezone.utc).isoformat(),
                   'queue_wait_ms': 0, 'cooldown_wait_ms': 0, 'request_elapsed_ms': 0}
        self.attempts.append(attempt)
        started = time.monotonic()
        breaker = None
        admitted = False
        try:
            if self.cancelled(): raise InterruptedError('已取消')
            if self.circuit_registry is not None:
                breaker = self.circuit_registry.get(self.project_id,self.config['endpoint'],requested)
                attempt['circuit_before'] = breaker.snapshot()
                breaker.before()
                admitted = True
            return self._call_json(messages, purpose, requested, attempt)
        except InterruptedError:
            attempt['status'] = 'cancelled'
            raise
        except ModelError as exc:
            attempt['status'] = exc.code
            raise
        except Exception:
            attempt['status'] = 'failed'
            raise
        finally:
            if attempt['status'] in {'timeout','transport_failed','schema_failed','content_json_failed','response_json_failed'} and 'retry_policy' not in attempt:
                from .reliability import retry_policy
                attempt['retry_policy']=retry_policy(post_started=attempt.get('post_started',False),error_code=attempt['status'])
            if breaker is not None and admitted:
                code = f"http_{attempt['http_status']}" if type(attempt.get('http_status')) is int and attempt['http_status'] >= 400 else attempt['status']
                if not attempt['post_started'] and code in {'queue_timeout','pacing_failed'}:
                    code = 'cancelled'  # Release a half-open lease without changing failure counts.
                breaker.after(code)
                attempt['circuit_after'] = breaker.snapshot()
            attempt['elapsed_ms'] = round((time.monotonic() - started) * 1000)
            attempt['finished_at'] = datetime.now(timezone.utc).isoformat()
            if self.request_observer is not None:
                self.request_observer('request_finished', dict(attempt))

    def _call_json(self, messages, purpose, requested, attempt):
        # JSON-mode gateways require an explicit JSON instruction. Enforce the
        # transport contract here so a new planner cannot accidentally omit it.
        messages = [dict(message) for message in messages]
        if messages and messages[0].get('role') == 'system' and isinstance(messages[0].get('content'),str):
            messages[0]['content'] += '\n只返回一个有效的 JSON 对象。'
        else:
            messages.insert(0, {'role':'system','content':'只返回一个有效的 JSON 对象。'})
        body = dict(model=requested, messages=messages, stream=False, max_tokens=12000 if purpose == 'verify' else 6000, temperature=0, response_format={'type':'json_object'})
        if requested.startswith('deepseek'):
            body['thinking'] = {'type':'enabled' if self._reasoning_effort else 'disabled'}
            if self._reasoning_effort:
                body['reasoning_effort'] = self._reasoning_effort
                body['max_tokens'] = 16000  # Includes reasoning and the JSON answer.
                body.pop('temperature', None)  # Ignored in DeepSeek thinking mode.
        if requested.startswith('qwen'): body['enable_thinking'] = False
        # Kimi's thinking models require temperature=1; the configured kimi-k3
        # provider rejects disabling reasoning. Set compatible parameters before
        # the first POST instead of retrying a possibly accepted request.
        if requested.startswith('kimi'):
            body['temperature'] = 1
            body['thinking'] = {'type':'enabled'}
        # Reproducibility metadata only; never store prompts, images or secrets.
        attempt['request_settings'] = {key: body[key] for key in
            ('temperature','max_tokens','stream','response_format','thinking','enable_thinking','reasoning_effort') if key in body}
        response = asyncio.run(self._request(body))
        attempt['status'] = 'response_received'
        # Keep reported identity/usage even when the completed response is truncated
        # or malformed; a failed result may still have consumed model tokens.
        actual = response.get('model') if isinstance(response, dict) else None
        actual = actual if isinstance(actual, str) and actual.strip() else None
        attempt['reported_model'] = actual
        # Keep the existing display fallback, but do not label it a provider report.
        self.models[purpose] = actual or requested
        usage = response.get('usage', {}) if isinstance(response, dict) else {}
        if not isinstance(usage, dict): usage = {}
        measured_usage = public_usage(usage)
        attempt['usage'] = measured_usage
        self.usage.append({'purpose':purpose,'requested_model':requested,'reported_model':actual, **measured_usage})
        try:
            choice = response['choices'][0]
            content = choice['message']['content']
            if choice.get('finish_reason') == 'length': raise ModelError('模型输出超出本次预算，请缩小问题或更换模型', 'output_truncated')
            if not isinstance(content, str) or not content.strip(): raise ModelError('模型未返回有效回答', 'schema_failed')
            content = re.sub(r'^```(?:json)?\s*|\s*```$', '', content.strip())
            result = json.loads(content)
            if not isinstance(result, dict): raise ModelError('模型返回格式不符合约定，请重试或切换模型', 'schema_failed')
        except ModelError: raise
        except json.JSONDecodeError: raise ModelError('模型返回格式不符合约定，请重试或切换模型', 'content_json_failed') from None
        except (KeyError, IndexError, TypeError, AttributeError): raise ModelError('模型返回格式不符合约定，请重试或切换模型', 'schema_failed') from None
        attempt['status'] = 'completed'
        return result
    async def _request(self, body):
        from .provider_pacing import provider_slot, PacingError
        attempt = self.attempts[-1] if self.attempts else {}
        try:
            async with provider_slot(self.config, self.cancelled, attempt) as lease:
                started = time.monotonic()
                try:
                    if self.cancelled():raise InterruptedError('已取消')
                    return await self._request_once(body, lease)
                finally:
                    attempt['request_elapsed_ms'] = round((time.monotonic()-started)*1000)
        except PacingError as exc:
            raise ModelError(str(exc), exc.code) from None

    async def _request_once(self, body, lease):
        timeout = min(120, max(5, int(self.config.get('timeout', 90))))
        has_images = any(isinstance(message, dict) and isinstance(message.get('content'), list)
                         and any(isinstance(part, dict) and part.get('type') == 'image_url'
                                 for part in message['content']) for message in body.get('messages', []))
        attempt = self.attempts[-1] if self.attempts else {}
        attempt['input_modalities'] = ['text', 'image'] if has_images else ['text']
        attempt['image_count'] = sum(
            part.get('type') == 'image_url' for message in body.get('messages', [])
            if isinstance(message, dict) and isinstance(message.get('content'), list)
            for part in message['content'] if isinstance(part, dict))
        if has_images:
            value = self.config.get('vision_timeout', max(timeout, 180))
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < float('inf'):
                raise ModelError('视觉响应等待时间配置无效', 'config_failed')
            timeout = min(300, max(5, int(value)))
        reasoning_request = (str(body.get('model', '')).startswith('deepseek')
                             and body.get('thinking', {}).get('type') == 'enabled'
                             and body.get('reasoning_effort') in ('low', 'high', 'max'))
        if reasoning_request:
            value = self.config.get('reasoning_timeout', max(timeout, 480))
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < float('inf'):
                raise ModelError('推理响应等待时间配置无效', 'config_failed')
            # A reasoning response includes internal thinking before its JSON.
            # Wait for that same POST; never spend another request on a timeout.
            timeout = min(600, max(5, int(value)))
        if self.attempts:
            self.attempts[-1]['effective_timeout_seconds'] = timeout
            self.attempts[-1]['timeouts'] = {'connect':10,'read':timeout,'overall':timeout,'kind':None}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout,connect=10),follow_redirects=False,trust_env=False,verify=True) as client:
                if self.cancelled():raise InterruptedError('已取消')
                if self.request_observer is not None:
                    self.request_observer('post_started', dict(self.attempts[-1]) if self.attempts else {})
                if self.attempts:self.attempts[-1]['post_started']=True
                task = asyncio.create_task(client.post(self.config['endpoint'].rstrip('/')+'/chat/completions', json=body, headers={'Authorization':'Bearer '+self.config.get('api_key',''), 'User-Agent':'Zhiheng/0.2 local research'}))
                deadline = time.monotonic()+timeout
                try:
                    while not task.done():
                        if self.cancelled(): raise InterruptedError('已取消')
                        if time.monotonic() > deadline:
                            if self.attempts:
                                self.attempts[-1]['timeout_kind']='overall'
                                self.attempts[-1]['timeouts']['kind']='overall'
                            raise ModelError('模型整体等待超时；未自动重复提交，可稍后重试', 'timeout')
                        await asyncio.wait({task}, timeout=.1)
                    response = await task
                finally:
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError): await task
            from .reliability import retry_policy
            response_policy=retry_policy(response.status_code,response.headers.get('Retry-After'),post_started=True)
            if self.attempts:
                self.attempts[-1]['http_status']=response.status_code
                self.attempts[-1]['retry_policy']=response_policy
            if response.status_code == 429:
                lease.defer(response_policy['retry_after_seconds'])
            if self.cancelled(): raise InterruptedError('已取消')
            if response.is_redirect: raise ModelError('模型地址要求跳转，请确认最终接口地址', 'http_failed')
            if response.status_code >= 400:
                reason={401:'模型凭据无效或过期',402:'模型服务返回账务或额度错误，请检查连接账户',403:'模型服务拒绝访问',404:'模型或接口不可用',429:'模型服务限流或额度不足'}.get(response.status_code,'模型服务请求失败')
                # Never echo provider error text: it can contain request content
                # or credentials. Recognize only specific compatibility classes.
                if response.status_code == 400:
                    try:
                        detail = response.json()
                        error = detail.get('error', detail) if isinstance(detail, dict) else {}
                        message = str(error.get('message', '')).lower() if isinstance(error, dict) else ''
                        code = error.get('code') if isinstance(error, dict) else None
                        if 'must contain' in message and 'json' in message:
                            reason = '模型服务要求显式JSON输出指令'
                        elif 'temperature' in message:
                            reason = '模型温度参数不兼容，请检查所选模型的推理设置'
                        elif code == 'REASONING_REQUIRED':
                            reason = '当前模型要求启用深度思考'
                        elif any(term in message for term in ('unsupported parameter', 'unknown parameter', 'invalid parameter')):
                            reason = '当前模型不支持请求中的参数'
                    except (ValueError, TypeError):
                        pass
                raise ModelError(f'{reason}（HTTP {response.status_code}）；未自动重复提交', 'http_failed')
            if len(response.content)>2*1024*1024: raise ModelError('模型响应超过2MB限制', 'response_too_large')
            return response.json()
        except httpx.TimeoutException as exc:
            kind='connect' if isinstance(exc,httpx.ConnectTimeout) else 'read' if isinstance(exc,httpx.ReadTimeout) else 'write' if isinstance(exc,httpx.WriteTimeout) else 'pool' if isinstance(exc,httpx.PoolTimeout) else 'transport'
            if self.attempts:
                from .reliability import retry_policy
                self.attempts[-1]['timeout_kind']=kind
                self.attempts[-1]['timeouts']['kind']=kind
                self.attempts[-1]['retry_policy']=retry_policy(post_started=self.attempts[-1].get('post_started',False),error_code=kind+'_timeout')
            raise ModelError('模型连接超时；未自动重复提交，可稍后重试' if kind=='connect' else '模型响应读取超时；未自动重复提交，可稍后重试', 'timeout') from None
        except httpx.RequestError: raise ModelError('无法连接模型服务，请检查网络和证书；未自动重复提交', 'transport_failed') from None
        except json.JSONDecodeError: raise ModelError('模型服务未返回有效JSON', 'response_json_failed') from None
