"""Detect drift in the subset of dekko's CLI surface `server.py` depends on.

Standalone maintenance script, NOT part of the MCP server or its tool
surface. Castor shells out to the `dekko` CLI that ships in its own
environment and relies on specific flags (e.g. `--no-regen`, `--budget`)
and JSON fields (e.g. `sites` in `query callers --json`). dekko releases
often, so this surface can drift silently.

This script runs `dekko <subcommand> --help` for each tracked subcommand,
then `dekko query callers/symbol --json` on this repo, and diffs the
tracked flags, query actions and JSON keys against a checked-in snapshot.
Drift outside the tracked subset is ignored on purpose.

It does not import `server.py`. It finds dekko the way Castor does, as the
`dekko` script beside the running interpreter, so run it with the project
environment from the repo root:

Usage:
    uv run python scripts/check_dekko_surface.py
    uv run python scripts/check_dekko_surface.py --update-snapshot

Exit codes:
    0  no drift in the tracked surface (or snapshot updated successfully)
    1  drift detected in the tracked surface
    2  dekko is missing, or its output is not what this script expects
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = Path(__file__).parent / "dekko_surface_snapshot.json"

# The flags Castor passes, per subcommand. Keep in sync with server.py.
TRACKED_FLAGS = {
    "map": ["--if-stale", "--quiet"],
    "query": ["--sites", "--no-regen", "--budget", "--json"],
    "outline": ["--budget", "--no-regen"],
    "context": ["--with-source", "--budget", "--no-regen"],
    "search": ["--budget", "--no-regen"],
    "trace": ["--no-regen"],
}

# `dekko query` actions Castor calls.
TRACKED_ACTIONS = ["callers", "symbol"]

# JSON probes: query action -> extra flags. Each runs against PROBE_SYMBOL
# in this repo.
PROBE_SYMBOL = "_agent_grep"
JSON_PROBES = {
    "callers": ["--sites"],
    "symbol": [],
}

_FLAG_RE = re.compile(r"--[A-Za-z][\w-]*")
_ACTIONS_RE = re.compile(r"\{([a-z,]+)\}")


def _dekko_bin() -> Path:
    """Return the dekko script beside the running interpreter.

    Returns:
        Path to the dekko executable.

    Raises:
        SystemExit: With code 2 if the script does not exist.
    """
    path = Path(sys.executable).parent / "dekko"
    if not path.is_file():
        print(
            f"[Error] dekko not found at {path}. Run this with the "
            "project environment: uv run python "
            "scripts/check_dekko_surface.py",
            file=sys.stderr,
        )
        raise SystemExit(2)

    return path


def _run(args: list[str], what: str, quiet: bool = False) -> str:
    """Run dekko in the repo root and return stdout.

    Args:
        args: Arguments after the dekko binary.
        what: Short label for error messages.
        quiet: Allow empty stdout (for `--quiet` commands).

    Returns:
        The captured stdout.

    Raises:
        SystemExit: With code 2 on a timeout, non-zero exit or unexpected
            empty output.
    """
    try:
        result = subprocess.run(
            [str(_dekko_bin()), *args],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=REPO_ROOT,
        )
    except subprocess.TimeoutExpired as exc:
        print(f"[Error] {what} timed out.", file=sys.stderr)
        raise SystemExit(2) from exc

    if result.returncode != 0 or not (quiet or result.stdout.strip()):
        print(
            f"[Error] {what} failed (returncode={result.returncode}): "
            f"{result.stderr.strip()[:300]}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    return result.stdout


def _parse_flags(help_text: str) -> list[str]:
    """Collect every long flag named in an option line of `--help`."""
    flags = set()
    for line in help_text.splitlines():
        if re.match(r"^\s{2}-", line):
            flags.update(_FLAG_RE.findall(line.split("  ", 2)[1]))

    return sorted(flags)


def _parse_actions(help_text: str) -> list[str]:
    """Return the action names in `dekko query --help`'s usage block."""
    for group in _ACTIONS_RE.findall(help_text):
        names = group.split(",")
        if "callers" in names:
            return sorted(names)

    return []


def _result_keys(payload: dict) -> list[str]:
    """Return the keys of the first item of `results`, if it is a list."""
    results = payload.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        return sorted(results[0])

    return []


def _probe_json(action: str, extra: list[str]) -> dict[str, list[str]]:
    """Run one `query --json` probe and record its JSON keys.

    Args:
        action: The query action, e.g. "callers".
        extra: Extra flags for this action.

    Returns:
        The top-level keys and the first result item's keys.

    Raises:
        SystemExit: With code 2 if the output is not a JSON object.
    """
    out = _run(
        ["query", action, PROBE_SYMBOL, *extra, "--no-regen", "--json"],
        f"dekko query {action} --json",
    )
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        print(
            f"[Error] dekko query {action} --json is not JSON.",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc

    if not isinstance(payload, dict):
        print(
            f"[Error] dekko query {action} --json is not an object.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    return {"top_level": sorted(payload), "result": _result_keys(payload)}


def _collect_live() -> dict:
    """Gather the live tracked surface from dekko.

    Returns:
        A dict with the dekko version, flags per subcommand, query actions
        and JSON keys per probe.

    Raises:
        SystemExit: With code 2 if `dekko query --help` looks wrong.
    """
    version = _run(["--version"], "dekko --version").strip()
    _run(
        ["map", "--if-stale", "--quiet"],
        "dekko map --if-stale",
        quiet=True,
    )

    help_texts = {
        sub: _run([sub, "--help"], f"dekko {sub} --help")
        for sub in TRACKED_FLAGS
    }
    if "usage: dekko" not in help_texts["query"]:
        print("[Error] Unexpected `dekko query --help`.", file=sys.stderr)
        raise SystemExit(2)

    return {
        "version": version,
        "flags": {sub: _parse_flags(text) for sub, text in help_texts.items()},
        "actions": _parse_actions(help_texts["query"]),
        "json": {
            name: _probe_json(name, extra)
            for name, extra in JSON_PROBES.items()
        },
    }


def _tracked_view(live: dict) -> dict:
    """Reduce a full surface to the tracked subset that is diffed."""
    return {
        "flags": {
            sub: [f for f in TRACKED_FLAGS[sub] if f in live["flags"][sub]]
            for sub in TRACKED_FLAGS
        },
        "actions": [a for a in TRACKED_ACTIONS if a in live["actions"]],
        "json": live["json"],
    }


def _diff_lists(label: str, old: list[str], new: list[str]) -> list[str]:
    """Report names added to or removed from a tracked list."""
    issues = [f"  REMOVED {label}: {n!r}" for n in old if n not in new]
    issues += [f"  NEW {label}: {n!r}" for n in new if n not in old]

    return issues


def _diff(snapshot: dict, live: dict) -> list[str]:
    """Compare the tracked view of the live surface with the snapshot.

    Args:
        snapshot: The checked-in snapshot.
        live: The full live surface from `_collect_live`.

    Returns:
        Human-readable drift lines; empty if nothing changed.
    """
    view = _tracked_view(live)
    issues = []
    for sub in TRACKED_FLAGS:
        old = snapshot.get("flags", {}).get(sub, [])
        issues += _diff_lists(f"flag of `{sub}`", old, view["flags"][sub])

    issues += _diff_lists(
        "query action", snapshot.get("actions", []), view["actions"]
    )
    for name, keys in view["json"].items():
        old = snapshot.get("json", {}).get(name, {})
        for part in ("top_level", "result"):
            issues += _diff_lists(
                f"`query {name} --json` {part} key",
                old.get(part, []),
                keys[part],
            )

    return issues


def _save_snapshot(live: dict) -> None:
    """Write the tracked view of the live surface to the snapshot file."""
    payload = {"version": live["version"], **_tracked_view(live)}
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    SNAPSHOT_PATH.write_text(text)


def main() -> int:
    """Entry point. Returns the process exit code (see module docstring)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update-snapshot",
        action="store_true",
        help="Overwrite the checked-in snapshot with the live surface.",
    )
    args = parser.parse_args()

    live = _collect_live()

    if args.update_snapshot:
        _save_snapshot(live)
        print(f"Snapshot updated: {SNAPSHOT_PATH}")
        return 0

    if not SNAPSHOT_PATH.exists():
        print(
            f"[Error] No snapshot at {SNAPSHOT_PATH}. "
            "Run with --update-snapshot.",
            file=sys.stderr,
        )
        return 2

    issues = _diff(json.loads(SNAPSHOT_PATH.read_text()), live)
    if issues:
        print("dekko CLI surface drift detected in the tracked subset:")
        print("\n".join(issues))
        print(
            "\nIf this is expected (a deliberate dekko upgrade), update "
            "server.py accordingly, then re-run with --update-snapshot."
        )
        return 1

    print(
        "No drift: the tracked dekko flags, actions and JSON keys match "
        f"the checked-in snapshot ({SNAPSHOT_PATH.name}, {live['version']})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
