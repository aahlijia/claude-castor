"""Show where Claude Code's tokens go, from its own session transcripts.

Castor exists to save Claude tokens and time, so this measures what there
is to save: how many turns sessions and their subagents take, how much
context each turn re-reads, what each tool puts into context and how often
Castor's tools are called at all. Run it before and after a change that is
meant to save something.

It reads the transcripts under ~/.claude/projects and prints counts only:
tool names, the first word of a shell command, sizes and times. No prompt
text, file contents or command arguments are printed.

    python3 scripts/usage_audit.py [--days 30] [--project NAME] [--root DIR]

Token weight adds the four token counts by their list-price ratios (input
1, cache read 0.1, cache write 1.25, output 5). It compares kinds of
spending; it is not dollars, and subagents may run on a cheaper model.
"""

import argparse
import json
import re
import statistics
import time
from collections import Counter
from collections import defaultdict
from collections.abc import Iterable
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_ROOT = Path.home() / ".claude" / "projects"

# Price of each token count, relative to one fresh input token.
WEIGHTS = {
    "input_tokens": 1.0,
    "cache_read_input_tokens": 0.1,
    "cache_creation_input_tokens": 1.25,
    "output_tokens": 5.0,
}

WEIGHT_LABELS = {
    "cache_read_input_tokens": "re-reading context",
    "cache_creation_input_tokens": "writing context",
    "output_tokens": "output",
    "input_tokens": "fresh input",
}

EDIT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})

# Shell commands that print a file; NQC implementers read this way.
READERS = frozenset({"cat", "nl", "head", "tail", "sed", "awk"})

# A Bash line that changes a file in place: NQC implementers edit mostly
# through Python heredocs that write a file back, and `sed -i`.
_BASH_WRITE_RE = re.compile(
    r"\b(?:sed|perl)\s+-[a-zA-Z]*i|\bgit\s+apply\b|\bapply_patch\b"
    r"|\.write_text\(|\bopen\([^)]*[\"'][wa]\+?[\"']"
)

CASTOR_PREFIX = "mcp__claude-castor__"

# `cd dir &&`, `VAR=value` and an opening bracket before the real command.
_LEAD_RE = re.compile(
    r"^(?:cd\s+\S+\s*(?:&&|;)\s*|[A-Za-z_]\w*=\S*\s+|\(\s*)+"
)

# Commands whose first argument says what they do, and the flags of each
# that take a value (so the value is not read as that argument).
_VALUE_FLAGS = {
    "git": {"-C", "-c"},
    "uv": {
        "--with",
        "--directory",
        "--extra",
        "--python",
        "-p",
        "--project",
        "--group",
        "--env-file",
        "--with-requirements",
    },
    "uvx": {"--with", "--from", "--python", "-p"},
    "python": set(),
    "python3": set(),
    "bash": set(),
    "sh": set(),
    "gh": set(),
    "dotnet": set(),
}

HEAD_CHARS = 40  # longest command head kept
TOP_ROWS = 12  # rows shown in each table


@dataclass
class Side:
    """Totals for one kind of transcript: main sessions or subagents.

    Attributes:
        transcripts: Transcript files read.
        turns: Model calls (one per assistant message).
        tokens: Token counts by usage key.
        calls: Tool calls by tool.
        chars: Characters of tool results by tool.
        waits: Seconds from each call to its result, by tool.
    """

    transcripts: int = 0
    turns: int = 0
    tokens: Counter = field(default_factory=Counter)
    calls: Counter = field(default_factory=Counter)
    chars: Counter = field(default_factory=Counter)
    waits: dict[str, list[float]] = field(
        default_factory=lambda: defaultdict(list)
    )


@dataclass
class Pool:
    """Subagent transcripts of one kind: those that edit, or never do."""

    count: int = 0
    turns: int = 0
    weight: float = 0.0


@dataclass
class AgentRun:
    """One subagent transcript.

    Attributes:
        turns: Model calls it made.
        weight: Its token weight.
        first_edit: The turn of its first edit; None if it never edited.
        reads_before: Read calls and Bash reads before that edit.
    """

    turns: int
    weight: float
    first_edit: int | None
    reads_before: int


@dataclass
class Audit:
    """Everything read from a set of transcripts.

    Attributes:
        main: Main sessions.
        sub: Subagents.
        bash_calls: Bash calls by the first command of the line.
        bash_chars: Characters of Bash output by that command.
        editing: Subagents that called Edit, Write or NotebookEdit.
        reading: Subagents that never did.
        kinds: Each subagent run, by the agent type its meta file names.
    """

    main: Side = field(default_factory=Side)
    sub: Side = field(default_factory=Side)
    bash_calls: Counter = field(default_factory=Counter)
    bash_chars: Counter = field(default_factory=Counter)
    editing: Pool = field(default_factory=Pool)
    reading: Pool = field(default_factory=Pool)
    kinds: dict[str, list[AgentRun]] = field(
        default_factory=lambda: defaultdict(list)
    )


@dataclass
class _Call:
    """A tool call waiting for its result."""

    name: str
    side: Side
    started: float | None
    command: str


@dataclass
class _Scan:
    """What one transcript holds while it is being read."""

    seen: set[str] = field(default_factory=set)
    pending: dict[str, _Call] = field(default_factory=dict)
    turns: int = 0
    weight: float = 0.0
    reads: int = 0
    first_edit: int | None = None
    reads_before: int = 0


def transcripts(
    root: Path, days: int = 30, project: str | None = None
) -> list[Path]:
    """Transcript files changed in the last `days` days.

    Args:
        root: The folder holding one folder per project.
        days: How far back to look, by file modification time.
        project: Keep only projects whose folder name contains this.

    Returns:
        The matching `.jsonl` files, subagent transcripts included, sorted.
    """
    cutoff = time.time() - days * 86400
    found = []
    for path in sorted(root.rglob("*.jsonl")):
        name = path.relative_to(root).parts[0]
        if project and project not in name:
            continue
        if path.stat().st_mtime >= cutoff:
            found.append(path)

    return found


def is_subagent(path: Path) -> bool:
    """True for a subagent's transcript file."""
    return "subagents" in path.parts or path.name.startswith("agent-")


def agent_type(path: Path) -> str | None:
    """The agent type a subagent's meta file records, if there is one."""
    meta = path.with_suffix(".meta.json")
    try:
        data = json.loads(meta.read_text())
    except (OSError, ValueError):
        return None

    kind = data.get("agentType") if isinstance(data, dict) else None
    return str(kind) if kind else None


def records(path: Path) -> Iterator[dict]:
    """The lines of a transcript that carry a message, as objects."""
    with path.open(errors="replace") as handle:
        for line in handle:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get("message"), dict):
                yield rec


def tool_name(name: str) -> str:
    """A tool's short name: Castor's by tool, other MCP tools by server."""
    if name.startswith(CASTOR_PREFIX):
        return "castor:" + name[len(CASTOR_PREFIX) :]
    if name.startswith("mcp__"):
        return "mcp:" + name.split("__")[1]

    return name


def _positionals(words: list[str], value_flags: set[str]) -> list[str]:
    """Words that are neither options nor the values of `value_flags`."""
    kept = []
    skip = False
    for word in words:
        if skip:
            skip = False
        elif word in value_flags:
            skip = True
        elif word.startswith("<<"):
            kept.append("(stdin)")
        elif not word.startswith("-"):
            kept.append(word.rsplit("/", 1)[-1])
    return kept


def command_head(command: str) -> str:
    """The first command of a shell line, without its arguments.

    A leading `cd dir &&` and `VAR=value` are skipped. For git, uv, python
    and the like the next word is kept too ("git diff", "uv run pytest"),
    since the first says nothing alone.
    """
    words = _LEAD_RE.sub("", command.strip()).split()
    if not words:
        return "?"
    first = words[0].rsplit("/", 1)[-1]
    if first not in _VALUE_FLAGS:
        return first[:HEAD_CHARS]

    rest = _positionals(words[1:], _VALUE_FLAGS[first])
    keep = 2 if first == "uv" and rest[:1] == ["run"] else 1
    return " ".join([first, *rest[:keep]])[:HEAD_CHARS]


def is_read(name: str, command: str) -> bool:
    """True for a Read call or a Bash line that prints a file.

    shortcut: a Bash line is judged by its first command, so `sed -i`
    counts and a reader after `;` or `&&` does not; pack_replay's
    bash_reads parses the whole line if exact files are ever needed.
    """
    if name == "Read":
        return True

    return name == "Bash" and command_head(command) in READERS


def is_write(name: str, command: str) -> bool:
    """True for an edit tool call or a Bash line that changes a file.

    shortcut: shell redirects (`> file`) are not counted, since most go to
    /dev/null or scratch and heredoc text looks like them; and a Python
    write to a scratch file counts. Parse the redirect targets if that
    starts to matter.
    """
    if name in EDIT_TOOLS:
        return True

    return name == "Bash" and _BASH_WRITE_RE.search(command) is not None


def result_chars(content: Any) -> int:
    """Characters of text in a tool result (a string or text blocks)."""
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0

    return sum(
        len(part.get("text", ""))
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    )


def weight(tokens: dict[str, int] | Counter) -> float:
    """Token counts added up by their price ratios (see WEIGHTS)."""
    return sum(
        (tokens.get(key) or 0) * ratio for key, ratio in WEIGHTS.items()
    )


def _seconds(stamp: Any) -> float | None:
    """A transcript timestamp as seconds since the epoch, if it parses."""
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _blocks(rec: dict) -> list[dict]:
    """The content blocks of a record's message."""
    content = rec["message"].get("content")
    if not isinstance(content, list):
        return []

    return [block for block in content if isinstance(block, dict)]


def _count_turn(rec: dict, side: Side, scan: _Scan) -> None:
    """Count a model call once, however many lines its message spans."""
    message = rec["message"]
    mid = message.get("id") or rec.get("requestId")
    if mid is not None and mid in scan.seen:
        return
    if mid is not None:
        scan.seen.add(mid)

    usage = message.get("usage") or {}
    side.turns += 1
    scan.turns += 1
    scan.weight += weight(usage)
    for key in WEIGHTS:
        side.tokens[key] += usage.get(key) or 0


def _count_call(block: dict, rec: dict, side: Side, scan: _Scan) -> None:
    """Count a tool call and remember it until its result arrives."""
    name = tool_name(str(block.get("name", "?")))
    side.calls[name] += 1
    args = block.get("input")
    command = str(args.get("command", "")) if isinstance(args, dict) else ""
    if is_write(name, command) and scan.first_edit is None:
        scan.first_edit = scan.turns
        scan.reads_before = scan.reads
    elif is_read(name, command):
        scan.reads += 1

    scan.pending[str(block.get("id"))] = _Call(
        name, side, _seconds(rec.get("timestamp")), command
    )


def _count_result(block: dict, rec: dict, report: Audit, scan: _Scan) -> None:
    """Add a tool result's size and wait to the call it answers."""
    call = scan.pending.pop(str(block.get("tool_use_id")), None)
    if call is None:
        return

    size = result_chars(block.get("content"))
    call.side.chars[call.name] += size
    ended = _seconds(rec.get("timestamp"))
    if call.started is not None and ended is not None:
        call.side.waits[call.name].append(ended - call.started)
    if call.name == "Bash":
        head = command_head(call.command)
        report.bash_calls[head] += 1
        report.bash_chars[head] += size


def _scan_transcript(path: Path, report: Audit) -> None:
    """Add one transcript to the audit."""
    subagent = is_subagent(path)
    home = report.sub if subagent else report.main
    home.transcripts += 1
    scan = _Scan()
    for rec in records(path):
        side = report.sub if rec.get("isSidechain") else home
        if rec.get("type") == "assistant":
            _count_turn(rec, side, scan)
        for block in _blocks(rec):
            if block.get("type") == "tool_use":
                _count_call(block, rec, side, scan)
            elif block.get("type") == "tool_result":
                _count_result(block, rec, report, scan)
    if subagent:
        edits = scan.first_edit is not None
        pool = report.editing if edits else report.reading
        pool.count += 1
        pool.turns += scan.turns
        pool.weight += scan.weight
        report.kinds[agent_type(path) or "(no type)"].append(
            AgentRun(
                scan.turns, scan.weight, scan.first_edit, scan.reads_before
            )
        )


def audit(paths: Iterable[Path]) -> Audit:
    """Read every transcript in `paths` into one audit."""
    report = Audit()
    for path in paths:
        _scan_transcript(path, report)
    return report


def _share(part: float, whole: float) -> str:
    """`part` of `whole` as a percentage, "-" when there is no whole."""
    return f"{100 * part / whole:.1f}%" if whole else "-"


def _weight_lines(report: Audit) -> list[str]:
    """Turns, context per turn and token weight for each side."""
    total = weight(report.main.tokens) + weight(report.sub.tokens)
    lines = [
        "Token weight (see --help; ratios, not dollars)",
        f"  {'':14} {'turns':>9} {'context/turn':>13} {'weight':>10} "
        f"{'share':>7}",
    ]
    for label, side in (
        ("main sessions", report.main),
        ("subagents", report.sub),
    ):
        per_turn = side.tokens["cache_read_input_tokens"] // max(side.turns, 1)
        own = weight(side.tokens)
        lines.append(
            f"  {label:14} {side.turns:>9,} {per_turn:>13,} "
            f"{own / 1e6:>9.1f}M {_share(own, total):>7}"
        )
    kinds = ", ".join(
        f"{label} "
        + _share(
            WEIGHTS[key] * (report.main.tokens[key] + report.sub.tokens[key]),
            total,
        )
        for key, label in WEIGHT_LABELS.items()
    )
    return [*lines, f"  by kind: {kinds}"]


def _subagent_lines(report: Audit) -> list[str]:
    """Subagents that never edit a file against those that do."""
    total = weight(report.main.tokens) + weight(report.sub.tokens)
    lines = ["Subagents"]
    for label, pool in (
        ("never edit a file", report.reading),
        ("edit a file", report.editing),
    ):
        turns = pool.turns // max(pool.count, 1)
        lines.append(
            f"  {label:18} {pool.count:>6,}   avg {turns:>3} turns   "
            f"{_share(pool.weight, total)} of all weight"
        )
    return lines


def _median(values: list[int]) -> str:
    """The median of `values`, rounded, or "-" when there are none."""
    return f"{statistics.median(values):.0f}" if values else "-"


def _kind_lines(report: Audit) -> list[str]:
    """Runs, turns, first edit and token weight for each agent type."""
    total = weight(report.main.tokens) + weight(report.sub.tokens)
    by_weight = sorted(
        report.kinds.items(),
        key=lambda item: sum(run.weight for run in item[1]),
        reverse=True,
    )
    lines = [
        "Subagents by type (medians; first edit and reads before it are"
        " over the runs that edit)",
        f"  {'type':28} {'runs':>6} {'edit':>5} {'turns':>6} "
        f"{'1st edit':>9} {'reads':>6} {'weight':>8} {'share':>7}",
    ]
    for kind, runs in by_weight[:TOP_ROWS]:
        edits = [run for run in runs if run.first_edit is not None]
        own = sum(run.weight for run in runs)
        lines.append(
            f"  {kind[:28]:28} {len(runs):>6,} {len(edits):>5,} "
            f"{_median([run.turns for run in runs]):>6} "
            f"{_median([run.first_edit or 0 for run in edits]):>9} "
            f"{_median([run.reads_before for run in edits]):>6} "
            f"{own / 1e6:>7.1f}M {_share(own, total):>7}"
        )
    return lines


def _tool_lines(report: Audit) -> list[str]:
    """The tools whose results put the most text into context."""
    chars = report.main.chars + report.sub.chars
    calls = report.main.calls + report.sub.calls
    total = sum(chars.values())
    lines = [
        "What tools put into context (result characters)",
        f"  {'tool':28} {'calls':>8} {'chars':>13} {'share':>7} "
        f"{'median wait':>12}",
    ]
    for name, size in chars.most_common(TOP_ROWS):
        waits = report.main.waits[name] + report.sub.waits[name]
        wait = f"{statistics.median(waits):.1f}s" if waits else "-"
        lines.append(
            f"  {name:28} {calls[name]:>8,} {size:>13,} "
            f"{_share(size, total):>7} {wait:>12}"
        )
    return lines


def _bash_lines(report: Audit) -> list[str]:
    """Bash output by the first command of the line."""
    total = sum(report.bash_chars.values())
    lines = ["Bash output by first command"]
    for head, size in report.bash_chars.most_common(TOP_ROWS):
        lines.append(
            f"  {head:28} {report.bash_calls[head]:>8,} {size:>13,} "
            f"{_share(size, total):>7}"
        )
    return lines


def _castor_lines(report: Audit) -> list[str]:
    """How often each Castor tool was called, and how long it took."""
    calls = report.main.calls + report.sub.calls
    castor = Counter(
        {name: n for name, n in calls.items() if name.startswith("castor:")}
    )
    lines = ["Castor"]
    for name, count in castor.most_common():
        waits = report.main.waits[name] + report.sub.waits[name]
        wait = f"{statistics.median(waits):.1f}s" if waits else "-"
        lines.append(f"  {name:36} {count:>6,}   median wait {wait}")
    lines.append(
        f"  all Castor calls: {sum(castor.values()):,}, against "
        f"{calls['Bash']:,} Bash and {calls['Read']:,} Read"
    )
    return lines


def render(report: Audit, days: int) -> str:
    """The audit as a text report."""
    if not report.main.transcripts and not report.sub.transcripts:
        return f"No transcripts changed in the last {days} day(s)."

    header = (
        f"Claude Code usage, last {days} day(s): "
        f"{report.main.transcripts} sessions, "
        f"{report.sub.transcripts} subagent transcripts"
    )
    sections = (
        _weight_lines(report),
        _subagent_lines(report),
        _kind_lines(report),
        _tool_lines(report),
        _bash_lines(report),
        _castor_lines(report),
    )
    return "\n\n".join([header, *("\n".join(lines) for lines in sections)])


def main(argv: list[str] | None = None) -> None:
    """Print the usage report for the chosen transcripts."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument(
        "--project", help="only projects whose folder name contains this"
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args(argv)
    paths = transcripts(args.root, args.days, args.project)
    print(render(audit(paths), args.days))


if __name__ == "__main__":
    main()
