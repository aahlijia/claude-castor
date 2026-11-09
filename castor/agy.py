from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import time
import uuid
from typing import Any


from castor.app import (
    _status_lines,
    logger,
    mcp,
)
from castor.common import (
    AUTH_HINT,
    DEFAULT_SESSION,
    DOCUMENT_PROMPT,
    INDEX_JSON_SCHEMA,
    INDEX_PROMPT,
    INSTALL_CMD,
    PRINT_TIMEOUT,
    REVIEW_PROMPT,
    SECURITY_REVIEW_PROMPT,
    SEMANTIC_SEARCH_JSON_SCHEMA,
    SEMANTIC_SEARCH_MAX_HITS,
    SEMANTIC_SEARCH_PROMPT,
    SUBPROCESS_TIMEOUT,
    SUMMARIZE_JSON_SCHEMA,
    SUMMARIZE_PROMPT,
    SUMMARIZE_TEXT_PROMPT,
    _INDEX_KEYS,
    _append_turn,
    _assemble_prompt,
    _cache_put,
    _cache_size_mb,
    _extract_json_blob,
    _fallback_text,
    _get_git_sha,
    _git_diff,
    _load_project_state,
    _ndjson_result_event,
    _parse_ndjson_result,
    _parse_summary,
    _save_project_state,
    _session_prefix,
    _try_cache,
)
from castor.jobs import (
    JOB_POOL_MAX,
    _Job,
    _evict_jobs,
    _jobs,
    _jobs_lock,
    _run_job,
)


_AUTH_MARKERS = (
    "authentication required",
    "please sign in",
    "not signed in",
    "login required",
)


def _needs_auth(text: str) -> bool:
    """True if agy output indicates the user is not signed in."""
    low = text.lower()
    return any(marker in low for marker in _AUTH_MARKERS)


# agy's stderr line when headless print mode refuses a tool permission.
_DENIAL_MARKER = "auto-denied"


DENIED_MSG = (
    "[Antigravity produced no output: it tried to run a shell command, "
    "and headless agy auto-denies commands it cannot prompt for. Retry "
    "with sandbox=True (read commands allowed, workspace read-only) or "
    "trust=True (full access), with cwd set to the project root.]"
)


def _empty_output_error(
    stdout: str,
    stderr: str,
    ndjson: bool,
) -> str | None:
    """Explain an agy run that exited 0 without an answer.

    agy 1.3+ ends a print-mode turn with empty output and exit code 0
    when it auto-denies a tool permission, so the exit code alone can't
    tell a denial from a real answer. In text mode the only trace is a
    stderr line; in stream-json mode the result event carries an empty
    response and a `denied_actions` list.

    Args:
        stdout: agy's stdout.
        stderr: agy's stderr.
        ndjson: True when the run used --output-format stream-json.

    Returns:
        A bracketed error string, or None when stdout holds an answer.
    """
    if ndjson:
        result = _ndjson_result_event(stdout) or {}
        response = result.get("response")
        has_text = isinstance(response, str) and response.strip()
        if has_text or result.get("structured_output") is not None:
            return None
        denied = bool(result.get("denied_actions"))
    elif stdout.strip():
        return None
    else:
        denied = False

    if denied or _DENIAL_MARKER in stderr:
        return DENIED_MSG

    detail = stderr.strip()
    return "[No response from Antigravity]" + (f"\n{detail}" if detail else "")


def _json_schema_args(schema: str | None) -> list[str]:
    """Return the agy flags that enforce structured JSON output.

    agy only applies --json-schema to the terminal event of the
    stream-json output format, so the two flags travel together. When
    schema is set, this switches the run from plain text to NDJSON —
    see _parse_ndjson_result for how the terminal result event (and its
    structured_output field) is recovered from that stream.

    Args:
        schema: A JSON Schema string to enforce, or None to leave the
            command in normal text mode.

    Returns:
        ["--output-format", "stream-json", "--json-schema", schema], or
        [] when schema is falsy.
    """
    if not schema:
        return []
    return ["--output-format", "stream-json", "--json-schema", schema]


def _build_agy_cmd(
    prompt: str,
    trust: bool,
    cwd: str | None,
    add_dirs: list[str] | None,
    model: str | None,
    sandbox: bool,
    effort: str | None = None,
    agent: str | None = None,
    json_schema: str | None = None,
) -> list[str]:
    """Assemble the agy print-mode command with the requested flags.

    agy's `--print` takes the prompt as its own inline argv value —
    it has no stdin fallback (`agy --print ""` errors rather than
    reading stdin). The prompt must therefore sit immediately after
    `--print`, and `--print-timeout` must be passed as a single
    `=`-joined token so it can't be swallowed as --print's value
    instead of the real prompt.

    agy's workspace is registered via `--add-dir`, not inherited from
    the subprocess's working directory — setting the subprocess cwd
    alone leaves agy rooted in its own default scratch directory, blind
    to the actual project. So `cwd` is passed to agy as its own
    `--add-dir` entry (in addition to being the subprocess's OS-level
    working directory, set separately in `_run_agy`).

    Access tier is mutually exclusive: trust (--dangerously-skip-
    permissions) wins over sandbox. Multi-turn context is handled by
    Castor's own transcript replay, not agy's --continue.

    The sandbox tier passes --sandbox together with
    --dangerously-skip-permissions. Since agy 1.3, headless print mode
    auto-denies every shell command outside trust (allow-rules in
    settings.json did not match either, tested on agy 1.4.7), and one
    denial ends the turn with empty output and exit 0. Auto-approving
    inside --sandbox lets agy run find/grep/git while the OS sandbox
    keeps the workspace read-only. Confirmed live on 1.4.7: writes to
    the workspace fail with "Operation not permitted", but writes to
    the home directory and network access still succeed, so this tier
    protects the project, not the whole machine.

    `--disable-slash-commands` is always passed: Castor's prompts
    routinely inline arbitrary file/directory content, and inlined
    text that happens to start with `/` at the start of a line must
    never be interpreted by agy as a skill invocation rather than
    inert content handed over for analysis.

    `agent`, like `model`, is passed straight through with no
    server-side validation — an unknown agent name is rejected by agy
    itself. `json_schema` is internal-only (no public tool param sets
    it directly): when set, it also switches `--output-format` to
    `stream-json`, since agy only enforces `--json-schema` on the
    terminal result event of that format — see _json_schema_args.

    Note: agy's `--mode` flag (accept-edits/plan) was spiked and
    deliberately left out of this command builder entirely.
    `accept-edits` hangs headless `--print` calls waiting for an
    approval prompt that can never be answered non-interactively.
    `plan` doesn't hang, but adds no real capability: without
    trust/sandbox it's a silent no-op (any tool request auto-denied),
    and with trust it behaves identically to omitting `--mode`
    entirely. Neither is worth the added API surface — see the design
    doc's item #10 for the full spike writeup.

    Returns:
        The full argv list for the agy subprocess.
    """
    cmd = ["agy", "--print", prompt, f"--print-timeout={PRINT_TIMEOUT}"]
    cmd.append("--disable-slash-commands")
    if trust:
        cmd.append("--dangerously-skip-permissions")
    elif sandbox:
        cmd.extend(["--sandbox", "--dangerously-skip-permissions"])
    if cwd:
        cmd.extend(["--add-dir", cwd])
    for directory in add_dirs or []:
        cmd.extend(["--add-dir", directory])
    if model:
        cmd.extend(["--model", model])
    if effort:
        cmd.extend(["--effort", effort])
    if agent:
        cmd.extend(["--agent", agent])
    cmd.extend(_json_schema_args(json_schema))
    return cmd


def _run_agy(
    prompt: str,
    trust: bool = False,
    cwd: str | None = None,
    add_dirs: list[str] | None = None,
    model: str | None = None,
    sandbox: bool = False,
    effort: str | None = None,
    agent: str | None = None,
    json_schema: str | None = None,
) -> str:
    """Run an agy print-mode prompt and return its stdout.

    The prompt is passed as --print's inline argv value — agy's print
    mode has no stdin fallback, so prompt size is bounded by the
    command-line argument limit (ARG_MAX; ~1MB on macOS), not unbounded
    as stdin piping would allow.

    Args:
        prompt: The fully-assembled prompt to send to agy.
        trust: If True, pass --dangerously-skip-permissions so agy
            auto-approves tool actions (writes, commands).
        cwd: Working directory for the subprocess; roots agy's
            workspace so it explores the correct project.
        add_dirs: Extra directories to grant agy read access to via
            repeated --add-dir flags.
        model: If set, pass --model <model> to select the agy model.
            Call gemini_models for the available names.
        sandbox: If True, pass --sandbox with auto-approval so agy can
            run read commands while the workspace stays read-only (see
            _build_agy_cmd). Ignored when trust is True (trust is the
            broader grant).
        effort: If set, pass --effort <effort> to select agy's reasoning
            effort (low/medium/high/xhigh/max). Not validated
            server-side; an invalid value is rejected by agy itself.
        agent: If set, pass --agent <agent> to route the prompt to one
            of agy's built-in specialized agents (see gemini_agents).
            Not validated server-side; an unknown name is rejected by
            agy itself.
        json_schema: Internal-only. If set, enforce structured JSON
            output via --json-schema (and --output-format stream-json),
            returning NDJSON — see _parse_ndjson_result. Not a public
            tool parameter; workflow tools set it with a fixed schema.

    Returns:
        agy's stdout, or a bracketed error/status string.
    """
    cmd = _build_agy_cmd(
        prompt,
        trust=trust,
        cwd=cwd,
        add_dirs=add_dirs,
        model=model,
        sandbox=sandbox,
        effort=effort,
        agent=agent,
        json_schema=json_schema,
    )

    logged_cmd = [
        f"<prompt: {len(prompt)} chars>" if arg is prompt else arg
        for arg in cmd
    ]
    logger.info(
        "run_agy: executing cmd=%s cwd=%s",
        logged_cmd,
        cwd,
    )
    t0 = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
            cwd=cwd,
        )
    except FileNotFoundError:
        logger.error("run_agy: agy CLI not found")
        return f"[Error: `agy` CLI not found. Install: {INSTALL_CMD}]"
    except subprocess.TimeoutExpired:
        logger.error(f"run_agy: timed out after {SUBPROCESS_TIMEOUT}s")
        return (
            "[Error: Antigravity timed out. Try a narrower scope, "
            "specific files, or fewer directories.]"
        )

    duration = time.time() - t0
    logger.info(
        "run_agy: finished in %.2fs with returncode=%d "
        "stdout_len=%d stderr_len=%d",
        duration,
        result.returncode,
        len(result.stdout),
        len(result.stderr),
    )

    combined = result.stdout + result.stderr
    if _needs_auth(combined):
        logger.warning("run_agy: user needs authentication")
        return f"[Not signed in to Antigravity. {AUTH_HINT}]"

    if result.returncode != 0 and result.stderr.strip():
        logger.error(
            f"run_agy: subprocess failed with stderr: {result.stderr.strip()}"
        )
        return f"[Antigravity error]\n{result.stderr.strip()}"

    empty = _empty_output_error(
        result.stdout,
        result.stderr,
        ndjson=bool(json_schema),
    )
    if empty is not None:
        logger.warning(f"run_agy: no usable output: {empty}")
        return empty

    res = result.stdout.strip()
    logger.info(f"run_agy: returning result (length={len(res)})")
    return res


def _list_models() -> list[str]:
    """Return agy's model names (raw lines), or [] on any failure."""
    try:
        result = subprocess.run(
            ["agy", "models"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    out = result.stdout.strip()
    if not out or _needs_auth(out + result.stderr):
        return []
    return out.splitlines()


def _cheapest_model() -> str | None:
    """Pick a cheap/fast model token from agy's list, or None.

    Matches only against the first token of each line (the model id
    itself, e.g. "gemini-3.6-flash-low") — never the human-readable name
    column that follows it — so a display name that happens to contain a
    needle as a substring can't produce a false match. Needles are
    ordered to prefer an explicit "-low" effort suffix first, since that's
    the actual "cheapest" signal; the family-name needles that follow are
    a fallback for naming schemes without effort suffixes.

    Returns None when no light tier is found, so the caller falls back to
    agy's default model. Kept as a fallback for callers that want a
    specific lightweight model rather than a low-effort pass of the
    default one (see --effort, preferred for that case).
    """
    for needle in ("-low", "flash-lite", "lite", "8b"):
        for line in _list_models():
            tokens = line.replace(",", " ").split()
            if not tokens:
                continue
            model_id = tokens[0]
            if needle in model_id.lower():
                return model_id.strip()
    return None


def _summarize_text(text: str, effort: str = "low") -> str:
    """Summarize arbitrary text into prose (read-only agy call).

    The internal primitive behind session compaction (FR-F4). Runs a
    low-effort pass of agy's default model to conserve free-tier quota,
    rather than overriding to a specific lightweight model.

    Args:
        text: The text to compress.
        effort: agy reasoning effort to request (low/medium/high/
            xhigh/max). Defaults to "low" for cheap/fast compaction.

    Returns:
        A prose summary, or a bracketed error/status string.
    """
    prompt = f"{SUMMARIZE_TEXT_PROMPT}\n\n{text}"
    return _run_agy(prompt, effort=effort)


def _parse_hits(raw: str) -> str:
    """Parse agy's semantic-search response into a JSON array of hits.

    First tries the schema-enforced NDJSON path (_parse_ndjson_result) —
    raw is stream-json output produced with SEMANTIC_SEARCH_JSON_SCHEMA,
    whose root object wraps the array under a "hits" key (agy's
    --json-schema requires a root object, not a root array — see
    SEMANTIC_SEARCH_JSON_SCHEMA). Falls back to extracting a bare JSON
    array from raw the old way (best-effort) when that path comes back
    empty. That fallback text is narrowed via _fallback_text
    first when raw is NDJSON, so a schema miss degrades to the old
    plain-text failure surface rather than the full multi-line
    transcript. Falls back to {"raw_markdown": raw} when neither path
    yields a list, so the caller can tell parsing failed.
    """
    structured = _parse_ndjson_result(raw)
    if isinstance(structured, dict) and isinstance(
        structured.get("hits"), list
    ):
        return json.dumps(structured["hits"])
    text = _fallback_text(raw)
    try:
        data = json.loads(_extract_json_blob(text))
    except (ValueError, TypeError):
        return json.dumps({"raw_markdown": text})
    if not isinstance(data, list):
        return json.dumps({"raw_markdown": text})
    return json.dumps(data)


def _dispatch(
    prompt: str,
    files: list[str] | None = None,
    directory: str | None = None,
    raw: bool = False,
    trust: bool = False,
    cwd: str | None = None,
    add_dirs: list[str] | None = None,
    model: str | None = None,
    sandbox: bool = False,
    use_cache: bool = True,
    session: str | None = None,
    effort: str | None = None,
    agent: str | None = None,
    json_schema: str | None = None,
) -> str:
    """Assemble, cache-check, run, and store a single agy prompt.

    The shared core of gemini_prompt and the workflow tools
    (gemini_index, gemini_review, ...). Enforces the trust/cwd guard, the
    side-effect-free cache gate, and session replay/append in one place so
    every caller handles caching and sessions identically.

    Args:
        session: When set (and cwd is given), replay that project-scoped
            session's transcript as context and append this turn to it on
            success. Session calls are stateful, so they are never cached.
        effort: agy reasoning effort (low/medium/high/xhigh/max),
            passed straight through to --effort. Included in the cache
            key alongside model, since it affects the response.
        agent: agy agent name, passed straight through to --agent.
            Included in the cache key alongside model/effort.
        json_schema: Internal-only structured-output schema (see
            _run_agy). Included in the cache key, since it changes the
            response format (NDJSON vs. plain text).

    Returns:
        agy's response, a cached response, or a bracketed error string.
    """
    if trust and not cwd:
        logger.warning("dispatch: trust=True but cwd is missing")
        return (
            "[Error: cwd is required when trust=True so agy's workspace "
            "is rooted in the correct project directory. Pass the "
            "absolute path of the user's project.]"
        )

    in_session = bool(session and cwd)
    logger.info(
        "dispatch: prompt_len=%d files=%s directory=%s raw=%s trust=%s "
        "sandbox=%s model=%s effort=%s agent=%s cwd=%s session=%s",
        len(prompt),
        files,
        directory,
        raw,
        trust,
        sandbox,
        model or "default",
        effort or "default",
        agent or "default",
        cwd,
        session,
    )

    history = _session_prefix(cwd, session) if in_session else None
    assembled = _assemble_prompt(prompt, raw, files, directory, history)
    cache_key, cached = _try_cache(
        use_cache,
        assembled,
        model,
        sandbox,
        trust,
        session,
        cwd,
        effort,
        agent,
        json_schema,
    )
    if cached is not None:
        logger.info(f"dispatch: cache hit for key={cache_key}")
        return cached
    if cache_key is not None:
        logger.info(f"dispatch: cache miss for key={cache_key}")

    response = _run_agy(
        assembled,
        trust=trust,
        cwd=cwd,
        add_dirs=add_dirs,
        model=model,
        sandbox=sandbox,
        effort=effort,
        agent=agent,
        json_schema=json_schema,
    )
    # Bracketed returns are errors/status, not real conversations, so
    # only append to the session (and cache) when agy actually responded.
    if not response.startswith("["):
        logger.info(f"dispatch: success, response_len={len(response)}")
        if in_session:
            _append_turn(cwd, session, prompt, response, _summarize_text)
        if cache_key is not None:
            _cache_put(cache_key, response, model, effort, agent)
    else:
        logger.warning(f"dispatch: returned status/error response: {response}")
    return response


@mcp.tool()
def gemini_prompt(
    prompt: str,
    files: list[str] | None = None,
    directory: str | None = None,
    raw: bool = False,
    trust: bool = False,
    cwd: str | None = None,
    add_dirs: list[str] | None = None,
    continue_session: bool = False,
    session: str | None = None,
    model: str | None = None,
    sandbox: bool = False,
    use_cache: bool = True,
    effort: str | None = None,
    agent: str | None = None,
) -> str:
    """Send a prompt to Antigravity (agy) and return the response.

    Use this to offload large-context work — file exploration, indexing,
    summarization, cross-file analysis, research. Claude should construct
    a thoughtful prompt that leads with task context (what you are doing
    and where you are in the task), followed by a specific ask.
    See GEMINI.md for the full prompting guide.

    agy explores the workspace on its own when rooted via `cwd`, so
    prefer that over inlining. Only pass `files` or `directory` to inline
    content when the user explicitly named specific files.

    For multi-step work against the same codebase, pass a `session` name
    (or continue_session=True for the default session) so the prior
    transcript is replayed as context. Sessions are Castor-owned and
    project-scoped (they require cwd), and they survive restarts. Clear
    the default session with gemini_reset; manage named ones with
    gemini_sessions / gemini_session_delete.

    Args:
        prompt: The fully-formed prompt Claude has constructed.
        files: Absolute paths to files to inline into the prompt.
        directory: A directory whose contents should be inlined.
        raw: If True, skip structured response instructions.
        trust: If True, pass --dangerously-skip-permissions so agy
            auto-approves tool actions (writes, commands). Requires
            `cwd`. Default False keeps agy read-only.
        cwd: Working directory for the agy subprocess. Required when
            using trust=True, and whenever a session is used (sessions
            are project-scoped). Pass the absolute project path.
        add_dirs: Extra directories to grant agy read access to (via
            --add-dir) without inlining them. Lets agy explore them.
        continue_session: Convenience flag — resume the project's default
            session ("__default__"). Equivalent to session="__default__".
            Ignored when `session` is set explicitly. Requires cwd.
        session: Name a project-scoped conversation to continue (e.g.
            "refactor-auth"). Castor replays its transcript as context and
            appends this turn on success. Requires cwd. Session calls are
            stateful and never cached.
        model: Select the agy model (e.g. a faster model for light
            summarization, a stronger one for deep reasoning). Call
            gemini_models for the available names. Defaults to agy's
            configured default.
        effort: Select agy's reasoning effort (low/medium/high/xhigh/
            max): a cheap/fast-vs-capable tradeoff independent of
            model choice.
            Defaults to agy's own default when omitted.
        agent: Route the prompt to one of agy's built-in specialized
            agents (e.g. "security-engineer", "root-cause-analyst").
            Call gemini_agents for the available names. Defaults to
            agy's own default agent when omitted.
        sandbox: If True, agy explores the filesystem and auto-approves
            read commands (find/grep/git) inside agy's OS sandbox, which
            keeps the workspace read-only. Writes outside the workspace
            and network access are not blocked. The middle ground
            between read-only (where any shell command is auto-denied)
            and trust. Ignored when trust=True.
        use_cache: If True (default), reuse a stored response when the
            same prompt is re-sent against an unchanged repo (matched by
            git HEAD + working-tree state). Only side-effect-free calls
            are cached — never trust mode or session calls. Set False to
            force a fresh run. Clear with gemini_cache_clear.
    """
    resolved = session or (DEFAULT_SESSION if continue_session else None)
    return _dispatch(
        prompt,
        files=files,
        directory=directory,
        raw=raw,
        trust=trust,
        cwd=cwd,
        add_dirs=add_dirs,
        model=model,
        sandbox=sandbox,
        use_cache=use_cache,
        session=resolved,
        effort=effort,
        agent=agent,
    )


def _validate_index_keys(data: dict) -> bool:
    """True when data contains all required gemini_index keys."""
    return _INDEX_KEYS.issubset(data.keys())


def _update_last_index(cwd: str) -> None:
    """Record a successful gemini_index run in the project state store.

    Best-effort — never raises. Consumed by FR-4 (gemini_status with cwd)
    to surface when the project was last indexed and at which commit.

    Args:
        cwd: Absolute project root that was just indexed.
    """
    state = _load_project_state(cwd)
    state["last_index"] = {
        "timestamp": time.time(),
        "git_sha": _get_git_sha(cwd) or "",
    }
    state["updated_at"] = time.time()
    _save_project_state(cwd, state)


def _parse_index(raw: str, cwd: str) -> str:
    """Parse agy's index response into a structured JSON string.

    First tries the schema-enforced NDJSON path (_parse_ndjson_result) —
    raw is the stream-json output produced with INDEX_JSON_SCHEMA, so
    structure is a contract agy validated, not a request it might have
    ignored. Falls back to extracting a JSON object from raw the old
    way (stripping code fences, best-effort) if that path comes back
    empty — e.g. schema enforcement failed, or agy fell back to plain
    text. That fallback text is narrowed via _fallback_text
    first when raw is NDJSON, so a schema miss degrades to the old
    plain-text failure surface rather than dumping the full multi-line
    transcript (tool calls, permission list, schema echo) as
    raw_markdown. Either way, required keys are (re-)validated and
    git_sha is injected; a minimal envelope with raw_markdown is
    returned as the last resort.

    Args:
        raw: agy's raw index response (NDJSON or plain text).
        cwd: Project root; used to inject the current git SHA.

    Returns:
        A JSON string with structured index fields plus git_sha, or
        (on parse failure) a minimal JSON envelope with git_sha and
        raw_markdown.
    """
    git_sha = _get_git_sha(cwd)
    structured = _parse_ndjson_result(raw)
    if isinstance(structured, dict) and _validate_index_keys(structured):
        structured["git_sha"] = git_sha
        return json.dumps(structured)
    text = _fallback_text(raw)
    fallback: dict[str, Any] = {
        "git_sha": git_sha,
        "raw_markdown": text,
    }
    try:
        data = json.loads(_extract_json_blob(text))
    except (ValueError, TypeError):
        return json.dumps(fallback)
    if not isinstance(data, dict) or not _validate_index_keys(data):
        return json.dumps(fallback)
    data["git_sha"] = git_sha
    return json.dumps(data)


@mcp.tool()
def gemini_index(
    cwd: str,
    model: str | None = None,
    agent: str | None = None,
) -> str:
    """Produce a structured JSON index of a codebase for use as context.

    Runs agy as a sandboxed explorer rooted at cwd — it navigates the
    project on its own and returns a structured JSON index: a summary,
    entry points, key modules with file/line/role, custom exception
    types, and an architecture overview. Also includes git_sha.

    This is the canonical "give me context I can reuse" call: it is
    side-effect-free, so when cwd is a git repo the result is cached
    against the repo state and re-runs are instant.

    Args:
        cwd: Absolute path to the project root to index.
        model: Optional agy model override (see gemini_models).
        agent: Optional agy agent override (see gemini_agents). Defaults
            to agy's default agent — the built-in "repo-index" agent
            was tried and measurably regressed reliability here (agy
            1.1.9): combined with sandboxed multi-turn exploration and
            --json-schema enforcement, it tends to restate its JSON
            answer across turns, which breaks both schema enforcement
            and the plain-text fallback. Pass agent="repo-index"
            explicitly to opt back in if a future agy version fixes
            this.

    Returns:
        A JSON string with keys: summary, entry_points, modules,
        exception_types, architecture, git_sha. On parse failure,
        {"git_sha": <sha>, "raw_markdown": <raw>}. On agy error,
        returns a bracketed error string.
    """
    raw = _dispatch(
        INDEX_PROMPT,
        raw=True,
        sandbox=True,
        cwd=cwd,
        model=model,
        agent=agent,
        json_schema=INDEX_JSON_SCHEMA,
    )
    if raw.startswith("["):
        return raw
    result = _parse_index(raw, cwd)
    _update_last_index(cwd)
    return result


@mcp.tool()
def gemini_review(
    cwd: str,
    diff: str | None = None,
    model: str | None = None,
    agent: str | None = "code-reviewer",
) -> str:
    """Get a free second-opinion review of a code diff from agy.

    Sends a diff to agy for a correctness-focused review at no cost to
    Claude's context. When diff is omitted, the uncommitted changes in
    cwd (staged and unstaged, via `git diff HEAD`) are reviewed. The diff
    is inlined, so the review is cached on the diff content. Complements —
    does not replace — Claude's own /code-review.

    Args:
        cwd: Absolute path to the git repository.
        diff: A unified diff to review. When None, the uncommitted
            changes in cwd are captured automatically.
        model: Optional agy model override (see gemini_models).
        agent: agy agent to run this under. Defaults to agy's built-in
            "code-reviewer" agent, purpose-built for this task. Pass
            None to fall back to agy's default agent, or another name
            to override (see gemini_agents).

    Returns:
        Review findings, or a bracketed error/status string.
    """
    if diff is None:
        diff = _git_diff(cwd)
        if diff.startswith("["):
            return diff
    if not diff.strip():
        return (
            "[No diff to review — the working tree is clean or the "
            "provided diff is empty.]"
        )
    return _dispatch(
        f"{REVIEW_PROMPT}\n\n[DIFF]\n{diff}", model=model, agent=agent
    )


@mcp.tool()
def gemini_security_review(
    cwd: str,
    diff: str | None = None,
    model: str | None = None,
    agent: str | None = "security-engineer",
) -> str:
    """Get a free security-focused review of a code diff from agy.

    Sends a diff to agy for a security-focused review at no cost to
    Claude's context. Complements — does not replace — gemini_review
    (which is correctness-focused and explicitly skips security); run
    both for a full pass. When diff is omitted, the uncommitted changes
    in cwd (staged and unstaged, via `git diff HEAD`) are reviewed. The
    diff is inlined, so the review is cached on the diff content.

    Args:
        cwd: Absolute path to the git repository.
        diff: A unified diff to review. When None, the uncommitted
            changes in cwd are captured automatically.
        model: Optional agy model override (see gemini_models).
        agent: agy agent to run this under. Defaults to agy's built-in
            "security-engineer" agent, purpose-built for this task.
            Pass None to fall back to agy's default agent, or another
            name to override (see gemini_agents).

    Returns:
        Security findings, or a bracketed error/status string.
    """
    if diff is None:
        diff = _git_diff(cwd)
        if diff.startswith("["):
            return diff
    if not diff.strip():
        return (
            "[No diff to review — the working tree is clean or the "
            "provided diff is empty.]"
        )
    return _dispatch(
        f"{SECURITY_REVIEW_PROMPT}\n\n[DIFF]\n{diff}",
        model=model,
        agent=agent,
    )


@mcp.tool()
def gemini_find_usages(cwd: str, symbol: str, model: str | None = None) -> str:
    """Find where and how a symbol is used across a codebase.

    Runs agy as a sandboxed explorer rooted at cwd to locate every use of
    `symbol` and summarize how it is used, with paths and line
    references. Side-effect-free, so the result is cached against the
    repo state when cwd is a git repo.

    Args:
        cwd: Absolute path to the project root.
        symbol: The symbol name to trace (function, class, variable, …).
        model: Optional agy model override (see gemini_models).

    Returns:
        A usage report, or a bracketed error string.
    """
    prompt = (
        f"Find every place the symbol `{symbol}` is used across this "
        "codebase. For each occurrence give the file path, the line, and "
        "a short note on how it is used (definition, call, import, "
        "re-export, test, etc.). Group by file and end with a one-line "
        "summary of the symbol's role. Reference real paths and line "
        "numbers."
    )
    return _dispatch(prompt, sandbox=True, cwd=cwd, model=model)


@mcp.tool()
def gemini_explain_error(
    cwd: str,
    error: str,
    model: str | None = None,
    agent: str | None = "root-cause-analyst",
) -> str:
    """Explain an error or stack trace against the codebase.

    Runs agy as a sandboxed explorer rooted at cwd to trace the error to
    its likely source and return ranked root-cause hypotheses with the
    files to check. Side-effect-free, so the result is cached against the
    repo state when cwd is a git repo.

    Args:
        cwd: Absolute path to the project root.
        error: The error message or stack trace to diagnose.
        model: Optional agy model override (see gemini_models).
        agent: agy agent to run this under. Defaults to agy's built-in
            "root-cause-analyst" agent, purpose-built for this task.
            Pass None to fall back to agy's default agent, or another
            name to override (see gemini_agents).

    Returns:
        Ranked root-cause hypotheses, or a bracketed error string.
    """
    prompt = (
        "Diagnose this error against the codebase. Trace it to its likely "
        "source and return the most probable root causes, ranked, each "
        "with the specific file(s) and line(s) to check and why. Be "
        f"concrete.\n\n[ERROR]\n{error}"
    )
    return _dispatch(prompt, sandbox=True, cwd=cwd, model=model, agent=agent)


@mcp.tool()
def gemini_summarize(cwd: str, target: str, model: str | None = None) -> str:
    """Summarize a large file or directory that would blow Claude's context.

    Runs agy as a sandboxed explorer rooted at cwd to read `target` and
    return a structured, token-lean summary. Side-effect-free, so the
    result is cached against the repo state when cwd is a git repo.

    Args:
        cwd: Absolute path to the project root.
        target: A file or directory path (under cwd) to summarize.
        model: Optional agy model override (see gemini_models).

    Returns:
        A JSON string with keys: summary, key_points. On parse failure,
        {"summary": <raw>, "key_points": []}. On agy error, a bracketed
        error string.
    """
    raw = _dispatch(
        SUMMARIZE_PROMPT.format(target=target),
        raw=True,
        sandbox=True,
        cwd=cwd,
        model=model,
        json_schema=SUMMARIZE_JSON_SCHEMA,
    )
    if raw.startswith("["):
        return raw
    return _parse_summary(raw)


@mcp.tool()
def gemini_semantic_search(
    cwd: str, query: str, model: str | None = None
) -> str:
    """Find code by natural-language intent, not an exact symbol name.

    Runs agy as a sandboxed explorer rooted at cwd to locate code matching
    `query` (e.g. "where is auth validated?") and return ranked hits with
    paths and lines. Complements gemini_find_usages, which is symbol-exact.
    Side-effect-free, so the result is cached against the repo state when
    cwd is a git repo.

    Args:
        cwd: Absolute path to the project root.
        query: A natural-language description of the code to find.
        model: Optional agy model override (see gemini_models).

    Returns:
        A JSON array of at most 20 objects {path, line, reason}, most
        relevant first. On parse failure, {"raw_markdown": <raw>}. On agy
        error, a bracketed error string.
    """
    raw = _dispatch(
        SEMANTIC_SEARCH_PROMPT.format(query=query, n=SEMANTIC_SEARCH_MAX_HITS),
        raw=True,
        sandbox=True,
        cwd=cwd,
        model=model,
        json_schema=SEMANTIC_SEARCH_JSON_SCHEMA,
    )
    if raw.startswith("["):
        return raw
    return _parse_hits(raw)


@mcp.tool()
def gemini_document(cwd: str, target: str, model: str | None = None) -> str:
    """Generate documentation for a symbol or file in the project's style.

    Runs agy as a sandboxed explorer rooted at cwd to draft docstrings or
    docs for `target`, inferring the project's existing style. Return-only
    — it never writes files; Claude applies the result. Side-effect-free,
    so the result is cached against the repo state when cwd is a git repo.

    Args:
        cwd: Absolute path to the project root.
        target: A file path or a symbol name to document.
        model: Optional agy model override (see gemini_models).

    Returns:
        Proposed docstrings/markdown, or a bracketed error string.
    """
    return _dispatch(
        DOCUMENT_PROMPT.format(target=target),
        sandbox=True,
        cwd=cwd,
        model=model,
    )


# agy runs are local subprocesses, so this pool is not tied to NIM's gate.
_agy_job_pool = ThreadPoolExecutor(max_workers=JOB_POOL_MAX)


@mcp.tool()
def gemini_start(
    prompt: str,
    files: list[str] | None = None,
    directory: str | None = None,
    raw: bool = False,
    trust: bool = False,
    cwd: str | None = None,
    add_dirs: list[str] | None = None,
    model: str | None = None,
    sandbox: bool = False,
    use_cache: bool = True,
    effort: str | None = None,
    agent: str | None = None,
) -> str:
    """Start an agy prompt in the background and return a job id.

    Use this for long explorations or fan-out (start several jobs across
    subtrees, then synthesize) so Claude is not blocked for the full agy
    timeout. The job runs off-thread; collect its result with gemini_poll.

    Background jobs are fresh-only by construction — they take no session
    param and never touch a transcript, because concurrent jobs would race
    on it. For stateful, multi-turn work use the synchronous gemini_prompt
    with a session name instead.

    Args:
        prompt: The fully-formed prompt Claude has constructed.
        files: Absolute paths to files to inline into the prompt.
        directory: A directory whose contents should be inlined.
        raw: If True, skip structured response instructions.
        trust: If True, full agent mode (requires cwd). Note: trust runs
            are never cached.
        cwd: Working directory for the agy subprocess. Required when
            trust=True.
        add_dirs: Extra directories to grant agy read access to.
        model: Select the agy model (see gemini_models).
        effort: Select agy's reasoning effort (low/medium/high/xhigh/
            max). Defaults to agy's own default when omitted.
        agent: Route the prompt to one of agy's built-in specialized
            agents (see gemini_agents). Defaults to agy's own default
            agent when omitted.
        sandbox: If True, agy explores with auto-approved commands
            inside its OS sandbox (workspace read-only). Ignored when
            trust=True.
        use_cache: If True (default), a cache hit completes the job
            immediately without spawning agy.

    Returns:
        A message with the job id to pass to gemini_poll, or a bracketed
        error string (e.g. when trust=True is missing cwd).
    """
    if trust and not cwd:
        return (
            "[Error: cwd is required when trust=True so agy's workspace "
            "is rooted in the correct project directory. Pass the "
            "absolute path of the user's project.]"
        )

    kwargs: dict[str, Any] = {
        "prompt": prompt,
        "files": files,
        "directory": directory,
        "raw": raw,
        "trust": trust,
        "cwd": cwd,
        "add_dirs": add_dirs,
        "model": model,
        "sandbox": sandbox,
        "use_cache": use_cache,
        "effort": effort,
        "agent": agent,
    }
    job_id = uuid.uuid4().hex[:8]
    with _jobs_lock:
        _evict_jobs()
        _jobs[job_id] = _Job(status="running", started_at=time.time())
    _agy_job_pool.submit(_run_job, job_id, kwargs, _dispatch)
    return (
        f"Started background job {job_id}. Poll it with "
        f'gemini_poll("{job_id}") — do other work first, then check back.'
    )


@mcp.tool()
def gemini_models() -> str:
    """List the models available to Antigravity (agy).

    Returns the names you can pass as the `model` argument to
    gemini_prompt. Requires sign-in — if not authenticated, returns the
    sign-in instruction instead.
    """
    try:
        result = subprocess.run(
            ["agy", "models"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return f"[Error: `agy` CLI not found. Install: {INSTALL_CMD}]"
    except subprocess.TimeoutExpired:
        return "[Error: `agy models` timed out.]"

    combined = result.stdout + result.stderr
    if _needs_auth(combined):
        return f"[Not signed in to Antigravity. {AUTH_HINT}]"

    if result.returncode != 0 and result.stderr.strip():
        return f"[Antigravity error]\n{result.stderr.strip()}"

    return result.stdout.strip() or "[No models reported by Antigravity]"


@mcp.tool()
def gemini_agents() -> str:
    """List agy's built-in specialized agents.

    Returns the names you can pass as the `agent` argument to
    gemini_prompt (and any workflow tools that accept one). Requires
    sign-in — if not authenticated, returns the sign-in instruction
    instead.
    """
    try:
        result = subprocess.run(
            ["agy", "agents"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return f"[Error: `agy` CLI not found. Install: {INSTALL_CMD}]"
    except subprocess.TimeoutExpired:
        return "[Error: `agy agents` timed out.]"

    combined = result.stdout + result.stderr
    if _needs_auth(combined):
        return f"[Not signed in to Antigravity. {AUTH_HINT}]"

    if result.returncode != 0 and result.stderr.strip():
        return f"[Antigravity error]\n{result.stderr.strip()}"

    return result.stdout.strip() or "[No agents reported by Antigravity]"


@mcp.tool()
def gemini_auth() -> str:
    """Return instructions for the user to sign in to Antigravity.

    Sign-in is interactive: agy prints an OAuth URL, the user consents
    in the browser, and agy waits for that to complete. An MCP tool call
    blocks Claude while it runs, so it cannot drive an interactive flow —
    the user has no way to act while the tool is pending. Instead, this
    returns an instruction for the user to run the sign-in command
    themselves directly in the Claude Code prompt.

    Call this when `gemini_status` or `gemini_prompt` reports that the
    user is not signed in.
    """
    try:
        result = subprocess.run(
            ["agy", "--version"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return f"[Error: `agy` CLI not found. Install: {INSTALL_CMD}]"

    if result.returncode != 0:
        return f"[Error: `agy` CLI not found. Install: {INSTALL_CMD}]"

    return (
        "To sign in to Antigravity, type this in the Claude Code prompt "
        "(the `!` runs it as a live terminal command):\n\n"
        '    ! agy -p "ok"\n\n'
        "Complete the Google consent in your browser when agy prints the "
        "sign-in URL. This is a one-time step — the token persists in the "
        "system keyring, so later calls run without prompts."
    )


@mcp.tool()
def gemini_setup() -> str:
    """Return setup instructions for Antigravity (agy).

    Call this when the user wants to set up Antigravity for the first
    time or wants to enable agent mode with filesystem access.
    """
    return f"""\
To use Antigravity (the `agy` CLI), install it and sign in once.

1. Install (if `gemini_status` reports NOT INSTALLED):

    {INSTALL_CMD}

2. Sign in (one-time). Tell the user to type this in the Claude Code
   prompt (the `!` runs it as a live terminal command):

    ! agy -p "ok"

   then complete the Google consent in the browser. The token is saved
   in the system keyring, so later calls run without prompts. The
   `gemini_auth` tool returns these same instructions.

Once signed in, call gemini_prompt with trust=True (and cwd set to the
project path) for full agent mode: agy explores the filesystem, runs
tools, and follows imports on its own.\
"""


def _status_extras() -> str:
    """Best-effort niceties appended to a READY status line.

    Every lookup is non-fatal — any failure leaves its piece out of the
    suffix, so a missing nicety never downgrades a READY status. Account
    and version freshness are intentionally omitted: agy exposes no
    whoami command, and `agy update` performs an update rather than a
    safe check. `agy changelog` is intentionally never called here (or
    anywhere in Castor): it mutates a disk cache on every invocation, so
    wiring it into a diagnostics tool that can be polled repeatedly
    would give every status check a hidden filesystem side effect.

    Returns:
        A newline-prefixed suffix listing available models and agents
        (whichever lookups succeeded), or "".
    """
    suffix = ""

    try:
        result = subprocess.run(
            ["agy", "models"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            models = result.stdout.strip()
            if models and not _needs_auth(models + result.stderr):
                shown = "\n".join(models.splitlines()[:10])
                suffix += f"\nModels available:\n{shown}"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    try:
        agents = subprocess.run(
            ["agy", "agents"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )
        if agents.returncode == 0:
            agent_names = agents.stdout.strip()
            if agent_names and not _needs_auth(agent_names + agents.stderr):
                shown = "\n".join(agent_names.splitlines()[:15])
                suffix += f"\nAgents available:\n{shown}"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return suffix


def _format_last_index(state: dict) -> str:
    """Format the last_index entry from a project state dict as a status line.

    Args:
        state: Project state dict from _load_project_state.

    Returns:
        A newline-prefixed "last_index: ..." line, or "" when no index
        has been recorded for the project yet.
    """
    entry = state.get("last_index")
    if not entry:
        return ""
    ts = entry.get("timestamp", 0.0)
    git_sha = entry.get("git_sha", "")
    ts_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))
    sha_str = f"  git_sha: {git_sha[:7]}" if git_sha else ""
    return f"\nlast_index: {ts_str}{sha_str}"


@mcp.tool()
def gemini_status(cwd: str | None = None) -> str:
    """Check that the agy CLI is installed and signed in.

    Run before first use or when troubleshooting. Returns a status string
    with fix instructions if not ready. When READY, appends cache size and
    available models (best-effort). When cwd is provided, also appends the
    last-index timestamp and git SHA for that project (from the project
    state store populated by gemini_index).

    Args:
        cwd: Optional absolute path to a project root. When given, per-
            project index metadata is included in the output.
    """
    not_installed = f"NOT INSTALLED: `agy` CLI not found.\nFix: {INSTALL_CMD}"

    try:
        version_result = subprocess.run(
            ["agy", "--version"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return not_installed

    if version_result.returncode != 0:
        return not_installed

    version = version_result.stdout.strip()

    try:
        auth_result = subprocess.run(
            [
                "agy",
                "--print",
                "Reply with exactly the word: OK",
                "--print-timeout=30s",
            ],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=40,
        )
    except FileNotFoundError:
        return not_installed

    combined = auth_result.stdout + auth_result.stderr
    if _needs_auth(combined):
        return (
            f"NOT SIGNED IN: agy {version} installed but not "
            f"authenticated.\nFix: {AUTH_HINT}"
        )

    if auth_result.returncode != 0:
        return (
            f"ERROR: agy {version} installed but a test prompt failed.\n"
            f"Error: {auth_result.stderr.strip()}"
        )

    suffix = _status_extras()
    suffix += f"\ncache_size_mb: {_cache_size_mb():.1f}"
    for line in _status_lines:
        suffix += line()
    if cwd is not None:
        suffix += _format_last_index(_load_project_state(cwd))
    return f"READY — agy {version}{suffix}"
