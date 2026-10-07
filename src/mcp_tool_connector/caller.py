"""Isolated, idempotent tool calls with a per-tool circuit breaker.

The rule behind this module: **one broken tool must not take the agent down.**
A tool exception becomes a RESULT (with the error spelled out), never a raised
exception. A tool that keeps failing is taken out of rotation automatically instead
of burning one model turn per retry.
"""
from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Mapping


@dataclass
class ToolCall:
    """The outcome of one call. An error is data, not an exception."""

    tool: str
    ok: bool
    result: Any = None
    error: str = ''
    cached: bool = False
    duration_ms: int = 0

    def as_agent_message(self) -> str:
        """How the outcome is shown to the model.

        Failures must be explicit and actionable; otherwise the agent retries the
        same call assuming it got the argument format wrong.
        """
        if self.ok:
            body = self.result if isinstance(self.result, str) else json.dumps(
                self.result, ensure_ascii=False, default=str)
            mark = ' (cached)' if self.cached else ''
            return f'{self.tool}: OK{mark}\n{body}'
        return (f'{self.tool}: FAILED: {self.error}. '
                f'Do not repeat this call; if the data is required, report that the tool failed.')


class IdempotencyCache:
    """Deduplicate calls by (tool, canonical arguments), with a TTL.

    Repeating a READ call wastes a round-trip and can return inconsistent data.
    Repeating a WRITE call is dangerous. So caching is opt-in per tool: only tools
    declared read-only are ever cached.
    """

    def __init__(self, ttl_seconds: float = 300.0, clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl_seconds
        self._clock = clock
        self._store: dict[str, tuple[float, Any]] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(tool: str, args: Mapping[str, Any] | None) -> str:
        canonical = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
        return f'{tool}:{hashlib.sha256(canonical.encode()).hexdigest()[:16]}'

    def get(self, tool: str, args: Mapping[str, Any] | None) -> tuple[bool, Any]:
        """Return (hit, value). A tuple, so a cached `None` is still a hit."""
        k = self.key(tool, args)
        entry = self._store.get(k)
        if entry is None:
            self.misses += 1
            return False, None
        stored_at, value = entry
        if self._clock() - stored_at > self.ttl:
            self._store.pop(k, None)          # expired: never serve stale data
            self.misses += 1
            return False, None
        self.hits += 1
        return True, value

    def put(self, tool: str, args: Mapping[str, Any] | None, value: Any) -> None:
        self._store[self.key(tool, args)] = (self._clock(), value)

    def clear(self) -> None:
        self._store.clear()
        self.hits = self.misses = 0


class IsolatedCaller:
    """Run tools with isolation, timeout, circuit breaker and optional read cache.

    `tools` is {name: callable}. The callable can be anything, including the
    JSON-RPC transport of a real MCP server (see `transport.McpHttpClient`). That
    decoupling is what makes the isolation testable without any server.

    Timeout note: Python cannot kill a running thread. On timeout the call returns
    a failure immediately and counts toward the breaker; the worker thread finishes
    in the background. Keep `max_workers` bounded for that reason.
    """

    def __init__(
        self,
        tools: Mapping[str, Callable[..., Any]],
        read_only: set[str] | frozenset[str] | None = None,
        cache: IdempotencyCache | None = None,
        timeout_seconds: float | None = 30.0,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        max_workers: int = 8,
        clock: Callable[[], float] = time.monotonic,
    ):
        if failure_threshold < 1:
            raise ValueError('failure_threshold must be >= 1')
        self.tools = dict(tools)
        self.read_only = set(read_only or ())
        self.cache = cache
        self.timeout = timeout_seconds
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown_seconds
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='tool')
        self.stats = {'ok': 0, 'failed': 0, 'blocked': 0, 'cached': 0}

    # ------------------------------------------------------------------ breaker
    def is_open(self, tool: str) -> bool:
        until = self._open_until.get(tool)
        if until is None:
            return False
        if self._clock() >= until:
            # cooldown elapsed: half-open, give it one more chance from zero
            self._open_until.pop(tool, None)
            self._failures.pop(tool, None)
            return False
        return True

    def reset(self, tool: str | None = None) -> None:
        if tool is None:
            self._failures.clear()
            self._open_until.clear()
        else:
            self._failures.pop(tool, None)
            self._open_until.pop(tool, None)

    # ------------------------------------------------------------------ call
    def call(self, tool: str, args: Mapping[str, Any] | None = None, **kwargs: Any) -> ToolCall:
        args = {**(args or {}), **kwargs}

        if tool not in self.tools:
            self.stats['failed'] += 1
            available = ', '.join(sorted(self.tools)[:8])
            return ToolCall(tool, False, error=f'unknown tool. Available: {available}')

        if self.is_open(tool):
            self.stats['blocked'] += 1
            return ToolCall(tool, False, error=(
                f'circuit open after {self._failures.get(tool, self.failure_threshold)} failures; '
                f'retry allowed within {int(self.cooldown)}s'))

        use_cache = self.cache is not None and tool in self.read_only
        if use_cache:
            hit, value = self.cache.get(tool, args)
            if hit:
                self.stats['cached'] += 1
                return ToolCall(tool, True, result=value, cached=True)

        started = time.monotonic()
        try:
            if self.timeout is None:
                result = self.tools[tool](**args)
            else:
                future = self._pool.submit(self.tools[tool], **args)
                # wait() instead of result(timeout=): since Python 3.11 the futures
                # TimeoutError IS the builtin TimeoutError, so a tool raising its own
                # TimeoutError would be misreported as our deadline expiring.
                done, _ = wait([future], timeout=self.timeout)
                if not done:
                    return self._fail(tool, f'timed out after {self.timeout}s', started)
                result = future.result()
        except Exception as e:                       # the whole point: tool errors never propagate
            return self._fail(tool, f'{type(e).__name__}: {e}', started)

        self._failures.pop(tool, None)
        self._open_until.pop(tool, None)
        self.stats['ok'] += 1
        if use_cache:
            self.cache.put(tool, args, result)
        return ToolCall(tool, True, result=result, duration_ms=_ms_since(started))

    def call_many(self, calls: list[tuple[str, Mapping[str, Any]]]) -> list[ToolCall]:
        """Several calls under the SAME isolation: one failure does not stop the rest."""
        return [self.call(t, a) for t, a in calls]

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def __enter__(self) -> 'IsolatedCaller':
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def summary(self) -> str:
        s = self.stats
        return (f"ok={s['ok']} failed={s['failed']} blocked={s['blocked']} cached={s['cached']} "
                f"open={sorted(t for t in self._open_until if self.is_open(t))}")

    # ------------------------------------------------------------------ internals
    def _fail(self, tool: str, error: str, started: float) -> ToolCall:
        self._failures[tool] = self._failures.get(tool, 0) + 1
        if self._failures[tool] >= self.failure_threshold:
            self._open_until[tool] = self._clock() + self.cooldown
        self.stats['failed'] += 1
        return ToolCall(tool, False, error=error, duration_ms=_ms_since(started))


def _ms_since(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)
