import fnmatch
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

# Parse --log-file from sys.argv
_log_file_path = None
_new_argv = []
_idx = 0
while _idx < len(sys.argv):
    _arg = sys.argv[_idx]
    if _arg == "--log-file":
        if _idx + 1 < len(sys.argv):
            _log_file_path = sys.argv[_idx + 1]
            _idx += 2
            continue
    elif _arg.startswith("--log-file="):
        _log_file_path = _arg.split("=", 1)[1]
        _idx += 1
        continue
    _new_argv.append(_arg)
    _idx += 1
sys.argv = _new_argv

logger = logging.getLogger("claude-castor")
logger.addHandler(logging.NullHandler())

_file_handler = None
if _log_file_path:
    try:
        _log_path = Path(_log_file_path).resolve()
        _log_path.parent.mkdir(parents=True, exist_ok=True)
        _file_handler = logging.FileHandler(_log_path, encoding="utf-8")
        _formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        _file_handler.setFormatter(_formatter)
        logger.addHandler(_file_handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    except Exception as _e:
        sys.stderr.write(f"Failed to configure logging: {_e}\n")

mcp = FastMCP("claude-castor")

if _log_file_path and _file_handler:
    try:
        _fastmcp_logger = logging.getLogger("fastmcp")
        _fastmcp_logger.addHandler(_file_handler)
        _fastmcp_logger.setLevel(logging.INFO)

        _root_logger = logging.getLogger()
        _root_logger.addHandler(_file_handler)
        _root_logger.setLevel(logging.INFO)

        logger.info(
            "Logging initialized. Server starting up. Writing logs to %s",
            _log_file_path,
        )
    except Exception as _e:
        pass

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

# Baked prompt for gemini_semantic_search — ranked hits as JSON.
SEMANTIC_SEARCH_PROMPT = """\
Search this codebase for code relevant to: "{query}".

Return ONLY a JSON array of at most {n} objects, most relevant first:

  [{{"path": "<repo-relative path>", "line": <int>, "reason": "<=12 words"}}]

Use real paths and line numbers from the code. Do NOT wrap the output in a
code fence. Return only the JSON array, nothing else.\
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


def _build_agy_cmd(
    prompt: str,
    trust: bool,
    cwd: str | None,
    add_dirs: list[str] | None,
    model: str | None,
    sandbox: bool,
    effort: str | None = None,
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
    permissions) wins over sandbox (--sandbox). Multi-turn context is
    handled by Castor's own transcript replay, not agy's --continue.

    `--disable-slash-commands` is always passed: Castor's prompts
    routinely inline arbitrary file/directory content, and inlined
    text that happens to start with `/` at the start of a line must
    never be interpreted by agy as a skill invocation rather than
    inert content handed over for analysis.

    Returns:
        The full argv list for the agy subprocess.
    """
    cmd = ["agy", "--print", prompt, f"--print-timeout={PRINT_TIMEOUT}"]
    cmd.append("--disable-slash-commands")
    if trust:
        cmd.append("--dangerously-skip-permissions")
    elif sandbox:
        cmd.append("--sandbox")
    if cwd:
        cmd.extend(["--add-dir", cwd])
    for directory in add_dirs or []:
        cmd.extend(["--add-dir", directory])
    if model:
        cmd.extend(["--model", model])
    if effort:
        cmd.extend(["--effort", effort])
    return cmd


def _run_agy(
    prompt: str,
    trust: bool = False,
    cwd: str | None = None,
    add_dirs: list[str] | None = None,
    model: str | None = None,
    sandbox: bool = False,
    effort: str | None = None,
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
        sandbox: If True, pass --sandbox so agy explores with terminal
            restrictions instead of auto-approving everything. Ignored
            when trust is True (trust is the broader grant).
        effort: If set, pass --effort <effort> to select agy's reasoning
            effort (low/medium/high). Not validated server-side; an
            invalid value is rejected by agy itself.

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

    res = result.stdout.strip() or "[No response from Antigravity]"
    logger.info(f"run_agy: returning result (length={len(res)})")
    return res


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
            text=True,
            timeout=10,
        )
        if head.returncode != 0:
            return None
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            capture_output=True,
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
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _git_diff(cwd: str) -> str:
    """Return uncommitted changes in cwd, or a bracketed error string.

    Diffs the working tree (staged and unstaged) against HEAD, so it
    captures the changes a user would want reviewed before committing.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=cwd,
            capture_output=True,
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
) -> str:
    """Hash everything that affects the response into a stable key."""
    payload = json.dumps(
        {
            "prompt": prompt,
            "model": model or "",
            "sandbox": sandbox,
            "repo": fingerprint or "",
            "effort": effort or "",
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
    return _cache_key(prompt, model, sandbox, fingerprint, effort)


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


def _list_models() -> list[str]:
    """Return agy's model names (raw lines), or [] on any failure."""
    try:
        result = subprocess.run(
            ["agy", "models"], capture_output=True, text=True, timeout=15
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
        effort: agy reasoning effort to request (low/medium/high).
            Defaults to "low" for cheap/fast compaction.

    Returns:
        A prose summary, or a bracketed error/status string.
    """
    prompt = f"{SUMMARIZE_TEXT_PROMPT}\n\n{text}"
    return _run_agy(prompt, effort=effort)


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


def _compact_session(session: dict) -> dict:
    """Summarize older turns when the replay exceeds the char budget.

    Folds everything but the most recent SESSION_KEEP_VERBATIM turns into
    the rolling `summary` prefix via _summarize_text. If summarization
    fails (bracketed agy response), the session is left untouched so a
    transient error never corrupts the transcript.
    """
    if len(_render_session(session)) <= SESSION_BUDGET_CHARS:
        return session
    turns = session.get("turns") or []
    old = turns[:-SESSION_KEEP_VERBATIM]
    if not old:
        return session
    text = (session.get("summary") or "") + "\n" + _render_turns(old)
    summary = _summarize_text(text)
    if summary.startswith("["):
        return session
    session["summary"] = summary
    session["turns"] = turns[-SESSION_KEEP_VERBATIM:]
    return session


def _append_turn(cwd: str, name: str, prompt: str, response: str) -> None:
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
    session = _compact_session(session)
    _save_session(cwd, name, session)


def _parse_summary(raw: str) -> str:
    """Parse agy's summarize response into a {summary, key_points} JSON.

    Falls back to {"summary": raw, "key_points": []} when the response is
    not valid JSON or lacks a summary.
    """
    fallback = {"summary": raw, "key_points": []}
    try:
        data = json.loads(_extract_json_blob(raw))
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


def _parse_hits(raw: str) -> str:
    """Parse agy's semantic-search response into a JSON array of hits.

    Falls back to {"raw_markdown": raw} when the response is not a JSON
    array, so the caller can tell parsing failed.
    """
    try:
        data = json.loads(_extract_json_blob(raw))
    except (ValueError, TypeError):
        return json.dumps({"raw_markdown": raw})
    if not isinstance(data, list):
        return json.dumps({"raw_markdown": raw})
    return json.dumps(data)


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
        prompt, model, sandbox, trust, session, cwd, effort
    )
    if key is None:
        return None, None
    return key, _cache_get(key)


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
        effort: agy reasoning effort (low/medium/high), passed straight
            through to --effort. Included in the cache key alongside
            model, since it affects the response.

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
        "sandbox=%s model=%s effort=%s cwd=%s session=%s",
        len(prompt),
        files,
        directory,
        raw,
        trust,
        sandbox,
        model or "default",
        effort or "default",
        cwd,
        session,
    )

    history = _session_prefix(cwd, session) if in_session else None
    assembled = _assemble_prompt(prompt, raw, files, directory, history)
    cache_key, cached = _try_cache(
        use_cache, assembled, model, sandbox, trust, session, cwd, effort
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
    )
    # Bracketed returns are errors/status, not real conversations, so
    # only append to the session (and cache) when agy actually responded.
    if not response.startswith("["):
        logger.info(f"dispatch: success, response_len={len(response)}")
        if in_session:
            _append_turn(cwd, session, prompt, response)
        if cache_key is not None:
            _cache_put(cache_key, response, model, effort)
    else:
        logger.warning(f"dispatch: returned status/error response: {response}")
    return response


# Background-job subsystem. agy print mode blocks for up to PRINT_TIMEOUT;
# these let a long prompt run off-thread so Claude can keep working and
# collect the result later via gemini_poll. Jobs are in-memory only — a
# server restart drops them. Concurrency is capped by the pool so a
# fan-out cannot spawn unbounded agy processes.
JOB_POOL_SIZE = 4
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
_job_pool = ThreadPoolExecutor(max_workers=JOB_POOL_SIZE)


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


def _run_job(job_id: str, kwargs: dict[str, Any]) -> None:
    """Worker body: run the prompt, then record the outcome on the job.

    Background jobs are session-free by construction (gemini_start takes no
    session param), so concurrent workers never race on a transcript file.
    A bracketed response (agy error/status) is recorded as an "error".
    """
    try:
        response = _dispatch(**kwargs)
    except Exception as exc:  # worker must never crash silently
        response = f"[Job crashed: {exc}]"

    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.status = "error" if response.startswith("[") else "done"
        job.response = response
        job.finished_at = time.time()


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
        effort: Select agy's reasoning effort (low/medium/high) — a
            cheap/fast-vs-capable tradeoff independent of model choice.
            Defaults to agy's own default when omitted.
        sandbox: If True, agy explores the filesystem (like trust) but
            runs under terminal restrictions and does not blindly
            auto-approve actions — the safe middle ground between
            read-only and trust. Ignored when trust=True.
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
    )


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

    Tries to extract a JSON object from raw (stripping code fences),
    validates required keys are present, then injects git_sha. Falls
    back to a minimal envelope with raw_markdown on parse failure.

    Args:
        raw: agy's raw index response.
        cwd: Project root; used to inject the current git SHA.

    Returns:
        A JSON string with structured index fields plus git_sha, or
        (on parse failure) a minimal JSON envelope with git_sha and
        raw_markdown.
    """
    git_sha = _get_git_sha(cwd)
    fallback: dict[str, Any] = {
        "git_sha": git_sha,
        "raw_markdown": raw,
    }
    try:
        data = json.loads(_extract_json_blob(raw))
    except (ValueError, TypeError):
        return json.dumps(fallback)
    if not isinstance(data, dict) or not _validate_index_keys(data):
        return json.dumps(fallback)
    data["git_sha"] = git_sha
    return json.dumps(data)


@mcp.tool()
def gemini_index(cwd: str, model: str | None = None) -> str:
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

    Returns:
        A JSON string with keys: summary, entry_points, modules,
        exception_types, architecture, git_sha. On parse failure,
        {"git_sha": <sha>, "raw_markdown": <raw>}. On agy error,
        returns a bracketed error string.
    """
    raw = _dispatch(INDEX_PROMPT, raw=True, sandbox=True, cwd=cwd, model=model)
    if raw.startswith("["):
        return raw
    result = _parse_index(raw, cwd)
    _update_last_index(cwd)
    return result


@mcp.tool()
def gemini_review(
    cwd: str, diff: str | None = None, model: str | None = None
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
    return _dispatch(f"{REVIEW_PROMPT}\n\n[DIFF]\n{diff}", model=model)


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
    cwd: str, error: str, model: str | None = None
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

    Returns:
        Ranked root-cause hypotheses, or a bracketed error string.
    """
    prompt = (
        "Diagnose this error against the codebase. Trace it to its likely "
        "source and return the most probable root causes, ranked, each "
        "with the specific file(s) and line(s) to check and why. Be "
        f"concrete.\n\n[ERROR]\n{error}"
    )
    return _dispatch(prompt, sandbox=True, cwd=cwd, model=model)


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
        effort: Select agy's reasoning effort (low/medium/high). Defaults
            to agy's own default when omitted.
        sandbox: If True, agy explores under terminal restrictions.
            Ignored when trust=True.
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
    }
    job_id = uuid.uuid4().hex[:8]
    with _jobs_lock:
        _evict_jobs()
        _jobs[job_id] = _Job(status="running", started_at=time.time())
    _job_pool.submit(_run_job, job_id, kwargs)
    return (
        f"Started background job {job_id}. Poll it with "
        f'gemini_poll("{job_id}") — do other work first, then check back.'
    )


@mcp.tool()
def gemini_poll(job_id: str) -> str:
    """Check a background job started with gemini_start.

    Args:
        job_id: The id returned by gemini_start.

    Returns:
        While running, a bracketed status with elapsed seconds. When done,
        agy's response. On error, a bracketed message. For an unknown id,
        a bracketed not-found note.
    """
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return (
                f"[Unknown job {job_id} — it may have been evicted or "
                "never existed. Run gemini_jobs to list current jobs.]"
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
    return response or "[No response from Antigravity]"


@mcp.tool()
def gemini_jobs() -> str:
    """List background jobs and their status.

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
def gemini_reset(cwd: str | None = None) -> str:
    """Clear a project's default session so the next call starts fresh.

    Deletes the reserved "__default__" session (the one continue_session
    resumes) for cwd. Named sessions are untouched — manage those with
    gemini_sessions and gemini_session_delete. Sessions are project-
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
def gemini_sessions(cwd: str) -> str:
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
def gemini_session_show(cwd: str, session: str) -> str:
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
def gemini_session_delete(cwd: str, session: str) -> str:
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
def gemini_cache_clear() -> str:
    """Delete all cached Antigravity responses.

    gemini_prompt reuses a stored response when the same prompt is
    re-sent against an unchanged repo (matched by git HEAD + working-tree
    state). Clear the cache to force fresh runs — e.g. after changing
    agy's config or its default model, or to reclaim disk.
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


@mcp.tool()
def gemini_models() -> str:
    """List the models available to Antigravity (agy).

    Returns the names you can pass as the `model` argument to
    gemini_prompt. Requires sign-in — if not authenticated, returns the
    sign-in instruction instead.
    """
    try:
        result = subprocess.run(
            ["agy", "models"], capture_output=True, text=True, timeout=30
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
            ["agy", "agents"], capture_output=True, text=True, timeout=30
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
            ["agy", "--version"], capture_output=True, text=True, timeout=10
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

    Every lookup is non-fatal — any failure returns an empty string, so a
    missing nicety never downgrades a READY status. Account and version
    freshness are intentionally omitted: agy exposes no whoami command,
    and `agy update` performs an update rather than a safe check.

    Returns:
        A newline-prefixed suffix listing available models, or "".
    """
    try:
        result = subprocess.run(
            ["agy", "models"], capture_output=True, text=True, timeout=15
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""

    if result.returncode != 0:
        return ""

    models = result.stdout.strip()
    if not models or _needs_auth(models + result.stderr):
        return ""

    shown = "\n".join(models.splitlines()[:10])
    return f"\nModels available:\n{shown}"


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
            ["agy", "--version"], capture_output=True, text=True, timeout=10
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
    if cwd is not None:
        suffix += _format_last_index(_load_project_state(cwd))
    return f"READY — agy {version}{suffix}"


if __name__ == "__main__":
    mcp.run()
