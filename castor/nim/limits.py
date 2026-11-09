"""NIM concurrency, rate limits and API key rotation."""

import threading
import time
from collections import deque
from dataclasses import dataclass

from castor.jobs import (
    _worker_patience,
)
from castor.nim.config import (
    NIM_DEFAULT_CONCURRENCY_PER_KEY,
    NIM_DEFAULT_RPM,
    NIM_JOB_MAX_WAIT,
    NIM_JOB_WAIT_MAX,
    NIM_JOB_WAIT_MIN,
    NIM_MAX_CONCURRENCY,
    NIM_MAX_COOLDOWN,
    NIM_MAX_WAIT,
    NIM_RATE_WINDOW,
    _nim_api_keys,
    _nim_env,
)


class _SlidingWindowLimiter:
    """Thread-safe strict sliding-window rate limiter (non-blocking).

    At most `limit` reservations succeed in any `window`-second span.
    reserve() never sleeps: it either takes a slot or reports how long
    until one frees up, so the caller decides whether waiting is worth it.
    """

    def __init__(self, limit: int, window: float) -> None:
        self._limit = max(1, limit)
        self._window = window
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def reserve(self) -> float:
        """Take a slot (returns 0.0) or return seconds until one frees."""
        now = time.monotonic()
        with self._lock:
            cutoff = now - self._window
            while self._times and self._times[0] <= cutoff:
                self._times.popleft()
            if len(self._times) < self._limit:
                self._times.append(now)
                return 0.0
            return self._times[0] + self._window - now


@dataclass
class _NimKeyState:
    """Rate-limit and health state for one API key.

    Attributes:
        limiter: Client-side sliding window for this key.
        cooldown_until: Monotonic deadline set by a 429 on this key.
        dead: True once the key was rejected (401/402) while another
            key was still usable; it is skipped for the session.
        requests: Requests sent with this key this session.
        rate_limited: 429s this key has returned this session.
    """

    limiter: _SlidingWindowLimiter
    cooldown_until: float = 0.0
    dead: bool = False
    requests: int = 0
    rate_limited: int = 0


_nim_lock = threading.Lock()


_nim_slots: threading.BoundedSemaphore | None = None


_nim_key_states: dict[str | None, _NimKeyState] = {}


_nim_rotation = 0  # advances per admitted request so keys alternate


_nim_stats = {"requests": 0, "ok": 0, "rate_limited": 0, "errors": 0}


def _nim_gate() -> threading.BoundedSemaphore:
    """Return the shared concurrency semaphore (lazy init)."""
    global _nim_slots
    with _nim_lock:
        if _nim_slots is None:
            _nim_slots = threading.BoundedSemaphore(_nim_gate_size())
        return _nim_slots


def _nim_gate_size() -> int:
    """Simultaneous in-flight NIM requests.

    CASTOR_NIM_CONCURRENCY when set; otherwise
    NIM_DEFAULT_CONCURRENCY_PER_KEY per key, at most NIM_MAX_CONCURRENCY.
    Keys are env-only, so this is fixed for the process.
    """
    keys = max(1, len(_nim_api_keys()))
    scaled = min(NIM_MAX_CONCURRENCY, NIM_DEFAULT_CONCURRENCY_PER_KEY * keys)
    return int(_nim_env("CASTOR_NIM_CONCURRENCY", scaled))


def _nim_job_max_wait() -> float:
    """Background-job wait from CASTOR_NIM_JOB_MAX_WAIT, clamped to 60..3600 s.

    A non-number or non-positive value falls back to NIM_JOB_MAX_WAIT.
    """
    wait = _nim_env("CASTOR_NIM_JOB_MAX_WAIT", NIM_JOB_MAX_WAIT)
    return min(NIM_JOB_WAIT_MAX, max(NIM_JOB_WAIT_MIN, wait))


def _nim_max_wait() -> float:
    """How long this thread may wait for a key or slot.

    The job wait on a background-job worker (_run_job marks it),
    NIM_MAX_WAIT otherwise: foreground calls block Claude, so fail fast.
    """
    return (
        _nim_job_max_wait()
        if getattr(_worker_patience, "on", False)
        else (NIM_MAX_WAIT)
    )


def _nim_key_state(key: str | None) -> _NimKeyState:
    """Return the state for `key`, creating it on first use."""
    with _nim_lock:
        state = _nim_key_states.get(key)
        if state is None:
            rpm = int(_nim_env("CASTOR_NIM_RPM", NIM_DEFAULT_RPM))
            state = _NimKeyState(_SlidingWindowLimiter(rpm, NIM_RATE_WINDOW))
            _nim_key_states[key] = state
        return state


def _nim_count(stat: str, key: str | None = None) -> None:
    """Increment a session counter (and the key's own, when given)."""
    state = _nim_key_state(key)
    with _nim_lock:
        _nim_stats[stat] += 1
        if stat == "requests":
            state.requests += 1
        elif stat == "rate_limited":
            state.rate_limited += 1


def _nim_set_cooldown(seconds: float, key: str | None) -> None:
    """Hold `key` back for `seconds` (after a 429); other keys continue."""
    until = time.monotonic() + min(seconds, NIM_MAX_COOLDOWN)
    state = _nim_key_state(key)
    with _nim_lock:
        state.cooldown_until = max(state.cooldown_until, until)


def _nim_cooldown_remaining(key: str | None) -> float:
    """Seconds left on `key`'s rate-limit cooldown (0.0 when clear)."""
    state = _nim_key_state(key)
    with _nim_lock:
        return max(0.0, state.cooldown_until - time.monotonic())


def _nim_pick_key(keys: list[str | None]) -> tuple[str | None, float]:
    """Reserve a slot on the next usable key, rotating across `keys`.

    Rejected keys are skipped unless every key was rejected. Returns
    (key, 0.0) when admitted, else (None, seconds until a key frees up).
    """
    global _nim_rotation
    live = [k for k in keys if not _nim_key_state(k).dead] or keys
    with _nim_lock:
        offset = _nim_rotation % len(live)
    shortest: float | None = None
    for key in live[offset:] + live[:offset]:
        state = _nim_key_state(key)
        wait = _nim_cooldown_remaining(key) or state.limiter.reserve()
        if wait <= 0:
            with _nim_lock:
                _nim_rotation += 1
            return key, 0.0
        shortest = wait if shortest is None else min(shortest, wait)
    return None, shortest or 0.001


def _nim_admit(
    keys: list[str | None] | None = None,
) -> tuple[str | None, str | None]:
    """Wait for a usable key and rate-limit slot, within _nim_max_wait().

    Returns (key, None) once admitted, or (None, bracketed error) when the
    wait would run past it: failing fast beats blocking Claude for minutes.
    """
    keys = keys or _nim_api_keys() or [None]
    deadline = time.monotonic() + _nim_max_wait()
    while True:
        key, wait = _nim_pick_key(keys)
        if wait <= 0:
            return key, None
        if time.monotonic() + wait > deadline:
            across = f" across {len(keys)} keys" if len(keys) > 1 else ""
            return None, (
                "[NVIDIA NIM is rate-limited: try again in "
                f"~{int(wait) + 1}s. Client limit is "
                f"{int(_nim_env('CASTOR_NIM_RPM', NIM_DEFAULT_RPM))} "
                f"requests/min per key{across} (CASTOR_NIM_RPM).]"
            )
        time.sleep(min(wait, 1.0))


def _reset_limits() -> None:
    """Drop the gate, key limiters, cooldowns, rotation and counters."""
    global _nim_slots, _nim_rotation
    with _nim_lock:
        _nim_slots = None
        _nim_key_states.clear()
        _nim_rotation = 0
        for name in _nim_stats:
            _nim_stats[name] = 0
