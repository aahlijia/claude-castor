from typing import Any
import hashlib
import json
import subprocess
import time
from castor.agy.prompts import (
    AUTH_HINT,
    INSTALL_CMD,
    PRINT_TIMEOUT,
    SUBPROCESS_TIMEOUT,
)
from castor.app import (
    logger,
)
from castor.common import (
    CACHE_KEY_VERSION,
    SUMMARIZE_TEXT_PROMPT,
    _append_turn,
    _assemble_prompt,
    _cache_get,
    _cache_put,
    _extract_json_blob,
    _parse_summary,
    _repo_fingerprint,
    _session_prefix,
)


"""Run the agy CLI: build the command, read its output, cache the result."""


def _is_side_effect_free(trust: bool, session: str | None) -> bool:
    """True when a call only reads — safe to cache.

    trust=True may write files or run commands; a session call is
    stateful (its result feeds the transcript and depends on prior
    turns). Neither is safely cacheable.
    """
    return not trust and not session


def _cache_key(
    prompt: str,
    model: str | None,
    sandbox: bool,
    fingerprint: str | None,
    effort: str | None = None,
    agent: str | None = None,
    json_schema: str | None = None,
) -> str:
    """Hash everything that affects the response into a stable key."""
    payload = json.dumps(
        {
            "version": CACHE_KEY_VERSION,
            "prompt": prompt,
            "model": model or "",
            "sandbox": sandbox,
            "repo": fingerprint or "",
            "effort": effort or "",
            "agent": agent or "",
            "json_schema": json_schema or "",
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_lookup_key(
    prompt: str,
    model: str | None,
    sandbox: bool,
    trust: bool,
    session: str | None,
    cwd: str | None,
    effort: str | None = None,
    agent: str | None = None,
    json_schema: str | None = None,
) -> str | None:
    """Return a cache key if this call is cacheable, else None.

    Cacheable only when side-effect-free. Sandbox explores the
    filesystem, so it is cached only when a repo fingerprint can prove
    the code is unchanged; non-explore (inline/read-only) calls are
    determined by the prompt alone and cache without one.
    """
    if not _is_side_effect_free(trust, session):
        return None
    fingerprint = _repo_fingerprint(cwd)
    if sandbox and fingerprint is None:
        return None
    return _cache_key(
        prompt,
        model,
        sandbox,
        fingerprint,
        effort,
        agent,
        json_schema,
    )


def _try_cache(
    use_cache: bool,
    prompt: str,
    model: str | None,
    sandbox: bool,
    trust: bool,
    session: str | None,
    cwd: str | None,
    effort: str | None = None,
    agent: str | None = None,
    json_schema: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve the cache for a call.

    Returns:
        A (key, cached_response) pair. key is None when the call is not
        cacheable (so the caller skips storing too); cached_response is
        None on a miss.
    """
    if not use_cache:
        return None, None
    key = _cache_lookup_key(
        prompt,
        model,
        sandbox,
        trust,
        session,
        cwd,
        effort,
        agent,
        json_schema,
    )
    if key is None:
        return None, None
    return key, _cache_get(key)


def _ndjson_result_event(stdout: str) -> dict | None:
    """Return the terminal NDJSON "result" event's payload, or None.

    The payload is the dict under `event["result"]` (see
    _parse_ndjson_result for the event layout). None means stdout is
    empty, is not NDJSON, or ended without a result event.
    """
    lines = [line for line in stdout.strip().splitlines() if line.strip()]
    if not lines:
        return None
    try:
        event = json.loads(lines[-1])
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict) or event.get("event") != "result":
        return None
    result = event.get("result")
    return result if isinstance(result, dict) else None


def _parse_ndjson_result(stdout: str) -> Any:
    """Extract structured_output from the terminal NDJSON result event.

    With `--output-format stream-json` + `--json-schema` (see
    _json_schema_args), agy emits one JSON object per line: an "init"
    event, N "step_update" events, and exactly one terminal event
    carrying the schema-validated payload.

    Confirmed live against agy 1.1.9: the terminal event's discriminator
    is the top-level "event" key with value "result" — NOT a "type" key
    as agy's own docs describe — and the payload sits one level down,
    at `event["result"]["structured_output"]`, not flattened onto the
    event itself. A schema-validation failure surfaces as a "result"
    event with no `structured_output` key at all (status "ERROR"), which
    this naturally returns None for. Re-checked on agy 1.4.7: the layout
    is unchanged, and structured_output sits beside a `response` string
    holding the same JSON as text. A run that ended on a denied tool
    action has neither (empty response plus a `denied_actions` list);
    _run_agy turns that into an error before any parser sees it.

    Args:
        stdout: The full NDJSON stdout from an agy run.

    Returns:
        The parsed structured_output value (dict or list, depending on
        the schema's root type), or None on any parse failure, missing
        terminal event, or missing structured_output.
    """
    result = _ndjson_result_event(stdout)
    return result.get("structured_output") if result else None


def _fallback_text(stdout: str) -> str:
    """Recover the terminal event's plain response text from NDJSON.

    Used as the fallback input when _parse_ndjson_result finds no
    structured_output (confirmed live: schema enforcement is not fully
    reliable once an --agent is combined with --sandbox multi-turn
    exploration — the terminal result event can omit structured_output
    entirely). Falling back to the *full* multi-line NDJSON stdout
    (tool-call step_updates, the tool list, permission_mode, the schema
    echo) would be a far noisier failure surface than a schema miss
    deserves — the terminal event's own "response" field is the same
    plain text `--output-format text` would have returned directly, so
    recovering just that keeps the old, narrow fallback shape.

    An NDJSON stream with an empty or missing response yields "", never
    the stream itself. The old `response or stdout` fallback leaked the
    whole stream (starting with the "init" event) into raw_markdown
    whenever agy answered with an empty string.

    Args:
        stdout: The full NDJSON stdout from an agy run, or plain text.

    Returns:
        The terminal event's response string ("" when absent), or
        stdout unchanged when it is not NDJSON with a result event.
    """
    result = _ndjson_result_event(stdout)
    if result is None:
        return stdout
    response = result.get("response")
    return response if isinstance(response, str) else ""


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


def _parse_agy_summary(raw: str) -> str:
    """Parse agy's stream-json summarize output into {summary, key_points}.

    Prefers the schema-enforced structured_output (raw is stream-json
    produced with SUMMARIZE_JSON_SCHEMA). When that is missing, the reply
    text goes through _parse_summary, so a schema miss degrades to the
    plain-text path instead of the full multi-line transcript.
    """
    structured = _parse_ndjson_result(raw)
    if isinstance(structured, dict) and "summary" in structured:
        return json.dumps(
            {
                "summary": structured.get("summary", ""),
                "key_points": structured.get("key_points", []),
            }
        )
    return _parse_summary(_fallback_text(raw))
