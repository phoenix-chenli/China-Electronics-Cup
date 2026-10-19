import json
import httpx
import pytest
from backend.model_gateway import ModelGateway,ModelError
from backend.reliability import GatewayCircuits,CircuitBreaker


@pytest.fixture
def config(monkeypatch):
    from backend import provider_pacing
    monkeypatch.delenv(provider_pacing.DIRECTORY_ENV, raising=False)
    monkeypatch.delenv(provider_pacing.QUEUE_TIMEOUT_ENV, raising=False)
    provider_pacing._registry.clear()
    return {'endpoint':'https://model.example.test/v1','api_key':'DO_NOT_LOG_TOKEN','models':{r:'same-model' for r in ('table','document','verify')},'timeout':5}


def mocked(monkeypatch,handler):
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(**kwargs,transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(ModelGateway,'_next_start',0)


@pytest.mark.parametrize('exception,kind',[(httpx.ConnectTimeout,'connect'),(httpx.ReadTimeout,'read'),(httpx.WriteTimeout,'write'),(httpx.PoolTimeout,'pool')])
def test_timeout_kind_is_audited_without_changing_compatible_code(config,monkeypatch,exception,kind):
    calls=[]
    def handle(request):calls.append(1);raise exception('DO_NOT_LOG_TOKEN',request=request)
    mocked(monkeypatch,handle);gateway=ModelGateway(config)
    with pytest.raises(ModelError) as error:gateway([],'document')
    assert error.value.code=='timeout' and len(calls)==1
    a=gateway.attempts[0];assert a['timeout_kind']==kind and a['timeouts']['kind']==kind
    assert a['retry_policy']['automatic_retry'] is False
    assert 'DO_NOT_LOG_TOKEN' not in json.dumps(gateway.attempts)


@pytest.mark.parametrize('status',[429,503])
def test_retry_after_and_breaker_do_not_repeat_http_post(config,monkeypatch,status):
    calls=[]
    def handle(request):calls.append(1);return httpx.Response(status,headers={'Retry-After':'23'},text='DO_NOT_LOG_TOKEN')
    mocked(monkeypatch,handle);registry=GatewayCircuits(threshold=1)
    gateway=ModelGateway(config,circuit_registry=registry,project_id='P1')
    with pytest.raises(ModelError):gateway([],'document')
    with pytest.raises(ModelError) as error:gateway([],'document')
    assert error.value.code=='circuit_open' and len(calls)==1
    assert gateway.attempts[0]['retry_policy']['retry_after_seconds']==23
    assert not gateway.attempts[1]['post_started']
    # Circuit ownership remains per-project, while actual provider quota is shared.
    ModelGateway._next_start=0
    with pytest.raises(ModelError) as other:
        ModelGateway({**config,'queue_timeout':.06},circuit_registry=registry,project_id='P2')([],'document')
    assert len(calls)==(1 if status==429 else 2)
    if status==429:
        assert other.value.code=='queue_timeout'
        assert registry.get('P2',config['endpoint'],'same-model').failures==0


def test_cancelled_half_open_probe_preserves_failures_and_releases_lease(config,monkeypatch):
    clock=[0];registry=GatewayCircuits(threshold=1,cooldown=10,clock=lambda:clock[0])
    breaker=registry.get('P',config['endpoint'],'same-model');breaker.before();breaker.after('timeout')
    clock[0]=11
    async def request(self,body):raise InterruptedError('cancelled fixture')
    monkeypatch.setattr(ModelGateway,'_request',request)
    gateway=ModelGateway(config,circuit_registry=registry,project_id='P')
    with pytest.raises(InterruptedError):gateway([],'document')
    assert breaker.failures==1 and breaker.until==10 and not breaker.probing
    breaker.before();breaker.after()
    assert breaker.snapshot()['state']=='closed'


def test_protocol_failure_is_not_transport_outage(config,monkeypatch):
    calls=[]
    def handle(request):calls.append(1);return httpx.Response(200,json={'choices':[{'message':{'content':'broken'}}]})
    mocked(monkeypatch,handle);registry=GatewayCircuits(threshold=1)
    gateway=ModelGateway(config,circuit_registry=registry,project_id='P')
    with pytest.raises(ModelError) as error:gateway([],'document')
    assert error.value.code=='content_json_failed'
    assert gateway.attempts[0]['circuit_after']['state']=='closed'
    assert len(calls)==1


def test_endpoint_model_and_service_circuits_are_isolated():
    a,b=GatewayCircuits(threshold=1),GatewayCircuits(threshold=1)
    broken=a.get('P','endpoint1','m1');broken.before();broken.after('timeout')
    for item in (a.get('P','endpoint2','m1'),a.get('P','endpoint1','m2'),b.get('P','endpoint1','m1')):
        item.before();assert item.snapshot()['state']=='closed'
