import threading
import pytest
from evaluation.acceptance_budget import AcceptanceBudget, BudgetExceeded


def reserve(budget, case='case'):
    return budget.reserve(case, code_sha256='c', config_sha256='m')


def test_45th_round_finishes_internal_calls_but_46th_is_blocked(tmp_path):
    budget = AcceptanceBudget(tmp_path/'ledger.jsonl')
    for i in range(44):
        aid = reserve(budget, str(i)); budget.observe(aid, 'post_started', {})
        budget.finish(aid, 'failed' if i == 0 else 'completed')
    last = reserve(budget, '45th')
    with pytest.raises(BudgetExceeded): reserve(budget, '46th')
    for purpose in ['document', 'verify']:
        budget.observe(last, 'post_started', {'purpose': purpose})
        budget.observe(last, 'request_finished', {'purpose': purpose, 'usage': {'total_tokens': 7}})
    budget.finish(last, 'completed')
    state = budget.snapshot()
    assert state['used_rounds'] == 45 and state['post_started'] == 46
    assert state['usage_reported']['total_tokens'] == 14
    with pytest.raises(BudgetExceeded): reserve(AcceptanceBudget(budget.path), 'after_restart')


def test_unused_reservation_released_but_started_timeout_consumes_budget(tmp_path):
    b = AcceptanceBudget(tmp_path/'ledger.jsonl', limit=1)
    aid = reserve(b); b.finish(aid, 'cancelled_before_post')
    assert b.snapshot()['remaining_rounds'] == 1
    aid = reserve(b); b.observe(aid, 'post_started', {}); b.finish(aid, 'timeout')
    assert b.snapshot()['used_rounds'] == 1
    with pytest.raises(BudgetExceeded): reserve(b)
    with pytest.raises(BudgetExceeded): b.observe(aid, 'post_started', {})


def test_shared_budget_atomic_reservations_and_unfinished_recovery(tmp_path):
    path = tmp_path/'ledger.jsonl'; AcceptanceBudget(path, limit=3)
    admitted = []
    def worker():
        try: admitted.append(reserve(AcceptanceBudget(path, limit=3)))
        except BudgetExceeded: pass
    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(admitted) == 3
    assert AcceptanceBudget(path, limit=3).snapshot()['pending_reservations'] == 3


def test_corrupt_journal_and_changed_limit_fail_closed(tmp_path):
    path = tmp_path/'ledger.jsonl'; b = AcceptanceBudget(path)
    with pytest.raises(ValueError): AcceptanceBudget(path, limit=44)
    with path.open('a') as f: f.write('{"type":')
    with pytest.raises(RuntimeError): b.snapshot()


def test_user_can_remove_limit_without_resetting_past_attempts(tmp_path):
    path = tmp_path/'ledger.jsonl'; b = AcceptanceBudget(path, limit=1)
    first = reserve(b); b.observe(first, 'post_started', {}); b.finish(first, 'completed')
    with pytest.raises(BudgetExceeded): reserve(b)
    with pytest.raises(ValueError): b.remove_limit_by_user('')
    b.remove_limit_by_user('用户明确取消测试轮数上限，只做必要测试')
    resumed = AcceptanceBudget(path, limit=1)
    second = reserve(resumed); resumed.observe(second, 'post_started', {}); resumed.finish(second, 'completed')
    state = resumed.snapshot()
    assert state['limit'] is None and state['remaining_rounds'] is None
    assert state['used_rounds'] == 2 and state['post_started'] == 2
    assert len(state['attempts']) == 2
