import fnmatch
import hashlib
import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


from castor.app import (
    _register_status_line,
    logger,
    mcp,
)
from castor.common import (
    CACHE_KEY_VERSION,
    DEFAULT_SESSION,
    REVIEW_PROMPT,
    SECURITY_REVIEW_PROMPT,
    SKIP_DIRS,
    SKIP_EXTENSIONS,
    SUMMARIZE_TEXT_PROMPT,
    SYSTEM_INSTRUCTION,
    _append_turn,
    _assemble_prompt,
    _cache_get,
    _cache_put,
    _git_diff,
    _is_binary_bytes,
    _load_gitignore,
    _parse_summary,
    _read_bytes,
    _session_prefix,
)
from castor.jobs import (
    JOB_POOL_MAX,
    _Job,
    _evict_jobs,
    _jobs,
    _jobs_lock,
    _run_job,
)
from castor.nim.config import (
    NIM_CACHE_BACKEND,
    NIM_DEFAULT_MAX_INPUT_CHARS,
    NIM_DEFAULT_MAX_TOKENS,
    NIM_DEFAULT_RPM,
    NIM_DEFAULT_TEMPERATURE,
    NIM_MAX_LISTED_MODELS,
    NIM_MERGE_SUMMARIES_PROMPT,
    NIM_PROMPT_THINKING,
    NIM_RATE_WINDOW,
    NIM_RECORDS_TTL_DAYS,
    NIM_SUMMARIZE_CHUNK_CHARS,
    NIM_SUMMARIZE_MAX_CHARS,
    NIM_SUMMARIZE_MAX_TOKENS,
    NIM_SUMMARIZE_MERGE_CHARS,
    NIM_SUMMARIZE_MERGE_POINTS,
    NIM_SUMMARIZE_PART_POINTS,
    NIM_SUMMARIZE_PROMPT,
    NIM_SUMMARIZE_THINKING,
    NIM_SUMMARIZE_WORKERS,
    NIM_VERIFY_MAX_TOKENS,
    _nim_api_keys,
    _nim_base_url,
    _nim_default_model,
    _nim_env,
    _nim_missing_key_error,
    _nim_url_error,
)
from castor.nim.limits import (
    _nim_cooldown_remaining,
    _nim_gate_size,
    _nim_job_max_wait,
    _nim_key_state,
    _nim_lock,
    _nim_stats,
)
from castor.nim.records import (
    _nim_trace_days,
    _nim_usage_entries,
    _nim_usage_line,
)
from castor.nim.transport import (
    _nim_request,
    _nim_stream_fields,
)


# ---------------------------------------------------------------------------
# NVIDIA NIM backend (nim_* tools)
#
# A second, non-agentic backend alongside agy. agy explores the workspace on
# its own; NIM is a plain OpenAI-compatible chat-completions endpoint with no
# filesystem access, so Castor inlines whatever the model needs (files, a
# directory, a diff) and sends it as a single request. Caching, sessions and
# the job table are shared with the agy path; the worker pool is NIM's own.
#
# Configuration is environment-only (set at registration with
# `claude mcp add -e NVIDIA_API_KEY=...`). The API key is read from the
# environment, never accepted as a tool argument, and never logged.
# ---------------------------------------------------------------------------


_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def _job_pool_size() -> int:
    """Background workers: the NIM gate size, at most JOB_POOL_MAX.

    A background agent run has one request in flight, so workers past the
    gate would only queue for slots.
    """
    return min(_nim_gate_size(), JOB_POOL_MAX)


_job_pool = ThreadPoolExecutor(max_workers=_job_pool_size())


def _nim_extract_text(data: Any, model: str) -> tuple[bool, str]:
    """Pull the assistant text out of a chat-completions response.

    Strips closed <think>…</think> blocks some reasoning models emit.
    Returns (ok, text); on failure text is a bracketed error.
    """
    choices = data.get("choices") if isinstance(data, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices else None
    if not isinstance(choice, dict):
        return False, "[NVIDIA NIM returned no choices.]"
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):  # content-part arrays
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    text = (
        _THINK_RE.sub("", content).strip() if isinstance(content, str) else ""
    )
    finish = choice.get("finish_reason")
    if not text:
        return False, (
            f"[NVIDIA NIM returned no content (model={model}, "
            f"finish_reason={finish}). If finish_reason is `length`, raise "
            "max_tokens — reasoning models can spend the whole budget "
            "thinking.]"
        )
    if finish == "length":
        text += (
            "\n\n[Note: response truncated at max_tokens — raise "
            "max_tokens for the full answer.]"
        )
    return True, text


def _nim_thinking_cap(model: str, budget: int) -> dict[str, int]:
    """Payload fields that cap reasoning, for models known to honor them.

    NIM's Nemotron models take `reasoning_budget` (tokens) and otherwise
    think without bound; other models get nothing extra.
    """
    if model.startswith("nvidia/nemotron"):
        return {"reasoning_budget": budget}

    return {}


def _run_nim(
    system: str | None,
    user: str,
    model: str,
    temperature: float,
    max_tokens: int,
    keys: list[str] | None = None,
    thinking: int | None = None,
) -> tuple[bool, str]:
    """Run one chat completion against NVIDIA NIM.

    `keys` pins the call to specific API keys (default: rotate across all).
    `thinking` caps reasoning tokens (default: half of max_tokens, at most
    NIM_PROMPT_THINKING).

    Returns:
        (ok, text). ok is False when text is a bracketed error. Tracking
        success explicitly (rather than sniffing for a leading "[") keeps
        a legitimate answer that starts with a bracket from being treated
        as a failure.
    """
    if not (keys or _nim_api_keys()):
        return False, _nim_missing_key_error()
    if not 0 <= temperature <= 2:
        return False, "[Error: temperature must be between 0 and 2.]"
    if max_tokens <= 0:
        return False, "[Error: max_tokens must be a positive integer.]"
    size = len(user) + len(system or "")
    limit = int(
        _nim_env("CASTOR_NIM_MAX_INPUT_CHARS", NIM_DEFAULT_MAX_INPUT_CHARS)
    )
    if size > limit:
        return False, (
            f"[Error: prompt is {size:,} chars, over the {limit:,}-char "
            "NVIDIA NIM input budget (CASTOR_NIM_MAX_INPUT_CHARS). Inline "
            "less content, or use gemini_prompt with cwd so agy explores "
            "the code itself.]"
        )
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    if thinking is None:
        thinking = min(NIM_PROMPT_THINKING, max_tokens // 2)
    logger.info(
        "nim: chat model=%s prompt_len=%d temperature=%s max_tokens=%d",
        model,
        size,
        temperature,
        max_tokens,
    )
    data, error = _nim_request(
        "POST",
        "/chat/completions",
        {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **_nim_stream_fields(),
            **_nim_thinking_cap(model, thinking),
        },
        keys,
    )
    if error:
        return False, error
    return _nim_extract_text(data, model)


def _summarize_text_nim(text: str) -> str:
    """Session-compaction summarizer backed by NIM (no agy needed)."""
    ok, out = _run_nim(
        SUMMARIZE_TEXT_PROMPT,
        text,
        _nim_default_model(),
        NIM_DEFAULT_TEMPERATURE,
        1024,
    )
    return out if ok else f"[{out}]"


def _nim_cache_key(
    system: str | None,
    user: str,
    model: str,
    temperature: float,
    max_tokens: int,
    thinking: int | None = None,
) -> str:
    """Hash everything that affects a NIM response into a cache key."""
    extra = {} if thinking is None else {"thinking": thinking}
    payload = json.dumps(
        {
            **extra,
            "version": CACHE_KEY_VERSION,
            "backend": NIM_CACHE_BACKEND,
            "base_url": _nim_base_url(),
            "system": system or "",
            "prompt": user,
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dispatch_nim(
    prompt: str,
    files: list[str] | None = None,
    directory: str | None = None,
    raw: bool = False,
    cwd: str | None = None,
    model: str | None = None,
    session: str | None = None,
    use_cache: bool = True,
    temperature: float | None = None,
    max_tokens: int | None = None,
    thinking: int | None = None,
) -> str:
    """Assemble, cache-check, run, and store a single NIM prompt.

    The NIM counterpart of _dispatch: it reuses _assemble_prompt, the
    response cache and the session store, but sends the system
    instruction as a real system message and has no trust/sandbox tiers
    (NIM cannot touch the filesystem). Session calls are stateful and
    never cached.

    Returns:
        The model's response, a cached response, or a bracketed error.
    """
    if session and not cwd:
        return (
            "[Error: cwd is required when using a session (sessions are "
            "project-scoped). Pass the absolute project path.]"
        )
    history = _session_prefix(cwd, session) if session and cwd else None
    user = _assemble_prompt(prompt, True, files, directory, history)
    system = None if raw else SYSTEM_INSTRUCTION
    chosen = model or _nim_default_model()
    temp = NIM_DEFAULT_TEMPERATURE if temperature is None else temperature
    cap = max_tokens or int(
        _nim_env("CASTOR_NIM_MAX_TOKENS", NIM_DEFAULT_MAX_TOKENS)
    )
    cache_key = None
    if use_cache and not session:
        cache_key = _nim_cache_key(system, user, chosen, temp, cap, thinking)
        cached = _cache_get(cache_key)
        if cached is not None:
            logger.info("nim: cache hit for key=%s", cache_key)
            return cached
    ok, response = _run_nim(system, user, chosen, temp, cap, thinking=thinking)
    if not ok:
        logger.warning("nim: returned error response: %s", response)
        return response
    if session and cwd:
        _append_turn(
            cwd, session, prompt, response, summarizer=_summarize_text_nim
        )
    if cache_key is not None:
        _cache_put(cache_key, response, chosen)
    return response


@mcp.tool()
def nim_prompt(
    prompt: str,
    files: list[str] | None = None,
    directory: str | None = None,
    raw: bool = False,
    cwd: str | None = None,
    continue_session: bool = False,
    session: str | None = None,
    model: str | None = None,
    use_cache: bool = True,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """Send a prompt to NVIDIA NIM (free hosted models) and return the reply.

    A second offload target next to gemini_prompt. Unlike agy, NIM is a
    plain chat endpoint: it cannot explore the filesystem, run commands, or
    follow imports. Anything it needs must be passed in `prompt`, `files`
    or `directory`, which Castor inlines. Use it for second opinions,
    review/summarize/explain over content you supply, or to spread load
    when agy's free quota is tight. For open-ended exploration of a
    codebase, prefer gemini_prompt with cwd.

    Privacy: everything inlined is sent to NVIDIA's hosted API. Free-tier
    data-use terms are NVIDIA's; do not send secrets or code the user has
    not cleared for a third party.

    Requires NVIDIA_API_KEY in the MCP server's environment — run
    nim_status to check. The call blocks until the model answers (the
    reply streams; it gives up after CASTOR_NIM_TIMEOUT, default 60s, with
    no new data); use nim_start for long prompts.

    Args:
        prompt: The fully-formed prompt Claude has constructed.
        files: Absolute paths to files to inline into the prompt.
        directory: A directory whose contents should be inlined.
        raw: If True, skip the structured-response system instruction.
        cwd: Absolute project path. Required when using a session.
        continue_session: Resume the project's default session
            ("__default__"). Ignored when `session` is set. Requires cwd.
        session: Name a project-scoped conversation to continue. Castor
            replays its transcript and appends this turn on success.
            Requires cwd. Session calls are never cached. Sessions are
            shared with gemini_prompt, so the same name continues across
            backends.
        model: NIM model id, e.g. "nvidia/nemotron-3-ultra-550b-a55b". Call
            nim_models for what is available. Defaults to
            CASTOR_NIM_MODEL, then Nemotron 3.5 Super.
        use_cache: If True (default), reuse a stored response for an
            identical prompt/model. Session calls are never cached.
        temperature: Sampling temperature 0 to 2 (default 0.2).
        max_tokens: Completion cap (default 4096, CASTOR_NIM_MAX_TOKENS).

    Returns:
        The model's response, or a bracketed error/status string.
    """
    resolved = session or (DEFAULT_SESSION if continue_session else None)
    return _dispatch_nim(
        prompt,
        files=files,
        directory=directory,
        raw=raw,
        cwd=cwd,
        model=model,
        session=resolved,
        use_cache=use_cache,
        temperature=temperature,
        max_tokens=max_tokens,
    )


@mcp.tool()
def nim_start(
    prompt: str,
    files: list[str] | None = None,
    directory: str | None = None,
    raw: bool = False,
    model: str | None = None,
    use_cache: bool = True,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """Start a NVIDIA NIM prompt in the background and return a job id.

    The NIM counterpart of gemini_start: same job table, NIM's own workers,
    and the same castor_poll / castor_jobs to collect results. Use it for
    long prompts or to fan the same question out to NIM and agy at once
    (start both, then compare). Background jobs are session-free. The
    client-side rate limiter and concurrency cap apply across all NIM
    jobs, so a large fan-out queues rather than flooding the endpoint.

    Args:
        prompt: The fully-formed prompt Claude has constructed.
        files: Absolute paths to files to inline into the prompt.
        directory: A directory whose contents should be inlined.
        raw: If True, skip the structured-response system instruction.
        model: NIM model id (see nim_models; default Nemotron 3.5 Super).
        use_cache: If True (default), a cache hit completes the job
            immediately without calling NIM.
        temperature: Sampling temperature 0 to 2 (default 0.2).
        max_tokens: Completion cap (default 4096).

    Returns:
        A message with the job id to pass to castor_poll.
    """
    kwargs: dict[str, Any] = {
        "prompt": prompt,
        "files": files,
        "directory": directory,
        "raw": raw,
        "model": model,
        "use_cache": use_cache,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    job_id = uuid.uuid4().hex[:8]
    with _jobs_lock:
        _evict_jobs()
        _jobs[job_id] = _Job(status="running", started_at=time.time())
    _job_pool.submit(_run_job, job_id, kwargs, _dispatch_nim)
    return (
        f"Started background NIM job {job_id}. Poll it with "
        f'castor_poll("{job_id}") — do other work first, then check back.'
    )


def _review_diff_nim(
    cwd: str, diff: str | None, prompt: str, model: str | None
) -> str:
    """Shared body of nim_review and nim_security_review."""
    if diff is None:
        diff = _git_diff(cwd)
        if diff.startswith("["):
            return diff
    if not diff.strip():
        return (
            "[No diff to review — the working tree is clean or the "
            "provided diff is empty.]"
        )
    return _dispatch_nim(f"{prompt}\n\n[DIFF]\n{diff}", model=model)


@mcp.tool()
def nim_review(
    cwd: str, diff: str | None = None, model: str | None = None
) -> str:
    """Get a free second-opinion correctness review of a diff from NIM.

    The diff is inlined and sent to NVIDIA's hosted API (see the privacy
    note on nim_prompt). When diff is omitted, the uncommitted changes in
    cwd (`git diff HEAD`) are reviewed. Run it alongside gemini_review for
    two independent models, or on its own when agy is rate-limited.
    Complements, does not replace, Claude's own /code-review. Large diffs
    over the NIM input budget are rejected with a clear error.

    Args:
        cwd: Absolute path to the git repository.
        diff: A unified diff to review. When None, uncommitted changes in
            cwd are captured automatically.
        model: NIM model id (see nim_models; default Nemotron 3.5 Super).

    Returns:
        Review findings, or a bracketed error/status string.
    """
    return _review_diff_nim(cwd, diff, REVIEW_PROMPT, model)


@mcp.tool()
def nim_security_review(
    cwd: str, diff: str | None = None, model: str | None = None
) -> str:
    """Get a free security-focused review of a diff from NIM.

    Complements nim_review (correctness-focused, skips security); run both
    for a full pass. Same inputs, privacy note and limits as nim_review.

    Args:
        cwd: Absolute path to the git repository.
        diff: A unified diff to review. When None, uncommitted changes in
            cwd are captured automatically.
        model: NIM model id (see nim_models; default Nemotron 3.5 Super).

    Returns:
        Security findings, or a bracketed error/status string.
    """
    return _review_diff_nim(cwd, diff, SECURITY_REVIEW_PROMPT, model)


@mcp.tool()
def nim_explain_error(
    error: str,
    files: list[str] | None = None,
    model: str | None = None,
) -> str:
    """Get ranked root-cause hypotheses for an error from NIM.

    NIM cannot read the repository, so pass the source files the stack
    trace points at via `files` (absolute paths, inlined). Without them
    the answer rests on the error text alone.

    Args:
        error: The error message or stack trace to diagnose.
        files: Absolute paths to relevant source files to inline.
        model: NIM model id (see nim_models; default Nemotron 3.5 Super).

    Returns:
        Ranked root-cause hypotheses, or a bracketed error string.
    """
    prompt = (
        "Diagnose this error. Return the most probable root causes, "
        "ranked, each with the specific file(s) and line(s) to check and "
        "why. Use only the supplied files and error text; say so when "
        f"they are not enough to be sure.\n\n[ERROR]\n{error}"
    )
    return _dispatch_nim(prompt, files=files, model=model)


@mcp.tool()
def nim_summarize(cwd: str, target: str, model: str | None = None) -> str:
    """Summarize a file or directory with NIM into compact JSON.

    Castor reads `target` (a file or directory under cwd) and inlines it.
    A target over one request's budget (about 240k chars) is split on line
    boundaries, each part summarized in parallel, then merged into one
    summary. Targets over 2,000,000 chars are refused; pass a narrower
    target or use gemini_summarize.

    Args:
        cwd: Absolute path to the project root.
        target: A file or directory path under cwd (absolute, or relative
            to cwd).
        model: NIM model id (see nim_models; default Nemotron 3.5 Super).

    Returns:
        A JSON string with keys: summary, key_points. On parse failure,
        {"summary": <raw>, "key_points": []}. On error, a bracketed
        error string.
    """
    root = Path(cwd).resolve()
    path = Path(target)
    path = (path if path.is_absolute() else root / path).resolve()
    if path != root and root not in path.parents:
        return f"[Error: target must be inside cwd ({root}).]"
    if not path.exists():
        return f"[Error: target not found: {path}]"
    blocks, error = _summary_blocks(path, root, _summary_chunk_chars())
    if error:
        return error
    chunks = _pack_chunks(blocks, _summary_chunk_chars())
    if len(chunks) <= 1:
        raw = _summarize_part(target, chunks[0] if chunks else "", model)
        if raw.startswith("["):  # a summary is a JSON object, never "[..."
            return raw
        return _tidy_summary(_parse_summary(raw))
    return _summarize_in_parts(target, chunks, model)


def _tidy_summary(summary: str) -> str:
    """Split key points the model packed into one string, one per line.

    The merge has returned every bullet in a single newline-joined string;
    this restores one list entry per bullet and drops bullet markers.
    """
    data = json.loads(summary)
    points = data.get("key_points")
    if isinstance(points, str):
        points = [points]
    if not isinstance(points, list):
        return summary
    lines = (
        re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s+", "", line).strip()
        for point in points
        for line in str(point).splitlines()
    )
    data["key_points"] = [line for line in lines if line]
    return json.dumps(data)


def _summary_chunk_chars() -> int:
    """Content chars per summarize request.

    NIM_SUMMARIZE_CHUNK_CHARS, or less when CASTOR_NIM_MAX_INPUT_CHARS is
    lower (leaving room for the prompt).
    """
    limit = int(
        _nim_env("CASTOR_NIM_MAX_INPUT_CHARS", NIM_DEFAULT_MAX_INPUT_CHARS)
    )
    return max(1000, min(NIM_SUMMARIZE_CHUNK_CHARS, limit - 5000))


def _summary_files(path: Path) -> list[Path]:
    """Files a summary covers: `path` itself, or a filtered walk of it.

    The walk skips what _inline_directory skips (gitignore, SKIP_DIRS,
    SKIP_EXTENSIONS) but has no per-file or total size cap.
    """
    if path.is_file():
        return [path]
    patterns = _load_gitignore(path)
    files: list[Path] = []
    for base, dirs, names in os.walk(path):
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in SKIP_DIRS
            and not any(fnmatch.fnmatch(d, p.rstrip("/")) for p in patterns)
        )
        for name in sorted(names):
            file_path = Path(base) / name
            rel = str(file_path.relative_to(path))
            ignored = any(
                fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p)
                for p in patterns
            )
            if not ignored and file_path.suffix not in SKIP_EXTENSIONS:
                files.append(file_path)
    return files


def _summary_blocks(
    path: Path, root: Path, size: int
) -> tuple[list[tuple[str, str]], str | None]:
    """(label, text) blocks for a summary, each at most `size` chars.

    Returns (blocks, None), or ([], error) past NIM_SUMMARIZE_MAX_CHARS;
    the ceiling is checked on file sizes before anything is read.
    """
    files = _summary_files(path)
    total = sum(f.stat().st_size for f in files)
    if total > NIM_SUMMARIZE_MAX_CHARS:
        return [], (
            f"[Error: {path} holds {total:,} bytes, over the "
            f"{NIM_SUMMARIZE_MAX_CHARS:,}-char nim_summarize ceiling. Pass "
            "a narrower target, or use gemini_summarize.]"
        )
    blocks: list[tuple[str, str]] = []
    for file_path in files:
        data = _read_bytes(file_path)
        if data is None or _is_binary_bytes(data):
            continue
        label = str(file_path.relative_to(root))
        text = data.decode("utf-8", errors="replace")
        blocks.extend(_split_block(label, text, size - 200))
    return blocks, None


def _split_block(label: str, text: str, size: int) -> list[tuple[str, str]]:
    """One block, or "label (lines A-B)" blocks of at most `size` chars."""
    if len(text) <= size:
        return [(label, text)]
    pieces: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        pieces.extend(
            (number, line[i : i + size]) for i in range(0, len(line), size)
        )
    blocks: list[tuple[str, str]] = []
    start, buffer = 1, ""
    for number, piece in pieces:
        if buffer and len(buffer) + len(piece) > size:
            blocks.append((f"{label} (lines {start}-{number - 1})", buffer))
            start, buffer = number, ""
        buffer += piece
    last = pieces[-1][0]
    blocks.append((f"{label} (lines {start}-{last})", buffer))
    return blocks


def _pack_chunks(blocks: list[tuple[str, str]], size: int) -> list[str]:
    """Pack labeled blocks, in order, into chunks of at most `size` chars."""
    chunks: list[str] = []
    current = ""
    for label, text in blocks:
        block = f"[FILE: {label}]\n{text}\n"
        if current and len(current) + len(block) > size:
            chunks.append(current)
            current = ""
        current += block
    if current:
        chunks.append(current)
    return chunks


def _summarize_part(
    target: str, content: str, model: str | None, part: str = ""
) -> str:
    """One summarize request (cached); `part` names it, e.g. "2 of 3"."""
    note = (
        f"\nPart {part} of {target}; summarize this part only, in at most "
        f"{NIM_SUMMARIZE_PART_POINTS} key points.\n"
        if part
        else ""
    )
    prompt = (
        f"{NIM_SUMMARIZE_PROMPT.format(target=target)}\n{note}\n"
        f"[CONTENT]\n{content}"
    )
    return _summary_request(prompt, model)


def _summary_request(prompt: str, model: str | None) -> str:
    """Send one summarize or merge prompt with the summary token budget."""
    return _dispatch_nim(
        prompt,
        raw=True,
        model=model,
        max_tokens=NIM_SUMMARIZE_MAX_TOKENS,
        thinking=NIM_SUMMARIZE_THINKING,
    )


def _summarize_in_parts(
    target: str, chunks: list[str], model: str | None
) -> str:
    """Summarize each chunk in parallel, then merge them into one summary.

    Any chunk error is returned for the whole call, before any merge.
    """
    count = len(chunks)
    workers = min(count, NIM_SUMMARIZE_WORKERS)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        raws = list(
            pool.map(
                lambda item: _summarize_part(
                    target, item[1], model, f"{item[0]} of {count}"
                ),
                enumerate(chunks, 1),
            )
        )
    for raw in raws:
        if raw.startswith("["):
            return raw
    parts = [json.loads(_tidy_summary(_parse_summary(raw))) for raw in raws]
    return _merge_summaries(target, parts, model)


def _merge_summaries(target: str, parts: list[dict], model: str | None) -> str:
    """Merge per-part summaries into one {summary, key_points} JSON.

    Past NIM_SUMMARIZE_MERGE_CHARS the merge gets the key points only.
    """
    body = json.dumps(parts, indent=1)
    if len(body) > NIM_SUMMARIZE_MERGE_CHARS:
        points = [{"key_points": p.get("key_points", [])} for p in parts]
        body = json.dumps(points, indent=1)[:NIM_SUMMARIZE_MERGE_CHARS]
    header = NIM_MERGE_SUMMARIES_PROMPT.format(
        target=target, parts=len(parts), points=NIM_SUMMARIZE_MERGE_POINTS
    )
    prompt = f"{header}\n\n[PARTS]\n{body}\n[/PARTS]"
    raw = _summary_request(prompt, model)
    if raw.startswith("["):
        return raw
    return _tidy_summary(_parse_summary(raw))


@mcp.tool()
def nim_models(query: str | None = None) -> str:
    """List the models NVIDIA NIM currently serves.

    The ids to pass as `model` to the nim_* tools. The catalog also holds
    non-chat models (embeddings, rerankers, vision); use `query` to narrow
    it, e.g. "llama", "nemotron", "deepseek".

    Args:
        query: Optional case-insensitive substring filter on model ids.

    Returns:
        One model id per line with a count header, or a bracketed error.
    """
    data, error = _nim_request("GET", "/models")
    if error:
        return error
    entries = data.get("data") if isinstance(data, dict) else None
    ids = sorted(
        {
            e["id"]
            for e in entries or []
            if isinstance(e, dict) and isinstance(e.get("id"), str)
        }
    )
    if not ids:
        return "[NVIDIA NIM reported no models.]"
    total = len(ids)
    if query:
        ids = [i for i in ids if query.lower() in i.lower()]
        if not ids:
            return f'[No NIM models match "{query}" ({total} total).]'
    header = f"NVIDIA NIM models ({len(ids)} of {total}):"
    shown = ids[:NIM_MAX_LISTED_MODELS]
    extra = len(ids) - len(shown)
    more = f"\n…and {extra} more — narrow with `query`." if extra else ""
    return header + "\n" + "\n".join(shown) + more


@mcp.tool()
def nim_usage(days: int = 7) -> str:
    """Summarize NIM usage from Castor's local records (no network call).

    Every chat request is logged locally (model, outcome, seconds, token
    counts; never prompt text), and every nim_agent run leaves a trace.
    Use this to see how much of the free tier Castor spends, which models
    fail, and how long agent runs take. CASTOR_NIM_TRACES=0 turns the
    records off; they are kept 30 days.

    Args:
        days: How many days back to include, today counted (default 7).

    Returns:
        Per-day request counts, failures, tokens and agent runs, then
        per-model totals.
    """
    days = max(1, min(days, NIM_RECORDS_TTL_DAYS))
    entries = _nim_usage_entries(days)
    runs = _nim_trace_days(days)
    if not entries and not runs:
        return f"No NIM usage recorded in the last {days} day(s)."
    lines = [f"NIM usage, last {days} day(s) (local records):"]
    for day in sorted({e["day"] for e in entries} | set(runs), reverse=True):
        rows = [e for e in entries if e["day"] == day]
        lines.append(f"{day}: {_nim_usage_line(rows, runs.get(day))}")
    lines.append("by model:")
    for model in sorted({str(e.get("model")) for e in entries}):
        rows = [e for e in entries if str(e.get("model")) == model]
        lines.append(f"  {model}: {_nim_usage_line(rows, None)}")
    return "\n".join(lines)


@mcp.tool()
def nim_status(verify: bool = True) -> str:
    """Check NVIDIA NIM configuration, key validity and rate-limit state.

    Run before first use or when troubleshooting. With verify=True
    (default) it sends one tiny completion to prove the key and default
    model work — this spends one free-tier request; pass verify=False to
    skip it. The API key itself is never printed.

    Args:
        verify: Whether to make a 1-request completion to validate the
            key and default model.

    Returns:
        A status report starting with READY, NOT CONFIGURED, or NOT READY.
    """
    keys = _nim_api_keys()
    if not keys:
        return (
            f"NOT CONFIGURED: no NVIDIA API key.\n{_nim_missing_key_error()}"
        )
    base = _nim_base_url()
    url_error = _nim_url_error(base)
    if url_error:
        return f"NOT READY: {url_error}"
    model = _nim_default_model()
    key_lines: list[str] = []
    if verify:
        results = [
            _run_nim(
                None,
                "Reply with exactly the word: OK",
                model,
                0,
                NIM_VERIFY_MAX_TOKENS,
                [key],
            )
            for key in keys
        ]
        if not any(ok for ok, _ in results):
            return f"NOT READY: {results[0][1]}"
        key_lines = [
            f"key {i}: {'OK' if ok else 'FAILED ' + text.splitlines()[0]}"
            for i, (ok, text) in enumerate(results, 1)
        ]
    lines = [f"READY — NVIDIA NIM{'' if verify else ' (key not verified)'}"]
    lines.append(f"base_url: {base}")
    lines.append(f"default_model: {model}")
    listing, list_error = _nim_request("GET", "/models")
    if list_error is None and isinstance(listing, dict):
        ids = {
            e.get("id")
            for e in listing.get("data") or []
            if isinstance(e, dict)
        }
        listed = "listed" if model in ids else "NOT in catalog"
        lines.append(f"models_listed: {len(ids)} (default model {listed})")
    rpm = int(_nim_env("CASTOR_NIM_RPM", NIM_DEFAULT_RPM))
    slots = _nim_gate_size()
    max_in = int(
        _nim_env("CASTOR_NIM_MAX_INPUT_CHARS", NIM_DEFAULT_MAX_INPUT_CHARS)
    )
    lines.append(
        f"limits: {rpm} req/{int(NIM_RATE_WINDOW)}s per key, "
        f"concurrency {slots}, max_input {max_in:,} chars, "
        f"job wait {int(_nim_job_max_wait())} s"
    )
    lines.append(f"keys: {len(keys)} (requests rotate across them)")
    lines.extend(key_lines)
    with _nim_lock:
        stats = dict(_nim_stats)
    lines.append(
        "this_session: "
        + " ".join(f"{name}={count}" for name, count in stats.items())
    )
    for i, key in enumerate(keys, 1):
        state = _nim_key_state(key)
        cooldown = _nim_cooldown_remaining(key)
        health = "rejected" if state.dead else "ok"
        lines.append(
            f"key {i}: {health}, requests={state.requests} "
            f"rate_limited={state.rate_limited}, "
            f"cooldown {f'{cooldown:.0f}s' if cooldown else 'none'}"
        )
    return "\n".join(lines)


def _nim_status_line() -> str:
    """One-line NVIDIA NIM summary for gemini_status (no network call)."""
    count = len(_nim_api_keys())
    if not count:
        return "\nnvidia_nim: not configured (optional; see nim_status)"
    noun = "key set" if count == 1 else f"{count} keys set"
    return (
        f"\nnvidia_nim: {noun}, default model {_nim_default_model()} "
        "(run nim_status to verify)"
    )


_register_status_line(_nim_status_line)
