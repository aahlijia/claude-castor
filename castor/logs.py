import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


from castor.common import (
    RUN_CONFIG_FILE,
    _run_regex,
)


# ---------------------------------------------------------------------------
# Verdict rows
#
# A deterministic reader for suite logs. The default format (first used by
# NQC's verdict.sh) prints each row as `%-13s %-34s %s` (STATUS NAME DETAIL)
# followed by a stamp line
# `@case <epoch> <STATUS_> <NAME>`; the stamp is the reliable split because
# a long name overflows its column. Plain pytest output is the fallback.
# ---------------------------------------------------------------------------

_PYTEST_ROW_RE = re.compile(r"^(FAILED|ERROR) (\S+)(?: - (.*))?$")

_PYTEST_TOTALS_RE = re.compile(r"\d+ (?:failed|passed|errors?)\b.* in [\d.]+s")

_PYTEST_COUNT_RE = re.compile(r"(\d+) (failed|passed|errors?)\b")

_VERDICT_STATUSES = ("PASS", "FAIL", "NOT ASSESSED")


@dataclass(frozen=True)
class LogFormat:
    """How a project's suite log spells its verdicts.

    The default is the layout of NQC's verdict.sh. A project picks another
    with the `log_format` key of `.castor/run.json`; pytest `FAILED` lines
    and the pytest result line are always read as a fallback.

    Attributes:
        case_re: Matches a stamp line; group 1 is the status with `_` for
            spaces, group 2 the test name. None when the suite has none.
        totals_re: Matches the totals line; groups are total, fail and not
            assessed. None when the suite has none.
        unstamped: Whether to read fixed-column verdict lines when a log
            has no stamps.
        name_start: Column where an unstamped line's name starts.
        name_end: Column of the space after the name field.
        reprint: The line that starts the summary reprint, or None.
        noise: Patterns whose matches `castor_compare_runs` ignores when it
            diffs trial lines (counters that move every run).
    """

    case_re: re.Pattern | None
    totals_re: re.Pattern | None
    unstamped: bool
    name_start: int
    name_end: int
    reprint: str | None
    noise: tuple[re.Pattern, ...] = ()


DEFAULT_LOG_FORMAT = LogFormat(
    case_re=re.compile(r"^@case \d+ (PASS|FAIL|NOT_ASSESSED) (.+)$"),
    totals_re=re.compile(r"^total (\d+), fail (\d+), not assessed (\d+)$"),
    unstamped=True,
    name_start=14,
    name_end=48,
    reprint="--- verdicts ---",
)

PYTEST_LOG_FORMAT = LogFormat(
    case_re=None,
    totals_re=None,
    unstamped=False,
    name_start=14,
    name_end=48,
    reprint=None,
)

LOG_FORMAT_PROFILES = {
    "default": DEFAULT_LOG_FORMAT,
    "pytest": PYTEST_LOG_FORMAT,
}


@dataclass(frozen=True)
class VerdictRow:
    """One test verdict found in a suite log.

    Attributes:
        name: The test name.
        status: "PASS", "FAIL" or "NOT ASSESSED".
        detail: The text after the name on the verdict line.
        line: 1-based line number of the verdict line.
        block: 1-based inclusive range of the lines printed for this test
            before its verdict, or None when there are none.
        occurrence: 0-based count of earlier rows with the same name.
    """

    name: str
    status: str
    detail: str
    line: int
    block: tuple[int, int] | None
    occurrence: int


def _clean_lines(lines: list[str]) -> list[str]:
    """Strips carriage returns so CRLF logs match like LF ones."""
    return [ln.replace("\r", "") for ln in lines]


def _verdict_line_index(lines: list[str], stamp: int) -> int | None:
    """Finds the nearest non-empty line above a stamp line."""
    for i in range(stamp - 1, -1, -1):
        if lines[i].strip():
            return i
    return None


def _stamped_row(
    lines: list[str],
    stamp: int,
    prev_stamp: int,
    counts: dict[str, int],
    case_re: re.Pattern,
) -> VerdictRow | None:
    """Builds the row for one stamp line, or None if its verdict is absent."""
    match = case_re.match(lines[stamp])
    if not match:
        return None
    status = match.group(1).replace("_", " ")
    name = match.group(2)
    idx = _verdict_line_index(lines, stamp)
    if idx is None or idx <= prev_stamp:
        return None
    text = lines[idx]
    if not text.startswith(status):
        return None
    at = text.find(name, len(status))
    if at < 0:
        return None
    detail = text[at + len(name) :].strip()
    first = prev_stamp + 2
    block = (first, idx) if first <= idx else None
    occurrence = counts.get(name, 0)
    counts[name] = occurrence + 1
    return VerdictRow(name, status, detail, idx + 1, block, occurrence)


def _stamped_rows(lines: list[str], case_re: re.Pattern) -> list[VerdictRow]:
    """Reads every stamped verdict row, in log order."""
    rows: list[VerdictRow] = []
    counts: dict[str, int] = {}
    prev_stamp = -1
    for i, text in enumerate(lines):
        if not case_re.match(text):
            continue
        row = _stamped_row(lines, i, prev_stamp, counts, case_re)
        if row:
            rows.append(row)
        prev_stamp = i
    return rows


def _pytest_rows(lines: list[str]) -> list[VerdictRow]:
    """Reads pytest `FAILED`/`ERROR` summary lines as FAIL rows."""
    rows: list[VerdictRow] = []
    counts: dict[str, int] = {}
    for i, text in enumerate(lines):
        match = _PYTEST_ROW_RE.match(text)
        if not match:
            continue
        name = match.group(2)
        occurrence = counts.get(name, 0)
        counts[name] = occurrence + 1
        detail = (match.group(3) or "").strip()
        rows.append(VerdictRow(name, "FAIL", detail, i + 1, None, occurrence))
    return rows


def _verdict_rows(
    lines: list[str], fmt: LogFormat = DEFAULT_LOG_FORMAT
) -> list[VerdictRow]:
    """Parses verdict rows from log lines.

    Stamped rows win when the log has any stamp. A log without stamps
    (verdict.sh before 2026-11-03) is read from its verdict lines. The
    pytest fallback runs only when neither found a row.

    Args:
        lines: The log, one string per line.
        fmt: The project's log format.

    Returns:
        The rows in log order, empty when the log has none.
    """
    cleaned = _clean_lines(lines)
    if fmt.case_re is not None and _has_case_stamps(cleaned, fmt):
        return _stamped_rows(cleaned, fmt.case_re) or _pytest_rows(cleaned)
    if fmt.unstamped:
        return _unstamped_rows(cleaned, fmt) or _pytest_rows(cleaned)
    return _pytest_rows(cleaned)


def _has_case_stamps(
    lines: list[str], fmt: LogFormat = DEFAULT_LOG_FORMAT
) -> bool:
    """True when any line is a stamp line."""
    case_re = fmt.case_re
    return case_re is not None and any(case_re.match(t) for t in lines)


def _is_case_stamp(text: str, fmt: LogFormat = DEFAULT_LOG_FORMAT) -> bool:
    """True when `text` is a stamp line."""
    return fmt.case_re is not None and bool(fmt.case_re.match(text))


def _is_totals_line(text: str, fmt: LogFormat = DEFAULT_LOG_FORMAT) -> bool:
    """True when `text` is the suite totals line."""
    return fmt.totals_re is not None and bool(fmt.totals_re.match(text))


def _unstamped_status(text: str, fmt: LogFormat) -> str | None:
    """A verdict line's status (padded to the name column), else None."""
    head = text[: fmt.name_start]
    for status in _VERDICT_STATUSES:
        if head == status.ljust(fmt.name_start - 1) + " ":
            return status
    return None


def _unstamped_verdict_lines(
    lines: list[str], fmt: LogFormat
) -> list[tuple[int, str | None]]:
    """(index, status) of each verdict line outside the summary reprint.

    The reprint starts at an unindented `--- verdicts ---` and runs to the
    totals line, or up to the first line that is neither blank nor a
    verdict line. Its last line is listed with status None, as a block
    boundary.
    """
    found: list[tuple[int, str | None]] = []
    in_reprint = False
    for i, text in enumerate(lines):
        if in_reprint:
            if _is_totals_line(text, fmt):
                found.append((i, None))
                in_reprint = False
                continue
            if not text.strip() or _unstamped_status(text, fmt):
                continue
            found.append((i - 1, None))
            in_reprint = False
        if fmt.reprint is not None and text.rstrip() == fmt.reprint:
            in_reprint = True
            continue
        status = _unstamped_status(text, fmt)
        if status:
            found.append((i, status))
    return found


def _unstamped_blocks(
    lines: list[str], fmt: LogFormat
) -> list[tuple[int, str, tuple[int, int] | None]]:
    """(index, status, block) of each verdict line outside the reprint.

    A block runs from the line after the previous verdict line (or line 1)
    to the line before this one; every verdict line is a boundary, even
    one whose name cannot be read, and so is the end of a reprint.
    """
    out = []
    prev = -1
    for i, status in _unstamped_verdict_lines(lines, fmt):
        first = prev + 2
        if status is not None:
            out.append((i, status, (first, i) if first <= i else None))
        prev = i
    return out


def _unstamped_rows(lines: list[str], fmt: LogFormat) -> list[VerdictRow]:
    """Reads verdict lines without stamps; overflowing names are skipped.

    The name field runs from `fmt.name_start` to `fmt.name_end` (14 to 48
    by default). A name longer than the field runs into the detail, so its
    line is left out (an overflowing name with a space at the end column is
    misread; accepted).
    """
    rows: list[VerdictRow] = []
    counts: dict[str, int] = {}
    end = fmt.name_end
    for i, status, block in _unstamped_blocks(lines, fmt):
        text = lines[i]
        if len(text) > end and text[end] != " ":
            continue
        name = text[fmt.name_start : end].rstrip()
        if not name:
            continue
        detail = text[end + 1 :].strip()
        occurrence = counts.get(name, 0)
        counts[name] = occurrence + 1
        rows.append(VerdictRow(name, status, detail, i + 1, block, occurrence))
    return rows


def _unstamped_named_rows(
    lines: list[str], name: str, fmt: LogFormat = DEFAULT_LOG_FORMAT
) -> tuple[list[VerdictRow], str | None]:
    """Rows for verdict lines whose name field starts with exactly `name`.

    For names too long for the 34-column field: the caller supplies the
    full name, and the line must continue with a space or end there. When
    the matched lines continue with different words, `name` is only part
    of their names, so the result is an error listing what follows it.
    """
    rows: list[VerdictRow] = []
    tails: dict[str, list[str]] = {}
    for i, status, block in _unstamped_blocks(lines, fmt):
        rest = lines[i][fmt.name_start :]
        after = rest[len(name) :]
        if not rest.startswith(name) or after[:1] not in ("", " "):
            continue
        words = after.split()
        tails.setdefault(words[0] if words else "", words[:3])
        detail = after.strip()
        rows.append(VerdictRow(name, status, detail, i + 1, block, len(rows)))
    if len(tails) > 1:
        return [], _unstamped_prefix_error(name, list(tails.values()))
    return rows, None


def _unstamped_prefix_error(name: str, tails: list[list[str]]) -> str:
    """The error for a `name` that begins rows continuing differently."""
    shown = [
        repr(" ".join([name, *words]))
        for words in tails[:VERDICT_ROW_CANDIDATES]
    ]
    more = len(tails) - VERDICT_ROW_CANDIDATES
    tail = f" ({more} more)" if more > 0 else ""
    return (
        f"[Error: {name!r} begins {len(tails)} different rows: "
        f"{', '.join(shown)}{tail}. Pass more of the row name.]"
    )


def _last_suite_totals(
    lines: list[str], fmt: LogFormat
) -> dict[str, int] | None:
    """Returns the last totals line (total, fail, not assessed), if any."""
    if fmt.totals_re is None:
        return None
    found = None
    for text in lines:
        match = fmt.totals_re.match(text)
        if match:
            total, fail, na = (int(g) for g in match.groups())
            found = {"total": total, "fail": fail, "not_assessed": na}
    return found


def _last_pytest_totals(lines: list[str]) -> dict[str, int] | None:
    """Returns totals from the last pytest result line, if any."""
    found = None
    for text in lines:
        if not _PYTEST_TOTALS_RE.search(text):
            continue
        counts = {"failed": 0, "passed": 0, "error": 0}
        for num, word in _PYTEST_COUNT_RE.findall(text):
            counts[word.rstrip("s")] += int(num)
        fail = counts["failed"] + counts["error"]
        found = {
            "total": fail + counts["passed"],
            "fail": fail,
            "not_assessed": 0,
        }
    return found


def _verdict_totals(
    lines: list[str], fmt: LogFormat = DEFAULT_LOG_FORMAT
) -> dict[str, int] | None:
    """Reads the suite totals line.

    Args:
        lines: The log, one string per line.
        fmt: The project's log format.

    Returns:
        A dict with `total`, `fail` and `not_assessed`, or None when the
        log has no totals line. The last match wins.
    """
    cleaned = _clean_lines(lines)
    return _last_suite_totals(cleaned, fmt) or _last_pytest_totals(cleaned)


def _row_for_line(rows: list[VerdictRow], n: int) -> VerdictRow | None:
    """Finds the row whose block or verdict line holds line `n`.

    Args:
        rows: Rows from `_verdict_rows`.
        n: A 1-based log line number.

    Returns:
        The matching row, or None.
    """
    for row in rows:
        if row.line == n:
            return row
        if row.block and row.block[0] <= n <= row.block[1]:
            return row
    return None


VERDICT_ROW_CANDIDATES = 10  # row names listed in a row lookup error


LOG_FORMAT_KEYS = frozenset(
    {"profile", "case", "totals", "name_start", "name_end", "reprint", "noise"}
)


def _log_format_from(value: Any) -> LogFormat:
    """Build a LogFormat from run.json's `log_format`; ValueError names it.

    `value` is None (the default), a profile name, or an object with an
    optional `profile` (the base, default "default") and overrides: `case`
    and `totals` (regex strings, or null for none; `case` needs two groups
    and `totals` three), `name_start` and `name_end` (columns) and
    `reprint` (a line, or null) and `noise` (a list of regexes).
    """
    if value is None:
        return DEFAULT_LOG_FORMAT
    if isinstance(value, str):
        value = {"profile": value}
    if not isinstance(value, dict):
        raise ValueError("log_format must be a profile name or an object.")
    extra = sorted(set(value) - LOG_FORMAT_KEYS)
    if extra:
        raise ValueError(f"log_format has unknown key(s): {extra}")
    name = value.get("profile", "default")
    base = LOG_FORMAT_PROFILES.get(name) if isinstance(name, str) else None
    if base is None:
        known = sorted(LOG_FORMAT_PROFILES)
        raise ValueError(f"log_format.profile must be one of {known}.")
    return LogFormat(
        case_re=_log_format_regex(value, "case", base.case_re, 2),
        totals_re=_log_format_regex(value, "totals", base.totals_re, 3),
        unstamped=base.unstamped,
        name_start=_log_format_column(value, "name_start", base.name_start),
        name_end=_log_format_column(value, "name_end", base.name_end),
        reprint=_log_format_reprint(value, base.reprint),
        noise=_log_format_noise(value, base.noise),
    )


def _log_format_regex(
    value: dict, key: str, default: re.Pattern | None, groups: int
) -> re.Pattern | None:
    """A log_format regex with exactly `groups` groups, else ValueError."""
    if key not in value:
        return default
    if value[key] is None:
        return None
    pattern = _run_regex(value[key], f"log_format.{key}")
    if pattern.groups != groups:
        raise ValueError(f"log_format.{key} needs exactly {groups} group(s).")
    return pattern


def _log_format_column(value: dict, key: str, default: int) -> int:
    """A log_format column number (an integer of at least 1)."""
    raw = value.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ValueError(f"log_format.{key} must be an integer of 1 or more.")
    return raw


def _log_format_reprint(value: dict, default: str | None) -> str | None:
    """The log_format reprint line: a non-empty string or null."""
    raw = value.get("reprint", default)
    if raw is not None and (not isinstance(raw, str) or not raw):
        raise ValueError("log_format.reprint must be a string or null.")
    return raw


def _log_format_noise(
    value: dict, default: tuple[re.Pattern, ...]
) -> tuple[re.Pattern, ...]:
    """The log_format noise patterns: regexes that never match empty."""
    if "noise" not in value:
        return default
    raw = value["noise"]
    if not isinstance(raw, list) or not all(
        isinstance(p, str) and p for p in raw
    ):
        raise ValueError(
            "log_format.noise must be a list of non-empty regex strings."
        )
    patterns = tuple(_run_regex(p, "log_format.noise") for p in raw)
    if any(p.match("") for p in patterns):
        raise ValueError("log_format.noise patterns must not match empty.")
    return patterns


def _log_format(root: Path) -> tuple[LogFormat, str | None]:
    """The project's log format from `root/.castor/run.json`.

    Reads only `log_format`, so it works without the run tier (no `allow`,
    no CASTOR_RUN_ROOTS).

    Args:
        root: The project root.

    Returns:
        (format, None), or (the default format, a sentence naming the
        problem) when the file or its `log_format` is bad. A missing file
        is the default format with no error.
    """
    path = root / RUN_CONFIG_FILE
    if not path.is_file():
        return DEFAULT_LOG_FORMAT, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return (
            DEFAULT_LOG_FORMAT,
            f"{RUN_CONFIG_FILE} is not valid JSON: {exc}",
        )
    if not isinstance(data, dict):
        return (
            DEFAULT_LOG_FORMAT,
            f"{RUN_CONFIG_FILE} must hold a JSON object.",
        )
    try:
        return _log_format_from(data.get("log_format")), None
    except ValueError as exc:
        return DEFAULT_LOG_FORMAT, f"{RUN_CONFIG_FILE}: {exc}"
