#!/usr/bin/env python3
"""Install claude-castor: registers the MCP server and installs slash commands."""

import shutil
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).parent.resolve()
COMMANDS_SRC = REPO_DIR / "commands"
COMMANDS_DEST = Path.home() / ".claude" / "commands"


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
            claude, "mcp", "add", "-s", "user", "claude-castor",
            str(uv), "--",
            "run", "--directory", str(REPO_DIR), "server.py",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  Error: {result.stderr.strip() or result.stdout.strip()}")
        sys.exit(1)
    print(f"  Registered — using uv at {uv}")


def install_commands() -> None:
    COMMANDS_DEST.mkdir(parents=True, exist_ok=True)
    print("Installing slash commands...")
    for src in sorted(COMMANDS_SRC.glob("*.md")):
        dest = COMMANDS_DEST / src.name
        shutil.copy(src, dest)
        print(f"  /{src.stem}")


def main() -> None:
    print(f"Installing claude-castor from {REPO_DIR}\n")

    claude = find_claude()
    uv = find_uv()

    register_mcp(claude, uv)
    install_commands()

    print(
        "\nDone! Restart Claude Code, then:\n"
        "  /castor-status    — verify the Gemini CLI is ready\n"
        "  /castor-explore   — explore the current codebase\n"
        "  /castor-research  — deep-dive on a specific topic"
    )


if __name__ == "__main__":
    main()
