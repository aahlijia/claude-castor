"""Detect drift in the subset of agy's CLI surface `server.py` depends on.

Standalone maintenance script — NOT part of the MCP server or its tool
surface. `server.py` hardcodes assumptions about agy's flags (e.g. `--print`
takes its value inline, not via stdin; `--add-dir` is not inherited from
cwd) and subcommands (`models`, `agents`). agy auto-updates and Castor pins
no version, so this surface has drifted silently before (see the 1.1.9
regression noted in CHANGELOG.md) and will again.

This script runs `agy --help` and diffs the flags/subcommands `server.py`
actually consumes against a checked-in snapshot, flagging additions,
removals, and description changes for just that tracked subset. It ignores
drift in untracked flags entirely — this is not a full agy changelog, only
an early-warning check for the specific surface Castor relies on.

Usage:
    python3 scripts/check_agy_surface.py                 # check for drift
    python3 scripts/check_agy_surface.py --update-snapshot  # refresh snapshot

Exit codes:
    0  no drift in the tracked surface (or snapshot updated successfully)
    1  drift detected in the tracked surface
    2  could not run `agy --help` (not installed, or unexpected output)
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


SNAPSHOT_PATH = Path(__file__).parent / "agy_surface_snapshot.json"

# The exact flags server.py builds agy commands with today (_build_agy_cmd)
# plus the subcommands it shells out to directly (_list_models, gemini_agents,
# gemini_status). Keep this list in sync with server.py — that sync is the
# whole point of this script existing.
TRACKED_FLAGS = [
    "--print",
    "--print-timeout",
    "--dangerously-skip-permissions",
    "--sandbox",
    "--add-dir",
    "--model",
    "--disable-slash-commands",
    "--effort",
    "--agent",
    "--output-format",
    "--json-schema",
]

TRACKED_SUBCOMMANDS = [
    "models",
    "agents",
]

# Matches a `--help` line of the form "  --flag   description text" or
# "  subcommand   description text" — two-plus leading spaces, a token with
# no internal whitespace, then two-plus spaces, then free-text description.
_ENTRY_RE = re.compile(r"^\s{2,}(\S+)\s{2,}(.+?)\s*$")


def _run_agy_help() -> str:
    """Run `agy --help` and return its combined stdout+stderr.

    Returns:
        The raw help text.

    Raises:
        SystemExit: With code 2 if `agy` is not installed or the call
            otherwise fails to produce output.
    """
    try:
        result = subprocess.run(
            ["agy", "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError as exc:
        print(
            "[Error] `agy` CLI not found on PATH. Install: "
            "curl -fsSL https://antigravity.google/cli/install.sh | bash",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    except subprocess.TimeoutExpired as exc:
        print("[Error] `agy --help` timed out.", file=sys.stderr)
        raise SystemExit(2) from exc

    text = (result.stdout or "") + (result.stderr or "")
    if not text.strip():
        print(
            "[Error] `agy --help` produced no output "
            f"(returncode={result.returncode}).",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return text


def _parse_help(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Split `agy --help` output into flags and subcommands.

    agy's help text has two sections: a leading block of `--flag` entries,
    then a "Available subcommands:" block of bare subcommand names. Both
    use the same two-space-indent, aligned-columns layout, so the same
    line regex handles either half; only the split point differs.

    Args:
        text: Raw `agy --help` output.

    Returns:
        A (flags, subcommands) pair, each mapping name -> description
        exactly as printed (e.g. "--model" -> "Model for the current CLI
        session", "models" -> "List available models").
    """
    marker = "Available subcommands:"
    if marker in text:
        flags_text, _, subcommands_text = text.partition(marker)
    else:
        flags_text, subcommands_text = text, ""

    flags = {}
    for line in flags_text.splitlines():
        match = _ENTRY_RE.match(line)
        if match and match.group(1).startswith("-"):
            flags[match.group(1)] = match.group(2)

    subcommands = {}
    for line in subcommands_text.splitlines():
        match = _ENTRY_RE.match(line)
        if match and not match.group(1).startswith("-"):
            subcommands[match.group(1)] = match.group(2)

    return flags, subcommands


def _load_snapshot() -> dict:
    """Load the checked-in snapshot, or an empty shape if absent."""
    if not SNAPSHOT_PATH.exists():
        return {"flags": {}, "subcommands": {}}
    return json.loads(SNAPSHOT_PATH.read_text())


def _save_snapshot(flags: dict[str, str], subcommands: dict[str, str]) -> None:
    """Write the current tracked surface to the snapshot file."""
    payload = {
        "flags": {name: flags.get(name, "") for name in TRACKED_FLAGS},
        "subcommands": {
            name: subcommands.get(name, "") for name in TRACKED_SUBCOMMANDS
        },
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    SNAPSHOT_PATH.write_text(text)


def _diff_tracked(
    label: str,
    tracked_names: list[str],
    live: dict[str, str],
    snapshot: dict[str, str],
) -> list[str]:
    """Compare one tracked-name category (flags or subcommands).

    Args:
        label: "flag" or "subcommand", used in the report lines.
        tracked_names: The names server.py depends on for this category.
        live: name -> description, freshly parsed from `agy --help`.
        snapshot: name -> description, from the checked-in snapshot.

    Returns:
        Human-readable drift lines; empty if nothing changed.
    """
    issues = []
    for name in tracked_names:
        was_known = name in snapshot and snapshot[name] != ""
        is_present = name in live
        if was_known and not is_present:
            issues.append(f"  REMOVED {label}: {name!r} no longer in --help")
        elif not was_known and is_present:
            issues.append(
                f"  NEW (now tracked) {label}: {name!r} present in --help "
                "but missing from the snapshot"
            )
        elif was_known and is_present and snapshot[name] != live[name]:
            issues.append(
                f"  CHANGED {label} description: {name!r}\n"
                f"    was: {snapshot[name]!r}\n"
                f"    now: {live[name]!r}"
            )
    return issues


def main() -> int:
    """Entry point. Returns the process exit code (see module docstring)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update-snapshot",
        action="store_true",
        help="Overwrite the checked-in snapshot with the live agy surface.",
    )
    args = parser.parse_args()

    help_text = _run_agy_help()
    live_flags, live_subcommands = _parse_help(help_text)

    missing_tracked_flags = [f for f in TRACKED_FLAGS if f not in live_flags]
    if missing_tracked_flags:
        print(
            "[Warning] Tracked flags not found in live --help output "
            f"(check the parser or agy's install): {missing_tracked_flags}",
            file=sys.stderr,
        )

    if args.update_snapshot:
        _save_snapshot(live_flags, live_subcommands)
        print(f"Snapshot updated: {SNAPSHOT_PATH}")
        return 0

    snapshot = _load_snapshot()
    issues = _diff_tracked(
        "flag", TRACKED_FLAGS, live_flags, snapshot.get("flags", {})
    )
    issues += _diff_tracked(
        "subcommand",
        TRACKED_SUBCOMMANDS,
        live_subcommands,
        snapshot.get("subcommands", {}),
    )

    if issues:
        print("agy CLI surface drift detected in tracked flags/subcommands:")
        print("\n".join(issues))
        print(
            "\nIf this is expected (a deliberate agy upgrade), update "
            "server.py accordingly, then re-run with --update-snapshot."
        )
        return 1

    print(
        "No drift: all tracked agy flags/subcommands match the "
        f"checked-in snapshot ({SNAPSHOT_PATH.name})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
