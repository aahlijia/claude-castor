import re
from collections import Counter
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path


from castor.app import (
    mcp,
)
from castor.common import (
    _read_bytes,
    _worktree_roots,
)
from castor.logs import (
    DEFAULT_LOG_FORMAT,
    LogFormat,
    VerdictRow,
    _clean_lines,
    _log_format,
    _verdict_rows,
)


# ---------------------------------------------------------------------------
# Compare runs
#
# Suite logs side by side, row by row: each row (name and occurrence) gets
# a class from its statuses across the runs, and the text shows the
# actionable ones with the trial lines that changed. Deterministic: no
# model reads it.
# ---------------------------------------------------------------------------

COMPARE_CLASS_ORDER = (
    "regressed",
    "flaky",
    "failing",
    "not_assessed",
    "numbers_changed",
    "missing",
    "fixed",
    "same",
)

COMPARE_MAX_TRIAL_LINES = 12

COMPARE_LINE_CHARS = 300

_CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\b")

_DECIMAL_RE = re.compile(r"\d+\.\d+")

# "2 of 3": the counts a numbers_changed row compares. Other integers in
# a detail are measurements that move every run.
_COUNT_RE = re.compile(r"\b(\d+) of (\d+)\b")


@dataclass(frozen=True)
class RowRun:
    """One row in one run: None when the run lacks it."""

    label: str
    row: VerdictRow | None
    trials: tuple[str, ...]


@dataclass(frozen=True)
class RowCompare:
    """One row across every run, with its class."""

    name: str
    occurrence: int
    klass: str
    runs: tuple[RowRun, ...]


def _mask_timings(text: str) -> str:
    """Clock stamps and decimal numbers become `#`: they move every run."""
    return _DECIMAL_RE.sub("#", _CLOCK_RE.sub("#", text))


def _compare_rows(
    runs: list[tuple[str, list[str]]],
    same_code: bool = False,
    fmt: LogFormat = DEFAULT_LOG_FORMAT,
) -> list[RowCompare]:
    """Every row across the runs, sorted by class order, then name.

    Args:
        runs: (label, log lines) per run, oldest first.
        same_code: True when no fix landed between the runs, so any
            PASS and FAIL mix is flaky.
        fmt: The project's log format.

    Returns:
        One RowCompare per (name, occurrence) seen in any run; `same` rows
        come last.
    """
    parsed = []
    keys: dict[tuple[str, int], None] = {}
    for label, lines in runs:
        clean = _clean_lines(lines)
        by_key = {(r.name, r.occurrence): r for r in _verdict_rows(clean, fmt)}
        parsed.append((label, clean, by_key))
        keys.update(dict.fromkeys(by_key))
    out = []
    for key in keys:
        row_runs = tuple(
            _row_run(label, lines, by_key.get(key))
            for label, lines, by_key in parsed
        )
        klass = _compare_class(row_runs, same_code)
        out.append(RowCompare(key[0], key[1], klass, row_runs))
    order = {k: i for i, k in enumerate(COMPARE_CLASS_ORDER)}
    return sorted(out, key=lambda r: (order[r.klass], r.name, r.occurrence))


def _row_run(label: str, lines: list[str], row: VerdictRow | None) -> RowRun:
    """A row in one run, with its trial lines."""
    if row is None:
        return RowRun(label, None, ())
    trials = tuple(lines[row.block[0] - 1 : row.block[1]]) if row.block else ()
    return RowRun(label, row, trials)


def _compare_class(runs: tuple[RowRun, ...], same_code: bool) -> str:
    """The row's class; the first rule that matches wins."""
    present = [r.row for r in runs if r.row is not None]
    verdicts = [r.status for r in present if r.status in ("PASS", "FAIL")]
    switched = _switch_class(verdicts, same_code)
    if switched:
        return switched
    if verdicts and all(v == "FAIL" for v in verdicts):
        return "failing"
    if any(r.status == "NOT ASSESSED" for r in present):
        return "not_assessed"
    numbers = {tuple(_COUNT_RE.findall(r.detail)) for r in present}
    if len(numbers) > 1:
        return "numbers_changed"
    if len(present) < len(runs):
        return "missing"
    return "same"


def _switch_class(verdicts: list[str], same_code: bool) -> str | None:
    """flaky, regressed or fixed from the PASS/FAIL order; else None."""
    if same_code and "PASS" in verdicts and "FAIL" in verdicts:
        return "flaky"
    switches = sum(a != b for a, b in pairwise(verdicts))
    if switches >= 2:
        return "flaky"
    if switches == 1:
        return "regressed" if verdicts[-1] == "FAIL" else "fixed"
    return None


def _compare_text(
    rows: list[RowCompare], max_trial_lines: int = COMPARE_MAX_TRIAL_LINES
) -> str:
    """The listed rows' blocks, then the counts per class."""
    blocks = [
        _compare_block(r, max_trial_lines) for r in rows if r.klass != "same"
    ]
    counts = Counter(r.klass for r in rows)
    summary = ", ".join(
        f"{counts[k]} {k}" for k in COMPARE_CLASS_ORDER if counts[k]
    )
    tail = f"rows: {summary or 'none differ'}"
    return "\n\n".join([*blocks, tail])


def _compare_block(row: RowCompare, max_trial_lines: int) -> str:
    """One row: its class, a line per run, then the trial differences."""
    head = (
        row.name
        if row.occurrence == 0
        else (f"{row.name} (occurrence {row.occurrence})")
    )
    lines = [f"{head} [{row.klass}]"]
    for run in row.runs:
        if run.row is None:
            lines.append(f"  {run.label} (absent)")
        else:
            lines.append(
                f"  {run.label} {run.row.status} {run.row.detail}".rstrip()
            )
    present = [r for r in row.runs if r.row is not None]
    for run in present[1:]:
        lines += _trial_diff(present[0], run, max_trial_lines)
    return "\n".join(line[:COMPARE_LINE_CHARS] for line in lines)


def _trial_diff(ref: RowRun, run: RowRun, limit: int) -> list[str]:
    """A later run's trial lines that differ from the first run's."""
    differ = []
    for i in range(max(len(ref.trials), len(run.trials))):
        a = ref.trials[i] if i < len(ref.trials) else None
        b = run.trials[i] if i < len(run.trials) else None
        if a is not None and b is not None:
            if _mask_timings(a) != _mask_timings(b):
                differ.append(b)
        elif b is not None:
            differ.append(b)
        else:
            differ.append(f"(no line {i + 1}; {ref.label}: {a.strip()})")
    if not differ:
        return [
            f"  {run.label} trial lines match {ref.label} apart from timings"
        ]
    out = [f"  {run.label} trial lines that differ from {ref.label}:"]
    out += [f"    {line}" for line in differ[:limit]]
    if len(differ) > limit:
        out.append(f"    [{len(differ) - limit} more]")
    return out


COMPARE_MAX_LOGS = 12

COMPARE_MAX_LOG_BYTES = 50_000_000

_LOG_STAMP_RE = re.compile(r"\d{8}-\d{6}")


@mcp.tool()
def castor_compare_runs(
    cwd: str,
    logs: list[str],
    rows: list[str] | None = None,
    same_code: bool = False,
) -> str:
    """Compare suite logs row by row: what regressed, what is flaky.

    No model reads anything: Castor parses the verdict rows and classes
    each row (name and occurrence) across the runs.

    Args:
        cwd: Absolute project root. Each log must be inside it or inside a
            worktree of the same git repo.
        logs: Two or more logs, oldest first (absolute or relative to
            `cwd`); each is labelled by its YYYYMMDD-HHMMSS file-name stamp,
            else run1, run2, ...
        rows: Only rows whose name equals or contains one of these.
        same_code: True when no fix landed between the runs, so a row that
            both passed and failed is flaky rather than fixed or regressed.

    Returns:
        One block per row that changed (regressed, flaky, failing,
        not_assessed, numbers_changed, missing, fixed): a line per run,
        then the trial lines that differ from the first run's (timings
        masked). The last line counts rows per class. Or a bracketed error.
    """
    root = Path(cwd).expanduser()
    if not root.is_absolute() or not root.is_dir():
        return f"[Error: cwd must be an absolute directory, not {cwd!r}.]"
    if len(logs) < 2 or len(logs) > COMPARE_MAX_LOGS:
        return f"[Error: give two logs to {COMPARE_MAX_LOGS}, oldest first.]"
    fmt, error = _log_format(Path(root))
    if error:
        return f"[Error: {error}]"
    runs, error = _compare_read_logs(root, logs)
    if error:
        return f"[Error: {error}]"
    found = _compare_rows(runs, same_code, fmt)
    if rows:
        found = [r for r in found if any(w in r.name for w in rows)]
    return _compare_text(found)


def _compare_read_logs(
    root: Path, logs: list[str]
) -> tuple[list[tuple[str, list[str]]], str | None]:
    """(label, lines) per log, or the first log's error."""
    allowed = _worktree_roots(root)
    runs = []
    labels: set[str] = set()
    for i, raw in enumerate(logs):
        path, error = _compare_log_path(root, allowed, raw)
        if path is None:
            return [], error
        data = _read_bytes(path)
        if data is None:
            return [], f"{raw!r} is unreadable."
        label = _log_label(path, i)
        if label in labels:
            label = f"{label}#{i + 1}"
        labels.add(label)
        runs.append(
            (label, data.decode("utf-8", errors="replace").splitlines())
        )
    return runs, None


def _compare_log_path(
    root: Path, allowed: list[Path], raw: str
) -> tuple[Path | None, str | None]:
    """A log as a file inside `allowed`, or the reason it is refused."""
    path = Path(raw).expanduser()
    full = (path if path.is_absolute() else root / path).resolve()
    if not any(full.is_relative_to(a) for a in allowed):
        return None, f"{raw!r} is outside cwd and its repo's worktrees."
    if not full.is_file():
        return None, f"{raw!r} is not a file."
    if full.stat().st_size > COMPARE_MAX_LOG_BYTES:
        return None, f"{raw!r} is over {COMPARE_MAX_LOG_BYTES:,} bytes."
    return full, None


def _log_label(path: Path, index: int) -> str:
    """The file name's YYYYMMDD-HHMMSS stamp, else run<index+1>."""
    stamp = _LOG_STAMP_RE.search(path.name)
    return stamp.group(0) if stamp else f"run{index + 1}"
