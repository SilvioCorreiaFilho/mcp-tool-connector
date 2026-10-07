import threading
import time

import pytest

from mcp_tool_connector import IdempotencyCache, IsolatedCaller


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def boom(**kw):
    raise ValueError('bad input')


def test_tool_exception_becomes_result_not_raise():
    with IsolatedCaller({'t': boom}) as c:
        r = c.call('t', x=1)
    assert r.ok is False
    assert 'ValueError: bad input' in r.error
    assert 'Do not repeat this call' in r.as_agent_message()


def test_unknown_tool_lists_available():
    with IsolatedCaller({'a': lambda: 1, 'b': lambda: 2}) as c:
        r = c.call('zzz')
    assert not r.ok and 'Available: a, b' in r.error


def test_breaker_opens_after_threshold_and_stops_calling():
    calls = []

    def failing(**kw):
        calls.append(1)
        raise TimeoutError('down')

    clock = FakeClock()
    with IsolatedCaller({'t': failing}, failure_threshold=3, cooldown_seconds=60, clock=clock) as c:
        for _ in range(3):
            assert not c.call('t').ok
        r = c.call('t')
        assert 'circuit open' in r.error
        assert len(calls) == 3                     # 4th call never reached the tool
        assert c.stats['blocked'] == 1


def test_breaker_half_opens_after_cooldown():
    state = {'fail': True}

    def tool(**kw):
        if state['fail']:
            raise RuntimeError('x')
        return 'ok'

    clock = FakeClock()
    with IsolatedCaller({'t': tool}, failure_threshold=2, cooldown_seconds=30, clock=clock) as c:
        c.call('t'); c.call('t')
        assert c.is_open('t')
        clock.t += 31
        state['fail'] = False
        r = c.call('t')
        assert r.ok and r.result == 'ok'
        assert not c.is_open('t')


def test_success_resets_failure_count():
    seq = iter([RuntimeError('a'), 'ok', RuntimeError('b'), RuntimeError('c')])

    def tool(**kw):
        v = next(seq)
        if isinstance(v, Exception):
            raise v
        return v

    with IsolatedCaller({'t': tool}, failure_threshold=3) as c:
        for _ in range(4):
            c.call('t')
        assert not c.is_open('t')                   # 1 fail, success, 2 fails < 3


def test_timeout_is_enforced_and_counts_as_failure():
    release = threading.Event()

    def slow(**kw):
        release.wait(5)
        return 'late'

    with IsolatedCaller({'t': slow}, timeout_seconds=0.2, failure_threshold=1) as c:
        t0 = time.monotonic()
        r = c.call('t')
        elapsed = time.monotonic() - t0
        release.set()
    assert not r.ok and 'timed out' in r.error
    assert elapsed < 2
    assert c.stats['failed'] == 1


def test_cache_only_for_read_only_tools():
    n = {'read': 0, 'write': 0}

    def read(**kw):
        n['read'] += 1
        return {'v': n['read']}

    def write(**kw):
        n['write'] += 1
        return 'done'

    with IsolatedCaller({'r': read, 'w': write}, read_only={'r'}, cache=IdempotencyCache()) as c:
        a, b = c.call('r', id=1), c.call('r', id=1)
        c.call('w', id=1); c.call('w', id=1)
    assert b.cached and b.result == a.result and n['read'] == 1
    assert n['write'] == 2                          # writes are never deduplicated


def test_cache_key_is_argument_order_independent():
    assert IdempotencyCache.key('t', {'a': 1, 'b': 2}) == IdempotencyCache.key('t', {'b': 2, 'a': 1})
    assert IdempotencyCache.key('t', {'a': 1}) != IdempotencyCache.key('t', {'a': 2})


def test_cache_ttl_expires_and_caches_none():
    clock = FakeClock()
    cache = IdempotencyCache(ttl_seconds=10, clock=clock)
    cache.put('t', {}, None)
    assert cache.get('t', {}) == (True, None)       # cached None is still a hit
    clock.t += 11
    assert cache.get('t', {}) == (False, None)


def test_call_many_isolates_each_call():
    with IsolatedCaller({'ok': lambda **k: 1, 'bad': boom}) as c:
        out = c.call_many([('ok', {}), ('bad', {}), ('ok', {})])
    assert [r.ok for r in out] == [True, False, True]


def test_invalid_threshold_rejected():
    with pytest.raises(ValueError):
        IsolatedCaller({}, failure_threshold=0)


def test_tool_raising_timeouterror_is_not_confused_with_deadline():
    def tool(**kw):
        raise TimeoutError('upstream did not answer')

    with IsolatedCaller({'t': tool}, timeout_seconds=5) as c:
        r = c.call('t')
    assert 'upstream did not answer' in r.error
    assert 'timed out after' not in r.error
