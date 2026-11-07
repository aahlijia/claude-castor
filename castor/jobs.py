import threading
import time
from dataclasses import dataclass
from typing import Any


from castor.common import (
    _nim_patience,
)


# Background-job subsystem. agy print mode blocks for up to PRINT_TIMEOUT;
# these let a long prompt run off-thread so Claude can keep working and
# collect the result later via gemini_poll. Jobs are in-memory only — a
# server restart drops them. Concurrency is capped by the pool so a
# fan-out cannot spawn unbounded agy processes. The pool is sized to the
# NIM gate (see _job_pool_size, built after the NIM helpers).
JOB_POOL_MAX = 8

MAX_JOBS = 50


@dataclass
class _Job:
    """State of one background agy job.

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

# Bracketed responses that are successes, not errors (nim_edit's patch).
_JOB_OK_PREFIXES = ("[nim_edit: OK",)


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


def _run_job(job_id: str, kwargs: dict[str, Any], runner: Any = None) -> None:
    """Worker body: run the prompt, then record the outcome on the job.

    `runner` is the dispatch function to call (default _dispatch for agy;
    nim_start passes _dispatch_nim), so every backend shares one job
    table, one pool, and one gemini_poll.

    Background jobs are session-free by construction (gemini_start takes no
    session param), so concurrent workers never race on a transcript file.
    A bracketed response (agy error/status) is recorded as an "error",
    unless it starts with one of _JOB_OK_PREFIXES.
    """
    _nim_patience.on = True  # a job may wait for NIM; Claude is not blocked
    try:
        if runner is None:
            # agy imports this module, so it is imported here, not at the top.
            from castor.agy import _dispatch as runner
        response = runner(**kwargs)
    except Exception as exc:  # worker must never crash silently
        response = f"[Job crashed: {exc}]"

    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        failed = response.startswith("[") and not response.startswith(
            _JOB_OK_PREFIXES
        )
        job.status = "error" if failed else "done"
        job.response = response
        job.finished_at = time.time()
