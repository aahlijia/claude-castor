import json
import time
from castor.app import (
    mcp,
)
from castor.common import (
    DEFAULT_SESSION,
    _cache_dir,
    _delete_session,
    _load_session,
    _session_dir,
)
from castor.jobs import (
    _POLLERS,
    _jobs,
    _jobs_lock,
)


"""MCP tools that serve every backend: background jobs, sessions, cache."""


@mcp.tool()
def castor_poll(job_id: str) -> str:
    """Check a background job (gemini_start, nim_*_start) or a run.

    Args:
        job_id: The id returned by gemini_start or a *_start tool, or a
            run id (`run-...`) from castor_run_start. A run returns JSON:
            progress while it runs, then its result.

    Returns:
        While running, a bracketed status with elapsed seconds. When done,
        the job's response. On error, a bracketed message. For an unknown
        id, a bracketed not-found note.
    """
    for prefix, poll in _POLLERS.items():
        if job_id.startswith(prefix):
            return poll(job_id)

    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return (
                f"[Unknown job {job_id} — it may have been evicted or "
                "never existed. Run castor_jobs to list current jobs.]"
            )
        status, response = job.status, job.response
        started, finished = job.started_at, job.finished_at

    if status == "running":
        elapsed = int(time.time() - started)
        return (
            f"[Job {job_id} still running — {elapsed}s elapsed. "
            "Poll again shortly.]"
        )

    took = int((finished or time.time()) - started)
    if status == "error":
        return f"[Job {job_id} failed after {took}s]\n{response}"
    return response or "[Job returned no response]"


@mcp.tool()
def castor_jobs() -> str:
    """List background jobs and their status (runs: castor_runs).

    Returns:
        One line per job (id, status, age/duration), newest last, or a
        note when there are none.
    """
    with _jobs_lock:
        if not _jobs:
            return "No background jobs."
        now = time.time()
        lines = []
        for jid, job in sorted(_jobs.items(), key=lambda kv: kv[1].started_at):
            secs = int((job.finished_at or now) - job.started_at)
            lines.append(f"{jid}  {job.status:<8} {secs}s")
    return "\n".join(lines)


@mcp.tool()
def castor_reset(cwd: str | None = None) -> str:
    """Clear a project's default session so the next call starts fresh.

    Deletes the reserved "__default__" session (the one continue_session
    resumes) for cwd. Named sessions are untouched — manage those with
    castor_sessions and castor_session_delete. Sessions are project-
    scoped, so cwd is required to know which project's default to clear.

    Args:
        cwd: Absolute project root whose default session to reset.
    """
    if not cwd:
        return (
            "[Error: cwd is required — sessions are project-scoped. Pass "
            "the absolute project root whose default session to reset.]"
        )
    if _delete_session(cwd, DEFAULT_SESSION):
        return (
            "Default session cleared — the next continue_session call "
            "starts fresh."
        )
    return "No default session to clear — the next call already starts fresh."


@mcp.tool()
def castor_sessions(cwd: str) -> str:
    """List the client-side sessions stored for a project.

    Evicts any sessions past the TTL, then lists the rest with turn count
    and last-updated time.

    Args:
        cwd: Absolute path to the project root.

    Returns:
        One line per session, or a note when there are none.
    """
    sessions_dir = _session_dir(cwd)
    if not sessions_dir.exists():
        return "[No sessions for this project.]"
    lines = []
    for path in sorted(sessions_dir.glob("*.json")):
        session = _load_session(cwd, path.stem)
        if not session:
            continue
        turns = len(session.get("turns") or [])
        compacted = " +summary" if session.get("summary") else ""
        updated = session.get("updated_at", 0.0)
        when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(updated))
        lines.append(f"{path.stem}  turns={turns}{compacted}  updated={when}")
    if not lines:
        return "[No sessions for this project.]"
    return "\n".join(lines)


@mcp.tool()
def castor_session_show(cwd: str, session: str) -> str:
    """Return a session's stored transcript as JSON.

    Args:
        cwd: Absolute path to the project root.
        session: The session name to inspect.

    Returns:
        The session JSON (summary + turns), or a bracketed not-found note.
    """
    data = _load_session(cwd, session)
    if not data:
        return f"[No such session '{session}' for this project.]"
    return json.dumps(data, indent=2)


@mcp.tool()
def castor_session_delete(cwd: str, session: str) -> str:
    """Delete a stored session.

    Args:
        cwd: Absolute path to the project root.
        session: The session name to delete.

    Returns:
        A confirmation, or a bracketed not-found note.
    """
    if _delete_session(cwd, session):
        return f"Deleted session '{session}'."
    return f"[No such session '{session}' for this project.]"


@mcp.tool()
def castor_cache_clear() -> str:
    """Delete all cached responses, from every backend.

    gemini_prompt and nim_prompt reuse a stored response when the same
    prompt is re-sent against an unchanged repo (matched by git HEAD +
    working-tree state). Clear the cache to force fresh runs, for example
    after changing a backend's config or default model, or to reclaim
    disk. Run records and NIM usage logs are kept.
    """
    cache_dir = _cache_dir()
    if not cache_dir.exists():
        return "Cache is already empty — nothing to clear."

    removed = 0
    for entry in cache_dir.glob("*.json"):
        try:
            entry.unlink()
            removed += 1
        except OSError:
            pass

    return f"Cleared {removed} cached response(s) from {cache_dir}."
