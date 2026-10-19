from evaluation.runner import following_case_cooldown


def test_received_429_and_open_breaker_respect_longest_pause():
    record = {'model_attempts': [
        {'http_status': 429, 'provider_cooldown_seconds': 50},
        {'status': 'circuit_open', 'post_started': False,
         'circuit_before': {'state': 'open', 'retry_after_seconds': 28}},
    ]}
    assert following_case_cooldown(record) == 50


def test_unknown_response_does_not_become_automatic_retry_or_rate_limit():
    assert following_case_cooldown({'model_attempts': [
        {'status': 'timeout', 'post_started': True,
         'retry_policy': {'automatic_retry': False}}
    ]}) == 0


def test_invalid_cooldown_and_closed_stale_timer_are_ignored():
    assert following_case_cooldown({'model_attempts': [
        {'http_status': 429, 'provider_cooldown_seconds': float('nan'),
         'retry_policy': {'retry_after_seconds': -10},
         'circuit_after': {'state': 'closed', 'retry_after_seconds': 999}}
    ]}) == 30
