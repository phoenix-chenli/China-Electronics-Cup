"""Request compatibility and transport safety; no test calls a real provider."""
import asyncio
import json

import httpx
import pytest

from backend.model_gateway import ModelError, ModelGateway


@pytest.fixture
def config(monkeypatch):
    from backend import provider_pacing
    monkeypatch.delenv(provider_pacing.DIRECTORY_ENV, raising=False)
    monkeypatch.delenv(provider_pacing.QUEUE_TIMEOUT_ENV, raising=False)
    provider_pacing._registry.clear()
    return {'endpoint':'https://models.example.test/v1','api_key':'secret-test-token',
            'models':{'document':'kimi-k3','table':'qwen3.8-max','verify':'glm-5.3'},'timeout':5}


def test_kimi_parameters_are_compatible_before_first_post(config, monkeypatch):
    sent=[]
    async def request(self, body):
        sent.append(body)
        return {'model':'kimi-k3','choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]}
    monkeypatch.setattr(ModelGateway,'_request',request)
    gateway=ModelGateway(config)
    assert gateway([{'role':'user','content':'JSON please'}],'document')=={'ok':True}
    assert len(sent)==1
    assert sent[0]['temperature']==1
    assert sent[0]['thinking']=={'type':'enabled'}
    assert sent[0]['model']=='kimi-k3'
    assert gateway.models['document']=='kimi-k3'


def test_qwen_keeps_own_parameter_family(config, monkeypatch):
    async def request(self, body):
        assert body['enable_thinking'] is False
        assert 'thinking' not in body
        return {'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}]}
    monkeypatch.setattr(ModelGateway,'_request',request)
    ModelGateway(config)([{'role':'user','content':'JSON'}],'table')


def test_deepseek_reasoned_call_is_audited_and_does_not_change_next_call(config, monkeypatch):
    config['models']['table'] = 'deepseek-flash'
    sent = []
    async def request(self, body):
        sent.append(body)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]}
    monkeypatch.setattr(ModelGateway, '_request', request)
    gateway = ModelGateway(config)
    gateway.call_with_reasoning([], 'table')
    gateway([], 'table')
    assert len(sent) == 2
    assert sent[0]['thinking'] == {'type': 'enabled'}
    assert sent[0]['reasoning_effort'] == 'high'
    assert sent[0]['max_tokens'] == 16000
    assert 'temperature' not in sent[0]
    assert gateway.attempts[0]['request_settings']['reasoning_effort'] == 'high'
    assert sent[1]['thinking'] == {'type': 'disabled'}
    assert 'reasoning_effort' not in sent[1]
    assert sent[1]['max_tokens'] == 6000


def test_reasoned_call_failure_restores_mode_without_retry(config, monkeypatch):
    config['models']['table'] = 'deepseek-flash'
    sent = []
    async def request(self, body):
        sent.append(body)
        raise ModelError('request failed', 'transport_failed')
    monkeypatch.setattr(ModelGateway, '_request', request)
    gateway = ModelGateway(config)
    with pytest.raises(ModelError):
        gateway.call_with_reasoning([], 'table')
    assert len(sent) == 1
    assert gateway._reasoning_effort is None


def test_reasoned_call_preserves_other_provider_parameters(config, monkeypatch):
    async def request(self, body):
        assert body['enable_thinking'] is False
        assert 'reasoning_effort' not in body
        assert 'thinking' not in body
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]}
    monkeypatch.setattr(ModelGateway, '_request', request)
    assert ModelGateway(config).call_with_reasoning([], 'table') == {'ok': True}


@pytest.mark.parametrize('code,body,expected',[
    (400,{'message':'invalid temperature: must be 1.0 when thinking is enabled secret-test-token'},'温度参数不兼容'),
    (400,{'code':'REASONING_REQUIRED','message':'secret-test-token'},'要求启用深度思考'),
    (400,{'error':{'message':'unsupported parameter: private secret-test-token'}},'不支持请求中的参数'),
    (401,{'message':'secret-test-token'},'凭据无效'),
    (429,{'message':'secret-test-token'},'限流或额度不足'),
])
def test_one_post_and_safe_error_message(config, monkeypatch, code, body, expected):
    requests=[]
    def handle(request):
        requests.append(request)
        return httpx.Response(code,json=body)
    original=httpx.AsyncClient
    def factory(**kwargs):
        assert kwargs['verify'] is True
        assert kwargs['follow_redirects'] is False
        assert kwargs['trust_env'] is False
        return original(**kwargs,transport=httpx.MockTransport(handle))
    monkeypatch.setattr(httpx,'AsyncClient',factory)
    monkeypatch.setattr(ModelGateway,'_next_start',0)
    with pytest.raises(ModelError) as exc:
        asyncio.run(ModelGateway(config)._request({'model':'kimi-k3'}))
    assert expected in str(exc.value)
    assert 'secret-test-token' not in str(exc.value)
    assert len(requests)==1
    assert '未自动重复提交' in str(exc.value)


def test_cancel_before_request_does_not_post(config):
    with pytest.raises(InterruptedError):
        ModelGateway(config,cancelled=lambda:True)([],'document')


def test_usage_details_and_observer_survive_truncated_paid_response(config,monkeypatch):
    events=[]
    def handle(request):
        return httpx.Response(200,json={'model':'reported-test',
            'usage':{'prompt_tokens':100,'completion_tokens':8,'total_tokens':108,
                     'prompt_cache_hit_tokens':30,'prompt_cache_miss_tokens':70,
                     'prompt_tokens_details':{'cached_tokens':30,'secret':'ignore'},
                     'completion_tokens_details':{'reasoning_tokens':5},'secret':'do not persist'},
            'choices':[{'finish_reason':'length','message':{'content':'{"incomplete":'}}]})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw: original(**kw,transport=httpx.MockTransport(handle)))
    gateway=ModelGateway(config,request_observer=lambda event,data:events.append((event,data)))
    with pytest.raises(ModelError) as exc:gateway([{'role':'user','content':'JSON'}],'table')
    assert exc.value.code=='output_truncated'
    assert [e[0] for e in events]==['post_started','request_finished']
    final=events[-1][1]
    assert final['post_started'] and final['status']=='output_truncated'
    assert final['usage']['completion_tokens_details']['reasoning_tokens']==5
    assert final['usage']['prompt_cache_hit_tokens']==30
    assert 'secret' not in json.dumps(final['usage'])
    assert final['started_at'] and final['finished_at']


def test_budget_observer_blocks_before_network(config,monkeypatch):
    def deny(event,data):
        if event=='post_started':raise ModelError('test budget exhausted','budget_exhausted')
    def handle(request):pytest.fail('budget did not block network')
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw: original(**kw,transport=httpx.MockTransport(handle)))
    gateway=ModelGateway(config,request_observer=deny)
    with pytest.raises(ModelError) as exc:gateway([{'role':'user','content':'JSON'}],'table')
    assert exc.value.code=='budget_exhausted' and not gateway.attempts[-1]['post_started']


@pytest.mark.parametrize('image_request', [False, True])
def test_cancel_during_pending_http_request_stops_without_retry(config,monkeypatch,image_request):
    state={'cancel':False,'requests':0,'cancelled':False}
    async def handle(request):
        state['requests']+=1
        state['cancel']=True
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            state['cancelled']=True
            raise
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(**kwargs,transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(ModelGateway,'_next_start',0)
    body = {'model': 'kimi-k3'}
    if image_request:
        body['messages'] = [{'role': 'user', 'content': [{'type': 'image_url'}]}]
    with pytest.raises(InterruptedError):
        asyncio.run(ModelGateway(config,cancelled=lambda:state['cancel'])._request(body))
    assert state['requests']==1 and state['cancelled']


def test_redirect_is_not_followed(config,monkeypatch):
    requests=[]
    def handle(request):
        requests.append(request)
        return httpx.Response(307,headers={'Location':'https://untrusted.example/'})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(**kwargs,transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(ModelGateway,'_next_start',0)
    with pytest.raises(ModelError,match='要求跳转'):
        asyncio.run(ModelGateway(config)._request({'model':'kimi-k3'}))
    assert len(requests)==1


def test_verifier_budget_and_failed_response_usage_are_preserved(config,monkeypatch):
    async def request(self,body):
        assert body['max_tokens']==12000
        return {'model':'glm-5.3','choices':[{'finish_reason':'length','message':{'content':''}}],
                'usage':{'completion_tokens':12000,'prompt_tokens':200}}
    monkeypatch.setattr(ModelGateway,'_request',request)
    gateway=ModelGateway(config)
    with pytest.raises(ModelError,match='超出本次预算'):
        gateway([{'role':'user','content':'JSON'}],'verify')
    assert gateway.models['verify']=='glm-5.3'
    assert gateway.usage[0]['completion_tokens']==12000


def test_json_gateway_accepts_new_planner_without_json_keyword(config,monkeypatch):
    messages=[{'role':'system','content':'返回结构化步骤。'},{'role':'user','content':[{'type':'text','text':'查询资料'}]}]
    original=json.loads(json.dumps(messages))
    async def request(self,body):
        # Mirrors the provider's observed JSON-mode precondition.
        assert any('json' in str(m.get('content','')).lower() for m in body['messages'])
        assert body['messages'][-1]['content']==original[-1]['content']
        return {'choices':[{'message':{'content':'{"status":"ready"}'},'finish_reason':'stop'}]}
    monkeypatch.setattr(ModelGateway,'_request',request)
    assert ModelGateway(config)(messages,'table')['status']=='ready'
    assert messages==original


@pytest.mark.parametrize('response, expected', [
    ({'model': 'reported-model', 'choices': [{'message': {'content': '{broken'}}]}, 'content_json_failed'),
    ({'model': 'reported-model', 'choices': [{'message': {'content': '[]'}}]}, 'schema_failed'),
    ({'model': 'reported-model', 'choices': []}, 'schema_failed'),
    ({'model': 'reported-model', 'choices': [{'message': {'content': ''}, 'finish_reason': 'length'}]}, 'output_truncated'),
    ({'model': 'reported-model', 'choices': [{'message': {'content': ''}}]}, 'schema_failed'),
])
def test_response_failures_keep_identity_usage_and_final_status(config, monkeypatch, response, expected):
    sent = []
    async def request(self, body):
        sent.append(body)
        return {**response, 'usage': {'prompt_tokens': 12, 'completion_tokens': 3}}
    monkeypatch.setattr(ModelGateway, '_request', request)
    gateway = ModelGateway(config)
    with pytest.raises(ModelError):
        gateway([], 'document')
    attempt = gateway.attempts[0]
    assert attempt['status'] == expected
    assert attempt['requested_model'] == 'kimi-k3'
    assert attempt['reported_model'] == 'reported-model'
    assert attempt['elapsed_ms'] >= 0
    assert gateway.usage[0]['completion_tokens'] == 3
    assert len(sent) == 1
    assert 'secret-test-token' not in json.dumps(gateway.attempts)


@pytest.mark.parametrize('failure, expected', [
    ('http', 'http_failed'), ('json', 'response_json_failed'),
    ('timeout', 'timeout'), ('connection', 'transport_failed'),
])
def test_transport_failures_record_requested_model_without_retry(config, monkeypatch, failure, expected):
    sent = []
    def handle(request):
        sent.append(request)
        if failure == 'timeout': raise httpx.ReadTimeout('private secret-test-token', request=request)
        if failure == 'connection': raise httpx.ConnectError('private secret-test-token', request=request)
        if failure == 'http': return httpx.Response(503, text='private secret-test-token')
        return httpx.Response(200, text='not json secret-test-token')
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(ModelGateway, '_next_start', 0)
    gateway = ModelGateway(config)
    with pytest.raises(ModelError) as error:
        gateway([], 'table')
    assert len(sent) == 1
    assert gateway.attempts[0]['status'] == expected
    assert gateway.attempts[0]['requested_model'] == 'qwen3.8-max'
    assert gateway.attempts[0]['reported_model'] is None
    assert gateway.attempts[0]['post_started'] is True
    assert 'secret-test-token' not in str(error.value)
    assert 'secret-test-token' not in json.dumps(gateway.attempts)


def test_cancel_before_post_is_recorded_and_success_does_not_invent_reported_model(config, monkeypatch):
    cancelled = ModelGateway(config, cancelled=lambda: True)
    with pytest.raises(InterruptedError): cancelled([], 'verify')
    assert cancelled.attempts[0]['status'] == 'cancelled'
    assert cancelled.attempts[0]['requested_model'] == 'glm-5.3'
    assert cancelled.attempts[0]['post_started'] is False
    async def request(self, body):
        return {'choices': [{'message': {'content': '{"ok": true}'}}]}
    monkeypatch.setattr(ModelGateway, '_request', request)
    gateway = ModelGateway(config)
    assert gateway([], 'table') == {'ok': True}
    assert gateway.attempts[0]['status'] == 'completed'
    assert gateway.attempts[0]['reported_model'] is None
    assert gateway.usage[0]['reported_model'] is None
    assert gateway.models['table'] == 'qwen3.8-max'  # Existing display fallback only.


@pytest.mark.parametrize('content, override, expected', [
    ('image_url is only prose', None, 90),
    ([{'type': 'text', 'text': 'image_url'}], None, 90),
    ([{'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,AA=='}}], None, 180),
    ([{'type': 'image_url'}], 240, 240),
    ([{'type': 'image_url'}], 999, 300),
    ([{'type': 'image_url'}], 1, 5),
])
def test_visual_wait_window_is_audited_and_never_reposts(config, monkeypatch, content, override, expected):
    config = {**config, 'timeout': 90}
    if override is not None:
        config['vision_timeout'] = override
    requests = []
    def handle(request):
        requests.append(request)
        raise httpx.ReadTimeout('private token', request=request)
    original = httpx.AsyncClient
    def factory(**kwargs):
        assert kwargs['timeout'].read == expected
        assert kwargs['timeout'].connect == 10
        return original(**kwargs, transport=httpx.MockTransport(handle))
    monkeypatch.setattr(httpx, 'AsyncClient', factory)
    monkeypatch.setattr(ModelGateway, '_next_start', 0)
    gateway = ModelGateway(config)
    with pytest.raises(ModelError) as exc:
        gateway([{'role': 'user', 'content': content}], 'document')
    assert exc.value.code == 'timeout' and len(requests) == 1
    assert gateway.attempts[0]['effective_timeout_seconds'] == expected
    assert gateway.attempts[0]['status'] == 'timeout'


@pytest.mark.parametrize('override', [True, '180', float('nan'), float('inf'), -1])
def test_invalid_visual_timeout_never_posts(config, monkeypatch, override):
    monkeypatch.setattr(ModelGateway, '_next_start', 0)
    gateway = ModelGateway({**config, 'vision_timeout': override})
    with pytest.raises(ModelError) as exc:
        gateway([{'role': 'user', 'content': [{'type': 'image_url'}]}], 'document')
    assert exc.value.code == 'config_failed' and not gateway.attempts[0]['post_started']


@pytest.mark.parametrize('override, images, expected', [
    (None, False, 480), (None, True, 480), (180, False, 180),
    (480, False, 480), (600, False, 600), (999, False, 600), (1, False, 5),
])
def test_reasoning_wait_window_does_not_retry_post(config, monkeypatch, override, images, expected):
    value = {**config, 'models': {**config['models'], 'table': 'deepseek-flash'}, 'timeout': 90}
    if override is not None:
        value['reasoning_timeout'] = override
    sent = []
    def handle(request):
        sent.append(request)
        raise httpx.ReadTimeout('unavailable', request=request)
    original = httpx.AsyncClient
    def factory(**kwargs):
        assert kwargs['timeout'].read == expected
        assert kwargs['timeout'].connect == 10
        return original(**kwargs, transport=httpx.MockTransport(handle))
    monkeypatch.setattr(httpx, 'AsyncClient', factory)
    gateway = ModelGateway(value)
    content = [{'type': 'image_url'}] if images else 'Return JSON'
    with pytest.raises(ModelError):
        gateway.call_with_reasoning([{'role': 'user', 'content': content}], 'table')
    assert len(sent) == 1
    assert gateway.attempts[0]['effective_timeout_seconds'] == expected
    assert gateway._reasoning_effort is None


@pytest.mark.parametrize('override', [True, '240', float('nan'), float('inf'), -1])
def test_invalid_reasoning_wait_never_posts(config, override):
    gateway = ModelGateway({**config, 'models': {**config['models'], 'table': 'deepseek-flash'},
                            'reasoning_timeout': override})
    with pytest.raises(ModelError) as exc:
        gateway.call_with_reasoning([], 'table')
    assert exc.value.code == 'config_failed'
    assert not gateway.attempts[0]['post_started']
