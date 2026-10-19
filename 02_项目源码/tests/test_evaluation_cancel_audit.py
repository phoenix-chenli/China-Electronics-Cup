import threading
from types import SimpleNamespace
from evaluation.runner import wait_answer_cleanup


def test_cleanup_wait_observes_worker_attempt_write():
    service = SimpleNamespace(lock=threading.Lock(), events={'a': object()}, attempts=[])
    def finalize():
        with service.lock:
            service.attempts.append({'post_started': True})
            service.events.pop('a')
    timer = threading.Timer(.03, finalize)
    timer.start()
    try:
        assert wait_answer_cleanup(service, 'a', timeout=.5)
        assert service.attempts == [{'post_started': True}]
    finally:
        timer.join()


def test_unknown_inflight_post_is_not_complete_zero_call_run():
    service = SimpleNamespace(lock=threading.Lock(), events={'a': object()})
    assert not wait_answer_cleanup(service, 'a', timeout=.01)
