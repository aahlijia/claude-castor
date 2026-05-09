import fnmatch
import os
import subprocess
from pathlib import Path

from fastmcp import FastMCP

mcp = FastMCP("claude-castor")

SYSTEM_INSTRUCTION = """\
You are a technical assistant. Respond with precision and structure.
- Use clear headings and bullet points where appropriate
- Include file names, line numbers, and symbol names when referencing code
- Be concise — avoid filler; every sentence should carry information
- If asked to index or summarize, produce output Claude can use as context\
"""

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


def _is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(8192)
    except OSError:
        return True


def _file_block(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        return f"[FILE: {path}]\n{content}\n"
    except OSError as e:
        return f"[FILE: {path}]\n[read error: {e}]\n"


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
        elif _is_binary(p):
            blocks.append(f"[FILE: {raw}]\n[skipped: binary]\n")
        else:
            blocks.append(_file_block(p))
    return "\n".join(blocks)


def _inline_directory(directory: str) -> str:
    root = Path(directory)
    ignore_patterns = _load_gitignore(root)
    blocks = []
    skipped = []
    total = 0

    for rel_root, dirs, files in os.walk(root):
        dirs[:] = [
            d for d in dirs
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
            if _is_binary(file_path):
                continue
            if file_path.stat().st_size > MAX_FILE_BYTES:
                skipped.append(rel)
                continue

            block = _file_block(file_path)
            if total + len(block) > MAX_TOTAL_BYTES:
                skipped.append(rel)
                continue

            blocks.append(block)
            total += len(block)

    result = "\n".join(blocks)
    if skipped:
        n = len(skipped)
        result += f"\n\n[Note: {n} file(s) skipped — too large or binary]"
    return result


def _run_gemini(prompt: str, trust: bool = False) -> str:
    cmd = ["gemini", "--skip-trust"]
    env = None
    if trust:
        cmd = ["gemini"]
        env = {**os.environ, "GEMINI_CLI_TRUST_WORKSPACE": "true"}
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=300,
            env=env
        )
    except FileNotFoundError:
        return (
            "[Error: `gemini` CLI not found. "
            "Run: npm install -g @google/gemini-cli]"
        )
    except subprocess.TimeoutExpired:
        return (
            "[Error: Gemini timed out after 300s. "
            "Try passing specific files instead of a full directory.]"
        )

    if result.returncode != 0 and result.stderr.strip():
        return f"[Gemini error]\n{result.stderr.strip()}"

    return result.stdout.strip() or "[No response from Gemini]"


@mcp.tool()
def gemini_prompt(
    prompt: str,
    files: list[str] | None = None,
    directory: str | None = None,
    raw: bool = False,
    trust: bool = False,
) -> str:
    """Send a prompt to Gemini and return the response.

    Use this to offload large-context work — file exploration, indexing,
    summarization, cross-file analysis, research. Claude should construct
    a thoughtful prompt that leads with task context (what you are doing
    and where you are in the task), followed by a specific ask.
    See GEMINI.md for the full prompting guide.

    Only pass `files` or `directory` if the user explicitly mentioned them.
    Do not crawl the filesystem on the user's behalf — that is Gemini's job.

    Args:
        prompt: The fully-formed prompt Claude has constructed.
        files: Absolute paths to files to inline into the prompt.
        directory: A directory whose contents should be inlined.
        raw: If True, skip structured response instructions.
        trust: If True, run Gemini in full agent mode with filesystem
            access. Only use after the user has completed OAuth via
            `gemini_setup`. Default is False (safe headless mode).
    """
    parts: list[str] = []

    if not raw:
        parts.append(SYSTEM_INSTRUCTION)

    parts.append(prompt)

    if files:
        parts.append(_inline_files(files))

    if directory:
        parts.append(_inline_directory(directory))

    return _run_gemini("\n\n".join(parts), trust=trust)


@mcp.tool()
def gemini_setup() -> str:
    """Return setup instructions for Gemini OAuth and agent mode.

    Call this when the user wants to set up Gemini for the first time
    or wants to enable deep research mode with filesystem access.
    """
    return """\
To enable full Gemini agent mode with filesystem access, complete
Google OAuth once (first time only).

Tell the user to type the following in the Claude Code prompt:

    ! gemini --skip-trust

They should:
  1. Complete the Google OAuth flow in the browser
  2. Exit with /exit or Ctrl+C

No directory trust step is needed — the MCP server sets
GEMINI_CLI_TRUST_WORKSPACE automatically when trust=True is used.

Once OAuth is complete, call gemini_prompt with trust=True for full
agent mode: Gemini can explore the filesystem, search code, and
follow imports on its own.\
"""


@mcp.tool()
def gemini_status() -> str:
    """Check that the gemini CLI is installed and authenticated.

    Run before first use or when troubleshooting. Returns a status
    string with fix instructions if not ready.
    """
    try:
        version_result = subprocess.run(
            ["gemini", "--version"],
            capture_output=True,
            text=True,
            timeout=10
        )
    except FileNotFoundError:
        return (
            "NOT INSTALLED: `gemini` CLI not found.\n"
            "Fix: npm install -g @google/gemini-cli"
        )

    if version_result.returncode != 0:
        return (
            "NOT INSTALLED: `gemini` CLI not found.\n"
            "Fix: npm install -g @google/gemini-cli"
        )

    version = version_result.stdout.strip()

    try:
        auth_result = subprocess.run(
            ["gemini", "--skip-trust"],
            input="Reply with exactly the word: OK",
            capture_output=True,
            text=True,
            timeout=30
        )
    except FileNotFoundError:
        return "NOT INSTALLED: `gemini` CLI not found."

    if auth_result.returncode != 0:
        return (
            f"NOT AUTHENTICATED: CLI found ({version}) but auth failed.\n"
            "Fix: run `gemini` interactively to complete Google OAuth.\n"
            f"Error: {auth_result.stderr.strip()}"
        )

    return f"READY — {version}"


if __name__ == "__main__":
    mcp.run()
