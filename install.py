#!/usr/bin/env python3
"""Register the claude-castor MCP server and install slash commands."""

import shutil
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).parent.resolve()
# Namespaced under castor/ so the commands install as /castor:<name>.
COMMANDS_SRC = REPO_DIR / "commands" / "castor"
COMMANDS_DEST = Path.home() / ".claude" / "commands" / "castor"
# Flat files from pre-namespace installs (/castor-<name>) to clean up.
LEGACY_COMMANDS_DIR = Path.home() / ".claude" / "commands"
LEGACY_COMMAND_STEMS = ("auth", "explore", "research", "status")
GEMINI_MD_SRC = REPO_DIR / "GEMINI.md"
GEMINI_MD_DEST = Path.home() / ".claude" / "GEMINI.md"


def find_uv() -> Path:
    uv = shutil.which("uv")
    if not uv:
        print(
            "Error: uv not found.\n"
            "Install it from https://docs.astral.sh/uv/\n"
            "  macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh\n"
            "  Windows:     powershell -c "
            '"irm https://astral.sh/uv/install.ps1 | iex"'
        )
        sys.exit(1)
    return Path(uv)


def find_claude() -> str:
    claude = shutil.which("claude")
    if not claude:
        print(
            "Error: claude CLI not found.\n"
            "Install Claude Code from https://claude.ai/code"
        )
        sys.exit(1)
    return claude


def register_mcp(claude: str, uv: Path) -> None:
    print("Registering MCP server...")
    result = subprocess.run(
        [
            claude,
            "mcp",
            "add",
            "-s",
            "user",
            "claude-castor",
            str(uv),
            "--",
            "run",
            "--directory",
            str(REPO_DIR),
            "server.py",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  Error: {result.stderr.strip() or result.stdout.strip()}")
        sys.exit(1)
    print(f"  Registered — using uv at {uv}")


def remove_legacy_commands() -> None:
    """Delete flat castor-*.md files from pre-namespace installs."""
    for stem in LEGACY_COMMAND_STEMS:
        legacy = LEGACY_COMMANDS_DIR / f"castor-{stem}.md"
        if legacy.exists():
            legacy.unlink()
            print(f"  removed legacy /castor-{stem}")


def install_commands() -> None:
    COMMANDS_DEST.mkdir(parents=True, exist_ok=True)
    print("Installing slash commands...")
    remove_legacy_commands()
    for src in sorted(COMMANDS_SRC.glob("*.md")):
        dest = COMMANDS_DEST / src.name
        shutil.copy(src, dest)
        print(f"  /castor:{src.stem}")

    shutil.copy(GEMINI_MD_SRC, GEMINI_MD_DEST)
    print(f"  GEMINI.md → {GEMINI_MD_DEST}")


def main() -> None:
    print(f"Installing claude-castor from {REPO_DIR}\n")

    claude = find_claude()
    uv = find_uv()

    register_mcp(claude, uv)
    install_commands()

    print(
        "\nDone! Restart Claude Code, then:\n"
        "  /castor:status    — verify the Antigravity (agy) CLI is ready\n"
        "  /castor:auth      — sign in if not authenticated\n"
        "  /castor:explore   — explore the current codebase\n"
        "  /castor:research  — deep-dive on a specific topic"
    )


if __name__ == "__main__":
    main()
