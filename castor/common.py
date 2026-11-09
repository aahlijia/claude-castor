from collections.abc import Callable
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any


INSTALL_CMD = "curl -fsSL https://antigravity.google/cli/install.sh | bash"

AUTH_HINT = (
    'Run the `gemini_auth` tool or type `! agy -p "ok"` '
    "in the Claude Code prompt to sign in — it is a one-time step."
)


SYSTEM_INSTRUCTION = """\
You are a technical assistant. Respond with precision and structure.
- Use clear headings and bullet points where appropriate
- Include file names, line numbers, and symbol names when referencing code
- Be concise — avoid filler; every sentence should carry information
- If asked to index or summarize, produce output Claude can use as context
- Do NOT narrate your actions (no "I will read..." / "I will view...")
- Do NOT append a "Summary of Work" or similar trailing section
- Reference files as plain paths, never as file:// links\
"""


# Baked prompt for gemini_index — requests a structured JSON object.
INDEX_PROMPT = """\
You are a technical assistant. Produce a structured JSON index of this
codebase for use as context by another engineer.

Return a JSON object with exactly these keys:

  "summary"         — one-paragraph description of what this codebase does
  "entry_points"    — list of top-level entry point paths as strings
  "modules"         — object mapping each key module or class name to
                      {"file": "<path>", "line": <int>, "role": "<desc>"}
  "exception_types" — list of custom exception or error type names
                      (empty list if none)
  "architecture"    — short paragraph on how the major pieces connect

Be precise — use real file paths, symbol names, and line numbers from
the code. Do NOT wrap the output in a code fence. Return only the JSON
object, nothing else.\
"""


# Baked prompt for gemini_review — a correctness-focused diff review.
REVIEW_PROMPT = """\
Review this code diff as a careful senior engineer. Focus on correctness:
bugs, broken edge cases, error handling, off-by-one and boundary issues,
resource and concurrency mistakes, and anything that would fail at runtime.
Skip pure style nitpicks. For each finding give the file, the relevant hunk,
the problem, and a concrete fix. If you find nothing substantive, say so
plainly rather than inventing concerns."""


# Baked prompt for gemini_security_review — a security-focused diff
# review. Complements REVIEW_PROMPT (correctness) rather than
# overlapping it — each explicitly defers the other's concerns.
SECURITY_REVIEW_PROMPT = """\
Review this code diff as a security engineer. Focus on: injection risks,
auth/authz gaps, secrets or credentials handled unsafely, unsafe
deserialization, path traversal, SSRF, insecure defaults, and anything that
would be exploitable if this code ran with attacker-influenced input. Skip
pure correctness/style nitpicks — that's a separate review. For each finding
give the file, the relevant hunk, the risk, and a concrete fix. If nothing
substantive stands out, say so plainly."""


# Baked prompt for gemini_summarize — structured, token-lean summary.
SUMMARIZE_PROMPT = """\
Read `{target}` (a file or directory under the project root) and summarize
it. Return ONLY a JSON object with exactly these keys:

  "summary"     — one short paragraph on what {target} is and does
  "key_points"  — list of terse, information-dense bullets. For a
                  directory, make each bullet a per-area roll-up prefixed
                  with the area's path.

Every bullet must carry information. Do NOT wrap the output in a code
fence. Return only the JSON object, nothing else.\
"""


# Baked prompt for gemini_semantic_search — ranked hits as JSON. Wrapped
# in a "hits" object (rather than a bare array) because agy's
# --json-schema enforcement requires a root object schema — see
# SEMANTIC_SEARCH_JSON_SCHEMA and _parse_hits.
SEMANTIC_SEARCH_PROMPT = """\
Search this codebase for code relevant to: "{query}".

Return ONLY a JSON object with one key "hits", an array of at most {n}
objects, most relevant first:

  {{"hits": [{{"path": "<repo-relative path>", "line": <int>,
  "reason": "<=12 words"}}]}}

Use real paths and line numbers from the code. Do NOT wrap the output in a
code fence. Return only the JSON object, nothing else.\
"""


# Baked prompt for gemini_document — proposed docs in the project's style.
DOCUMENT_PROMPT = """\
Generate documentation for `{target}` in this project. If `{target}`
resolves to a file, document its public items; otherwise treat it as a
symbol name and document that symbol. Match the project's existing
docstring and comment style — infer it from neighboring code. Return only
the proposed docstrings or markdown; do not restate or rewrite the
implementation, and do not modify any file.\
"""


# Internal prompt for compressing a session transcript (FR-F4).
SUMMARIZE_TEXT_PROMPT = """\
Summarize the following prior conversation into a compact paragraph that
preserves decisions made, facts established, and open threads. Be terse —
this becomes the memory of an ongoing session. Output only the summary.\
"""


# Framing prepended to a replayed session transcript (FR-F3).
SESSION_REPLAY_HEADER = (
    "This is a continuing conversation. Earlier context is provided below "
    "so you can continue it consistently. Do not reintroduce yourself or "
    "repeat prior answers — just continue from where it left off."
)


SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".next",
    "dist",
    "build",
    ".venv",
    "venv",
    ".tox",
    "coverage",
    ".mypy_cache",
    ".pytest_cache",
}

SKIP_EXTENSIONS = {
    ".lock",
    ".log",
    ".pyc",
    ".min.js",
    ".map",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".ico",
    ".svg",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
}

MAX_FILE_BYTES = 100 * 1024  # 100 KB per file

MAX_TOTAL_BYTES = 800 * 1024  # 800 KB total inline content

# Cap how many skipped paths are listed back, to bound the note size.
MAX_SKIPPED_LISTED = 50


# agy print mode waits this long for a single prompt to resolve.
PRINT_TIMEOUT = "300s"

# Subprocess wall-clock guard, slightly above PRINT_TIMEOUT.
SUBPROCESS_TIMEOUT = 310


# Cached responses older than this are treated as misses, bounding
# staleness even when the git-SHA match would otherwise hold an entry.
CACHE_TTL_SECONDS = 7 * 24 * 3600

# Part of every cache key. Bump it when a change makes stored responses
# untrustworthy. 2: the sandbox tier gained auto-approval, and older
# entries can hold denied runs (empty NDJSON) cached as successes.
CACHE_KEY_VERSION = 2


# Client-side session machinery (FR-F). Castor owns the transcript and
# replays it into fresh `agy --print` calls — agy's own --continue is not
# used, so sessions are project-scoped and survive restarts.
DEFAULT_SESSION = "__default__"  # name continue_session=True maps to

SESSION_BUDGET_CHARS = 24000  # replay size before older turns are summarized

SESSION_KEEP_VERBATIM = 3  # most-recent turns kept uncompressed

SESSION_TTL_SECONDS = 30 * 24 * 3600  # idle sessions evicted after this


# Max hits gemini_semantic_search asks agy to return.
SEMANTIC_SEARCH_MAX_HITS = 20


_INDEX_KEYS = frozenset(
    {
        "summary",
        "entry_points",
        "modules",
        "exception_types",
        "architecture",
    }
)


# --json-schema payloads enforced on the three JSON-emitting workflow
# tools (gemini_index, gemini_summarize, gemini_semantic_search), so
# structure is a contract agy enforces rather than a request it might
# ignore (see _parse_ndjson_result). Confirmed live against agy 1.1.9
# that --json-schema requires a root object schema — a root array
# schema returns an error result with no structured_output at all —
# which is why SEMANTIC_SEARCH_JSON_SCHEMA wraps its array in a "hits"
# object instead of matching gemini_semantic_search's bare-array prompt
# shape 1:1 (see SEMANTIC_SEARCH_PROMPT and _parse_hits).
INDEX_JSON_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "entry_points": {
                "type": "array",
                "items": {"type": "string"},
            },
            "modules": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
                        "role": {"type": "string"},
                    },
                    "required": ["file", "line", "role"],
                },
            },
            "exception_types": {
                "type": "array",
                "items": {"type": "string"},
            },
            "architecture": {"type": "string"},
        },
        "required": [
            "summary",
            "entry_points",
            "modules",
            "exception_types",
            "architecture",
        ],
    }
)


SUMMARIZE_JSON_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "key_points": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["summary", "key_points"],
    }
)


SEMANTIC_SEARCH_JSON_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "hits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "line": {"type": "integer"},
                        "reason": {"type": "string"},
                    },
                    "required": ["path", "line", "reason"],
                },
                "maxItems": SEMANTIC_SEARCH_MAX_HITS,
            },
        },
        "required": ["hits"],
    }
)


def _read_bytes(path: Path) -> bytes | None:
    """Read a file's raw bytes, returning None on read error."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _is_binary_bytes(data: bytes) -> bool:
    """Detect binary content by scanning the first 8 KB for a null byte."""
    return b"\x00" in data[:8192]


def _decode_block(path: Path, data: bytes) -> str:
    """Format already-read bytes as a labeled file block."""
    content = data.decode("utf-8", errors="replace")
    return f"[FILE: {path}]\n{content}\n"


def _load_gitignore(root: Path) -> list[str]:
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        return []
    try:
        lines = gitignore.read_text().splitlines()
        return [
            line.strip()
            for line in lines
            if line.strip() and not line.startswith("#")
        ]
    except OSError:
        return []


def _inline_files(paths: list[str]) -> str:
    blocks = []
    limit_kb = MAX_FILE_BYTES // 1024
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            blocks.append(f"[FILE: {raw}]\n[not found]\n")
        elif not p.is_file():
            blocks.append(f"[FILE: {raw}]\n[skipped: not a file]\n")
        elif p.stat().st_size > MAX_FILE_BYTES:
            blocks.append(f"[FILE: {raw}]\n[skipped: >{limit_kb}KB]\n")
        else:
            data = _read_bytes(p)
            if data is None:
                blocks.append(f"[FILE: {raw}]\n[read error]\n")
            elif _is_binary_bytes(data):
                blocks.append(f"[FILE: {raw}]\n[skipped: binary]\n")
            else:
                blocks.append(_decode_block(p, data))
    return "\n".join(blocks)


def _inline_directory(directory: str) -> str:
    root = Path(directory)
    ignore_patterns = _load_gitignore(root)
    blocks = []
    skipped = []
    total = 0

    for rel_root, dirs, files in os.walk(root):
        dirs[:] = [
            d
            for d in dirs
            if d not in SKIP_DIRS
            and not any(
                fnmatch.fnmatch(d, p.rstrip("/")) for p in ignore_patterns
            )
        ]

        for file_name in sorted(files):
            file_path = Path(rel_root) / file_name
            rel = str(file_path.relative_to(root))

            if any(
                fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(file_name, p)
                for p in ignore_patterns
            ):
                continue
            if file_path.suffix in SKIP_EXTENSIONS:
                continue
            if file_path.stat().st_size > MAX_FILE_BYTES:
                skipped.append(rel)
                continue

            data = _read_bytes(file_path)
            if data is None or _is_binary_bytes(data):
                continue

            block = _decode_block(file_path, data)
            if total + len(block) > MAX_TOTAL_BYTES:
                skipped.append(rel)
                continue

            blocks.append(block)
            total += len(block)

    result = "\n".join(blocks)
    if skipped:
        listing = "\n".join(skipped[:MAX_SKIPPED_LISTED])
        extra = len(skipped) - MAX_SKIPPED_LISTED
        more = f"\n…and {extra} more" if extra > 0 else ""
        result += (
            "\n\n[Skipped files — too large or over the inline budget; "
            f"request explicitly if needed]\n{listing}{more}"
        )
    return result


def _is_side_effect_free(trust: bool, session: str | None) -> bool:
    """True when a call only reads — safe to cache.

    trust=True may write files or run commands; a session call is
    stateful (its result feeds the transcript and depends on prior
    turns). Neither is safely cacheable.
    """
    return not trust and not session


def _repo_fingerprint(cwd: str | None) -> str | None:
    """Return a git HEAD + working-tree fingerprint for cwd.

    Combines the HEAD commit with a hash of `git status --porcelain`, so
    any staged, unstaged, or untracked change busts the cache. Returns
    None when cwd is unset or not a git repository — the caller then
    declines to cache explore-mode calls it cannot prove are unchanged.
    """
    if not cwd:
        return None
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
        if head.returncode != 0:
            return None
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    porcelain = hashlib.sha256(status.stdout.encode("utf-8")).hexdigest()
    return f"{head.stdout.strip()}:{porcelain}"


def _get_git_sha(cwd: str) -> str | None:
    """Return the HEAD commit SHA for cwd, or None.

    Args:
        cwd: Working directory to query.

    Returns:
        The HEAD SHA string, or None if cwd is not a git repo or git
        is unavailable.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


_REV_RANGE_RE = re.compile(r"^[\w./~^@{}:+-]+(\.\.\.?[\w./~^@{}:+-]+)?$")


def _git_diff(cwd: str, rev_range: str | None = None) -> str:
    """Return a diff of cwd, or a bracketed error string.

    By default diffs the working tree (staged and unstaged) against HEAD,
    so it captures the changes a user would want reviewed before
    committing. `rev_range` (e.g. "abc123..HEAD") diffs committed work
    instead; it must look like a revision or range, never an option.
    """
    if rev_range and (
        rev_range.startswith("-") or not _REV_RANGE_RE.match(rev_range)
    ):
        return f"[Error: {rev_range!r} is not a git revision or range.]"
    try:
        result = subprocess.run(
            ["git", "diff", rev_range or "HEAD"],
            cwd=cwd,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return "[Error: `git` not found.]"
    except subprocess.TimeoutExpired:
        return "[Error: `git diff` timed out.]"

    if result.returncode != 0:
        return f"[Error: git diff failed]\n{result.stderr.strip()}"
    return result.stdout


def _cache_dir() -> Path:
    """Return the on-disk cache directory (created lazily on write)."""
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "claude-castor"


def _cache_size_mb() -> float:
    """Return the total size of all cache files in MB.

    Walks the entire cache directory recursively, covering both the flat
    response-cache entries and the projects/ subdirectory added by the
    project-state store. Best-effort — returns 0.0 on any error.

    Returns:
        Total size in megabytes, rounded to one decimal place by callers.
    """
    cache_dir = _cache_dir()
    if not cache_dir.exists():
        return 0.0
    total = 0
    try:
        for entry in cache_dir.rglob("*"):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total / (1024 * 1024)


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


def _cache_get(key: str) -> str | None:
    """Return a cached response for key, or None on miss/expiry/error."""
    path = _cache_dir() / f"{key}.json"
    if not path.exists():
        return None
    try:
        entry = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if time.time() - entry.get("created_at", 0) > CACHE_TTL_SECONDS:
        return None
    return entry.get("response")


def _cache_put(
    key: str,
    response: str,
    model: str | None,
    effort: str | None = None,
    agent: str | None = None,
) -> None:
    """Store a response under key. Best-effort; never raises."""
    cache_dir = _cache_dir()
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"{key}.json").write_text(
            json.dumps(
                {
                    "response": response,
                    "model": model or "",
                    "effort": effort or "",
                    "agent": agent or "",
                    "created_at": time.time(),
                }
            )
        )
    except OSError:
        pass


def _project_key(cwd: str) -> str:
    """SHA-256 of the resolved absolute project path."""
    return hashlib.sha256(str(Path(cwd).resolve()).encode("utf-8")).hexdigest()


def _project_state_path(cwd: str) -> Path:
    """Path to the per-project state sidecar JSON."""
    return _cache_dir() / "projects" / f"{_project_key(cwd)}.json"


def _load_project_state(cwd: str) -> dict:
    """Load the project state dict, returning {} on miss or parse error."""
    path = _project_state_path(cwd)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save_project_state(cwd: str, state: dict) -> None:
    """Atomically persist the project state dict. Best-effort; never raises.

    Uses write-to-tmp + os.replace so concurrent writes from background
    jobs cannot corrupt the file (POSIX rename is atomic).
    """
    path = _project_state_path(cwd)
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state))
        os.replace(tmp, path)
    except OSError:
        pass


def _safe_session_name(name: str) -> str:
    """Slugify a session name to a filesystem-safe token."""
    safe = "".join(
        c if c.isalnum() or c in "-_" else "_" for c in name.strip()
    )
    return safe or "session"


def _session_dir(cwd: str) -> Path:
    """Directory holding one project's session files."""
    return _cache_dir() / "sessions" / _project_key(cwd)


def _session_path(cwd: str, name: str) -> Path:
    """Path to a single session's transcript JSON."""
    return _session_dir(cwd) / f"{_safe_session_name(name)}.json"


def _load_session(cwd: str, name: str) -> dict:
    """Load a session dict, evicting it first if older than the TTL.

    Returns {} on miss, parse error, or TTL expiry.
    """
    path = _session_path(cwd, name)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if time.time() - mtime > SESSION_TTL_SECONDS:
        path.unlink(missing_ok=True)
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save_session(cwd: str, name: str, session: dict) -> None:
    """Atomically persist a session dict. Best-effort; never raises."""
    path = _session_path(cwd, name)
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(session))
        os.replace(tmp, path)
    except OSError:
        pass


def _delete_session(cwd: str, name: str) -> bool:
    """Delete a session file. Returns True if one existed."""
    try:
        _session_path(cwd, name).unlink()
        return True
    except OSError:
        return False


def _render_turns(turns: list[dict]) -> str:
    """Render verbatim exchanges as role-tagged replay text."""
    blocks = []
    for i, turn in enumerate(turns, 1):
        blocks.append(
            f"[EXCHANGE {i}]\nUser: {turn.get('prompt', '')}\n"
            f"Assistant: {turn.get('response', '')}"
        )
    return "\n\n".join(blocks)


def _render_session(session: dict) -> str:
    """Render a session as a replay block, or "" when it has no content."""
    if not session:
        return ""
    parts = [SESSION_REPLAY_HEADER]
    summary = session.get("summary") or ""
    if summary:
        parts.append(f"[EARLIER SUMMARY]\n{summary}")
    turns = session.get("turns") or []
    if turns:
        parts.append(_render_turns(turns))
    if len(parts) == 1:  # header only — no prior context yet
        return ""
    return "\n\n".join(parts)


def _session_prefix(cwd: str, name: str) -> str:
    """Load a session and render it as a history prefix for _dispatch."""
    return _render_session(_load_session(cwd, name))


def _compact_session(
    session: dict,
    summarizer: Callable[[str], str],
) -> dict:
    """Summarize older turns when the replay exceeds the char budget.

    Folds everything but the most recent SESSION_KEEP_VERBATIM turns into
    the rolling `summary` prefix via `summarizer`, which each backend
    supplies so a session never needs another backend installed. If
    summarization fails (bracketed response), the session is left
    untouched so a transient error never corrupts the transcript.
    """
    if len(_render_session(session)) <= SESSION_BUDGET_CHARS:
        return session
    turns = session.get("turns") or []
    old = turns[:-SESSION_KEEP_VERBATIM]
    if not old:
        return session
    text = (session.get("summary") or "") + "\n" + _render_turns(old)
    summary = summarizer(text)
    if summary.startswith("["):
        return session
    session["summary"] = summary
    session["turns"] = turns[-SESSION_KEEP_VERBATIM:]
    return session


def _append_turn(
    cwd: str,
    name: str,
    prompt: str,
    response: str,
    summarizer: Callable[[str], str],
) -> None:
    """Append a successful exchange to a session, compacting if needed."""
    now = time.time()
    session = _load_session(cwd, name)
    if not session:
        session = {
            "name": name,
            "summary": "",
            "turns": [],
            "created_at": now,
        }
    session.setdefault("turns", [])
    session["turns"].append(
        {"prompt": prompt, "response": response, "at": now}
    )
    session["updated_at"] = now
    session = _compact_session(session, summarizer)
    _save_session(cwd, name, session)


def _parse_summary(raw: str) -> str:
    """Parse agy's summarize response into a {summary, key_points} JSON.

    First tries the schema-enforced NDJSON path (_parse_ndjson_result) —
    raw is stream-json output produced with SUMMARIZE_JSON_SCHEMA — then
    falls back to extracting a JSON object the old way (best-effort)
    when that path comes back empty. That fallback text is narrowed via
    _fallback_text first when raw is NDJSON, so a schema miss
    degrades to the old plain-text failure surface rather than the full
    multi-line transcript. Falls back to {"summary": raw, "key_points":
    []} when neither path yields a valid summary.
    """
    structured = _parse_ndjson_result(raw)
    if isinstance(structured, dict) and "summary" in structured:
        return json.dumps(
            {
                "summary": structured.get("summary", ""),
                "key_points": structured.get("key_points", []),
            }
        )
    text = _fallback_text(raw)
    fallback = {"summary": text, "key_points": []}
    try:
        data = json.loads(_extract_json_blob(text))
    except (ValueError, TypeError):
        return json.dumps(fallback)
    if not isinstance(data, dict) or "summary" not in data:
        return json.dumps(fallback)
    return json.dumps(
        {
            "summary": data.get("summary", ""),
            "key_points": data.get("key_points", []),
        }
    )


def _assemble_prompt(
    prompt: str,
    raw: bool,
    files: list[str] | None,
    directory: str | None,
    history: str | None = None,
) -> str:
    """Build the full prompt: system prefix, history, ask, inlined content.

    Args:
        history: A replayed session transcript (see _session_prefix) to
            inject between the system instruction and the current ask.
            None for stateless calls.
    """
    parts: list[str] = []
    if not raw:
        parts.append(SYSTEM_INSTRUCTION)
    if history:
        parts.append(history)
    parts.append(prompt)
    if files:
        parts.append(_inline_files(files))
    if directory:
        parts.append(_inline_directory(directory))
    return "\n\n".join(parts)


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


def _strip_fences(text: str) -> str:
    """Strip a single outermost markdown code fence from text."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    first_newline = stripped.find("\n")
    if first_newline == -1 or not stripped.endswith("```"):
        return stripped
    return stripped[first_newline + 1 : -3].strip()


def _extract_json_blob(text: str) -> str:
    """Pull the outermost JSON object/array out of a prose-wrapped reply.

    agy is told to return bare JSON but sometimes leads with a line of
    narration anyway (e.g. "Let me produce the JSON index.\\n\\n{...}").
    Strips fences, then — if the result isn't already clean JSON — slices
    from the first '{' or '[' to the last matching close bracket.
    """
    stripped = _strip_fences(text)
    starts = [i for i in (stripped.find("{"), stripped.find("[")) if i != -1]
    if not starts:
        return stripped
    start = min(starts)
    if start == 0:
        return stripped
    close = "}" if stripped[start] == "{" else "]"
    end = stripped.rfind(close)
    if end == -1 or end < start:
        return stripped
    return stripped[start : end + 1]


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


# .dekko holds dekko's map and notes: read through the dekko tools only.
_NIM_AGENT_DENIED_DIRS = {".git", ".ssh", ".aws", ".gnupg", ".dekko"}

_AGENT_DENIED_ERROR = "path is off limits (secrets, .git and .dekko)"

_NIM_AGENT_DENIED_NAMES = {
    "id_rsa",
    "id_ed25519",
    ".netrc",
    ".npmrc",
    ".pypirc",
}

_NIM_AGENT_DENIED_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def _agent_denied(relative: Path) -> bool:
    """True for paths the agent may never read (secrets, .git, .dekko)."""
    if _NIM_AGENT_DENIED_DIRS & set(relative.parts):
        return True
    name = relative.name.lower()
    return (
        name.startswith(".env")
        or name in _NIM_AGENT_DENIED_NAMES
        or name.endswith(_NIM_AGENT_DENIED_SUFFIXES)
    )


def _agent_resolve(root: Path, raw: str) -> tuple[Path | None, str | None]:
    """Resolve `raw` inside `root`: (path, None) or (None, error).

    Symlinks are followed before the containment check, so a link out of
    the project is refused like any other outside path.
    """
    try:
        target = (root / (raw or ".")).resolve()
    except (OSError, RuntimeError, ValueError):
        return None, "invalid path"
    if not target.is_relative_to(root):
        return None, "path is outside the project"
    if _agent_denied(target.relative_to(root)):
        return None, _AGENT_DENIED_ERROR
    return target, None


def _edit_one_line(text: str) -> str:
    """`text` with its line breaks folded into spaces."""
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Run tier
#
# Castor's one path that runs project commands: an allowlisted suite command
# started in its own session, watched by a monitor thread, reported when it
# ends. Two keys open it: the project commits .castor/run.json, and the MCP
# registration lists the root in CASTOR_RUN_ROOTS (a cloned repo cannot
# authorize itself). Castor never kills, signals or restarts a process.
# ---------------------------------------------------------------------------

RUN_CONFIG_FILE = ".castor/run.json"


def _run_regex(value: Any, key: str) -> re.Pattern:
    """A compiled pattern, or ValueError naming `key`."""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a regex string.")
    try:
        return re.compile(value)
    except re.error as exc:
        raise ValueError(f"{key} is not a valid regex: {exc}") from None


def _worktree_roots(root: Path) -> list[Path]:
    """`root` plus every worktree of its git repo, resolved."""
    roots = [root.resolve()]
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return roots
    if out.returncode != 0:
        return roots
    roots.extend(
        Path(line.removeprefix("worktree ")).resolve()
        for line in out.stdout.splitlines()
        if line.startswith("worktree ")
    )
    return roots
