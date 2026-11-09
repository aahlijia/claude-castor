import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


# Background-job subsystem, shared by every backend. A long prompt runs
# off-thread so Claude can keep working and collect the result later via
# castor_poll. Jobs are in-memory only: a server restart drops them. Each
# backend owns its worker pool and sizes it to its own limits; none may
# exceed JOB_POOL_MAX workers.
JOB_POOL_MAX = 8

MAX_JOBS = 50


@dataclass
class _Job:
    """State of one background job.

    Attributes:
        status: "running", "done", or "error".
        started_at: Unix time the job was submitted.
        finished_at: Unix time it completed, or None while running.
        response: The result (real response when done, bracketed message
            when error), or None while running.
    """

    status: str
    started_at: float
    finished_at: float | None = None
    response: str | None = None


_jobs: dict[str, _Job] = {}

_jobs_lock = threading.Lock()

# `.on` is True on a background thread: nothing blocks on it, so a backend
# may wait longer for a slot than it would for a foreground call.
_worker_patience = threading.local()

# Bracketed responses that are successes, not errors.
_JOB_OK_PREFIXES: set[str] = set()

# Id prefix -> the poll function for ids that are not in the job table.
_POLLERS: dict[str, Callable[[str], str]] = {}


def _register_job_ok_prefix(prefix: str) -> None:
    """Record a bracketed response prefix that means the job succeeded."""
    _JOB_OK_PREFIXES.add(prefix)


def _register_poller(prefix: str, poll: Callable[[str], str]) -> None:
    """Send poll requests for ids starting with `prefix` to `poll`."""
    _POLLERS[prefix] = poll


def _evict_jobs() -> None:
    """Drop the oldest finished jobs to keep _jobs under MAX_JOBS.

    The caller must hold _jobs_lock. Only finished jobs are evicted, so a
    running job is never dropped out from under a pending poll.
    """
    if len(_jobs) < MAX_JOBS:
        return
    finished = sorted(
        (
            (jid, job)
            for jid, job in _jobs.items()
            if job.finished_at is not None
        ),
        key=lambda kv: kv[1].finished_at or 0,
    )
    for jid, _ in finished:
        if len(_jobs) < MAX_JOBS:
            break
        del _jobs[jid]


def _run_job(
    job_id: str,
    kwargs: dict[str, Any],
    runner: Callable[..., str],
) -> None:
    """Worker body: run the prompt, then record the outcome on the job.

    Every backend shares this one job table and one castor_poll; `runner`
    is the backend's dispatch function.

    Background jobs are session-free by construction, so concurrent workers
    never race on a transcript file. A bracketed response is recorded as an
    "error", unless it starts with a registered ok prefix.
    """
    _worker_patience.on = True
    try:
        response = runner(**kwargs)
    except Exception as exc:  # worker must never crash silently
        response = f"[Job crashed: {exc}]"

    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        failed = response.startswith("[") and not response.startswith(
            tuple(_JOB_OK_PREFIXES)
        )
        job.status = "error" if failed else "done"
        job.response = response
        job.finished_at = time.time()
