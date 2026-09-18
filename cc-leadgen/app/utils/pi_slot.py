"""Redis-backed slot pool for serializing pi subprocess invocations.

Why this exists
---------------
The pi subprocess serializes internally on a global lock — only one invocation
can hold it at a time, even though we spawn one subprocess per Celery task.
When N>1 tasks fire concurrently, they all wait their turn inside the
subprocess, but the *outer* `subprocess.run` call has a 300 s wall-clock
timeout (DEFAULT_TIMEOUT_S in mockup_generator.py). The first task gets
through, the rest time out before the lock cycles to them.

This module gives us a Redis-backed semaphore so we can bound concurrency at
the application layer, before the subprocess is even spawned. A Redis list
acts as the pool: it's pre-filled with N tokens at worker startup; each task
does a blocking BLPOP to acquire a token, runs the pi subprocess, and LPUSHes
the token back. With N=1, calls are strictly serialized; raising N trades
serialization for higher throughput at the cost of pi-internal lock contention.

Token model
-----------
Tokens are opaque strings ("1") pushed onto a Redis list. Acquire = BLPOP
(blocking pop from the head). Release = LPUSH (push back to the head).
This is the classic "Redis semaphore" pattern. On a worker crash mid-task,
the held token is lost — but the worker_ready signal re-initializes the pool
to N tokens on restart, so the pool self-heals.

Layout
------
Pool key  : "pi:subprocess:slots"  (Redis list)
Bootstrap : worker_ready signal in celery_app.py calls init_slots(N)
Safety net: pi_slot() context manager also calls init_slots() lazily, so if
            Redis was flushed mid-run the pool refills.

Usage
-----
    from app.utils.pi_slot import pi_slot

    with pi_slot(timeout=DEFAULT_TIMEOUT_S):
        proc = subprocess.run([PI_BIN, "-p", prompt, ...],
                              timeout=DEFAULT_TIMEOUT_S)
"""
from __future__ import annotations

import logging  # noqa: F401  (kept for tests that monkeypatch stdlib logging)
from contextlib import contextmanager
from typing import Iterator

import redis
from redis.exceptions import RedisError

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)

PI_SLOT_KEY = "pi:subprocess:slots"


def _client() -> redis.Redis:
    """Return a Redis client dedicated to the slot pool.

    Note on socket_timeout: Celery configures its broker Redis client with a
    short socket_timeout (5 s) so that broker hiccups fail fast instead of
    hanging the worker. We reuse the same Redis URL but explicitly override
    socket_timeout to None here because BLPOP is designed to block for the
    full timeout duration (up to 300 s for our slot acquire). Without this
    override, our BLPOP would be killed by the inherited socket_timeout long
    before the slot's logical timeout, causing spurious "slot_timeout" errors.

    socket_connect_timeout is also set to None so a slow first connect does
    not cause flakes (the connection pool reconnects lazily).
    """
    return redis.Redis.from_url(
        get_settings().redis_url,
        decode_responses=True,
        socket_timeout=None,
        socket_connect_timeout=None,
    )


def init_slots(n: int) -> int:
    """Fill the slot pool to exactly N tokens.

    Atomic via Lua. Multiple concurrent callers (worker_ready signal + N
    worker tasks all calling lazily) will all converge on the same final
    size: each call adds only the delta between the current LLEN and N.

    Idempotent. If the pool already has >= N tokens, we leave it alone —
    we never shrink because that would steal tokens from in-flight tasks.

    Returns the resulting pool size.
    """
    if n < 1:
        raise ValueError(f"pi_max_concurrent must be >= 1, got {n}")

    try:
        r = _client()
        result = r.eval(_INIT_SLOTS_LUA, 1, PI_SLOT_KEY, n)
        result = int(result)
        if result == n:
            # Either we just initialized to N, or it was already at N.
            log.info("pi_slot_pool_initialized", max_concurrent=n, total=result)
        else:
            log.info("pi_slot_pool_present", current=result, requested=n)
        return result
    except RedisError as e:
        log.error("pi_slot_init_failed", error=str(e))
        raise


# Atomic check-and-fill: read LLEN, push only the delta up to ARGV[1].
# Multiple concurrent callers converge on the same final size.
_INIT_SLOTS_LUA = """
local current = redis.call('LLEN', KEYS[1])
local n = tonumber(ARGV[1])
if current < n then
    for i = 1, n - current do
        redis.call('LPUSH', KEYS[1], '1')
    end
    return n
end
return current
"""


def acquire_slot(timeout: int) -> str | None:
    """Blocking acquire. Returns the token, or None on timeout."""
    import time as _t
    t0 = _t.time()
    try:
        r = _client()
        res = r.blpop(PI_SLOT_KEY, timeout=timeout)
        elapsed = _t.time() - t0
        log.info("pi_slot_acquired", waited_s=round(elapsed, 2), result=bool(res))
        return res[1] if res else None
    except RedisError as e:
        elapsed = _t.time() - t0
        log.error("pi_slot_acquire_failed", error=str(e), waited_s=round(elapsed, 2))
        return None


def release_slot(token: str) -> None:
    """Return a token to the pool. Best-effort; logs on Redis errors."""
    try:
        r = _client()
        r.lpush(PI_SLOT_KEY, token)
    except RedisError as e:
        log.error("pi_slot_release_failed", error=str(e), token=token)


@contextmanager
def pi_slot(timeout: int = 0) -> Iterator[None]:
    """Context manager: acquire a pi subprocess slot for the duration of the
    block, release on exit (including exceptions).

    Raises TimeoutError if no slot becomes available within `timeout` seconds.
    Other Redis errors propagate — caller decides whether to retry or fail.

    The slot pool must be initialised by the worker_ready signal in
    celery_app.py before any task acquires. We do not lazy-init here because
    LLEN==0 is ambiguous (could mean "never initialised" or "tokens all
    held by other in-flight tasks") and a lazy bootstrap would race with
    other waiters, growing the pool beyond the configured max_concurrent.
    """
    token = acquire_slot(timeout=timeout)
    if token is None:
        raise TimeoutError(f"No pi subprocess slot available within {timeout}s")

    try:
        yield
    finally:
        release_slot(token)


def current_available() -> int:
    """Read-only: how many slots are currently free. Useful for diagnostics."""
    try:
        return _client().llen(PI_SLOT_KEY)
    except RedisError as e:
        log.error("pi_slot_inspect_failed", error=str(e))
        return -1
