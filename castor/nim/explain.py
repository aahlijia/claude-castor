import json
import re
from pathlib import Path
from typing import Any


from castor.app import (
    mcp,
)
from castor.common import (
    _read_bytes,
)
from castor.logs import (
    DEFAULT_LOG_FORMAT,
    LogFormat,
    VERDICT_ROW_CANDIDATES,
    VerdictRow,
    _clean_lines,
    _has_case_stamps,
    _log_format,
    _unstamped_named_rows,
    _verdict_rows,
)
from castor.nim.agent import (
    NIM_AGENT_LINE_CHARS,
    _CITATION_RE,
    _dispatch_nim_agent,
    _run_nim_agent,
    _start_agent_job,
)
from castor.nim.roles import (
    NIM_EXPLAIN_ROW_BLOCK_LINES,
    NIM_EXPLAIN_ROW_MAX_STEPS,
    NIM_EXPLAIN_ROW_PROMPT,
    NIM_EXPLAIN_ROW_STEPS,
    NIM_EXPLAIN_ROW_TRIAL_LINES,
    ROLE_FORMATS,
    ROLE_FORMAT_ERROR,
    _answer_lines,
    _nim_error_json,
    _numbered_lines,
    _role_bad_input,
    _role_log,
    _strip_check_tags,
)


# ---------------------------------------------------------------------------
# Explain a verdict row
#
# Castor finds the row in the log and hands the agent its verdict line and
# trial lines; the agent finds the code that decides the verdict. It is a
# locator: the caller reads the cited code and decides harness or game.
# Its site gate passed on 2026-11-04 (21 NQC cases, 2 runs on Super: 18
# and 19 sites, 0 errors; results-20261104-191044.json).
# ---------------------------------------------------------------------------


@mcp.tool()
def nim_explain_row(
    cwd: str,
    log_path: str,
    row: str,
    occurrence: int = 0,
    format: str = "text",  # noqa: A002 (the tool arg is named format)
    model: str | None = None,
    max_steps: int | None = None,
) -> str:
    """Explain why a FAIL or NOT ASSESSED row failed, with a free NIM agent.

    Castor finds the row in a verdict.sh-style log and hands the agent the
    verdict line, the row's trial lines and the script name (the log's
    folder). The agent finds where the verdict is decided, names the
    `check` precondition and picks the deciding trial lines. It does not
    say whether the harness or the game is wrong: read the cited code and
    decide that yourself. Read-only.

    Args:
        cwd: Absolute project root.
        log_path: The log file, absolute or relative to `cwd`.
        row: The row's exact name, or a piece of it that matches one row
            name (case-sensitive).
        occurrence: Which run of a repeated row name, from 0.
        format: "text" (default) or "json". JSON is Castor's parse of the
            four-line answer plus the citation checks and agent stats.
        model: NIM model id (CASTOR_NIM_AGENT_MODEL, then Nemotron 3.5
            Super).
        max_steps: Request budget (default 6, at most 12).

    Returns:
        The four-line answer (Row, Verdict code, Precondition, Trial
        lines) or a bracketed error. In JSON mode, one JSON object
        (errors are {"error": ..., "message": ...}).
    """
    kwargs, error = _explain_row_job(
        cwd, log_path, row, occurrence, format, model, max_steps
    )
    if error or kwargs is None:
        return error or ROLE_FORMAT_ERROR
    if "label" in kwargs:
        return _dispatch_explain_row_json(**kwargs)
    return _dispatch_nim_agent(**kwargs)


@mcp.tool()
def nim_explain_row_start(
    cwd: str,
    log_path: str,
    row: str,
    occurrence: int = 0,
    format: str = "text",  # noqa: A002 (the tool arg is named format)
    model: str | None = None,
    max_steps: int | None = None,
) -> str:
    """Start nim_explain_row in the background; collect it with castor_poll.

    Args:
        cwd: See nim_explain_row.
        log_path: See nim_explain_row.
        row: See nim_explain_row.
        occurrence: See nim_explain_row.
        format: See nim_explain_row. A JSON job's response is the JSON
            object, including an error object (the job still reads "done").
        model: See nim_explain_row.
        max_steps: See nim_explain_row.

    Returns:
        A message with the job id to pass to castor_poll, or a bracketed
        error.
    """
    kwargs, error = _explain_row_job(
        cwd, log_path, row, occurrence, format, model, max_steps
    )
    if error or kwargs is None:
        return error or ROLE_FORMAT_ERROR
    runner = _dispatch_explain_row_json if "label" in kwargs else None
    return _start_agent_job("NIM explain-row", kwargs, runner)


def _explain_row_job(
    cwd: str,
    log_path: str,
    row: str,
    occurrence: int,
    fmt: str,
    model: str | None,
    max_steps: int | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """The dispatch kwargs for one explain-row run, or the input error.

    JSON runs also carry `label` (the log relative to the root, for the
    trial lines) and the found row's `row_name` and `row_status`, which
    the JSON takes in place of the model's Row line.
    """
    if fmt not in ROLE_FORMATS:
        return None, ROLE_FORMAT_ERROR
    task, root, error, facts = _explain_row_build(
        cwd, log_path, row, occurrence
    )
    if error or task is None:
        return None, _role_bad_input(error, fmt)
    steps = min(
        max(1, max_steps or NIM_EXPLAIN_ROW_STEPS), NIM_EXPLAIN_ROW_MAX_STEPS
    )
    kwargs: dict[str, Any] = {
        "task": task,
        "cwd": root,
        "model": model,
        "max_steps": steps,
    }
    if fmt == "json":
        kwargs.update(facts)
    return kwargs, None


def _explain_row_task(
    cwd: str, log_path: str, row: str, occurrence: int
) -> tuple[str | None, str, str | None]:
    """The explain-row role's task: (task, resolved root, error).

    Args:
        cwd: The project root.
        log_path: The log, absolute or relative to `cwd`.
        row: The row's exact name or a piece of one name.
        occurrence: Which run of the name, from 0.

    Returns:
        The task text and resolved root, or (None, root, bracketed error).
    """
    task, root, error, _ = _explain_row_build(cwd, log_path, row, occurrence)
    return task, root, error


def _explain_row_build(
    cwd: str, log_path: str, row: str, occurrence: int
) -> tuple[str | None, str, str | None, dict[str, str]]:
    """_explain_row_task plus the facts the JSON runner needs.

    The facts are `label` (the log relative to the root), `row_name` and
    `row_status` of the row Castor found; empty on an error.
    """
    target, root, error = _role_log(cwd, log_path)
    if error or target is None:
        return None, root, error, {}
    data = _read_bytes(target)
    if data is None:
        return None, root, f"[Error: {log_path!r} is unreadable.]", {}
    lines = _clean_lines(data.decode("utf-8", errors="replace").splitlines())
    fmt, error = _log_format(Path(root))
    if error:
        return None, str(root), f"[Error: {error}]", {}
    unstamped = None if _has_case_stamps(lines, fmt) else lines
    found, error = _explain_row_find(
        _verdict_rows(lines, fmt), row, occurrence, unstamped, fmt
    )
    if error or found is None:
        return None, root, error, {}
    label = str(target.relative_to(root))
    task = _explain_row_prompt(found, lines, label, target.parent.name)
    facts = {
        "label": label,
        "row_name": found.name,
        "row_status": found.status,
    }
    return task, root, None, facts


def _explain_row_find(
    rows: list[VerdictRow],
    row: str,
    occurrence: int,
    lines: list[str] | None = None,
    fmt: LogFormat = DEFAULT_LOG_FORMAT,
) -> tuple[VerdictRow | None, str | None]:
    """The row to explain: exact name, else the one name holding `row`.

    `lines` is the log when it has no stamps: a `row` that matches no
    parsed row is then looked up by exact name on the verdict lines,
    which finds names too long for verdict.sh's name column.
    """
    names = list(dict.fromkeys(r.name for r in rows))
    matches = [row] if row in names else [n for n in names if row in n]
    if len(matches) > 1:
        return None, _explain_row_lookup_error(row, matches, rows)
    same: list[VerdictRow] = []
    if matches:
        same = [r for r in rows if r.name == matches[0]]
    elif lines and row and fmt.unstamped:
        same, error = _unstamped_named_rows(lines, row, fmt)
        if error:
            return None, error
    if not same and not rows:
        return None, (
            "[Error: the log has no verdict rows (no @case stamps or pytest "
            "FAILED lines).]"
        )
    if not same:
        return None, _explain_row_lookup_error(row, [], rows)
    return _explain_row_pick(same, occurrence)


def _explain_row_pick(
    same: list[VerdictRow], occurrence: int
) -> tuple[VerdictRow | None, str | None]:
    """Run `occurrence` of one row name; a PASS run is refused."""
    if not 0 <= occurrence < len(same):
        return None, (
            f"[Error: row {same[0].name!r} occurs {len(same)} time(s); "
            f"occurrence must be 0 to {len(same) - 1}, not {occurrence}.]"
        )
    found = same[occurrence]
    if found.status == "PASS":
        return None, f"[Error: row {found.name!r} passed; nothing to explain.]"
    return found, None


def _explain_row_lookup_error(
    row: str, matches: list[str], rows: list[VerdictRow]
) -> str:
    """The error for zero or several matching rows, naming candidates."""
    if matches:
        head = f"{row!r} matches {len(matches)} rows"
        names, ask = matches, " Give the exact name."
    else:
        head = f"no row matches {row!r}"
        failed = [r.name for r in rows if r.status != "PASS"]
        names = list(dict.fromkeys(failed or [r.name for r in rows]))
        ask = ""
    shown = ", ".join(repr(n) for n in names[:VERDICT_ROW_CANDIDATES])
    more = len(names) - VERDICT_ROW_CANDIDATES
    tail = f" ({more} more)" if more > 0 else ""
    return f"[Error: {head}. Rows: {shown}{tail}.{ask}]"


def _explain_row_prompt(
    row: VerdictRow,
    lines: list[str],
    label: str,
    script: str,
) -> str:
    """Fill NIM_EXPLAIN_ROW_PROMPT for one row of the log."""
    reason = ""
    if row.status == "NOT ASSESSED" and row.detail:
        reason = f"NOT ASSESSED reason: {row.detail}\n"
    return NIM_EXPLAIN_ROW_PROMPT.format(
        log=label,
        name=row.name,
        status=row.status,
        line=row.line,
        verdict_line=lines[row.line - 1][:NIM_AGENT_LINE_CHARS],
        script=script,
        reason=reason,
        block=_explain_row_block(lines, row.block),
    )


def _explain_row_block(lines: list[str], block: tuple[int, int] | None) -> str:
    """A row's trial lines, numbered; a long block keeps both ends."""
    if block is None:
        return "(none: the row printed no lines before its verdict)"
    first, last = block
    count = last - first + 1
    if count <= NIM_EXPLAIN_ROW_BLOCK_LINES:
        return _numbered_lines(lines, first, last)
    half = NIM_EXPLAIN_ROW_BLOCK_LINES // 2
    return "\n".join(
        (
            _numbered_lines(lines, first, first + half - 1),
            f"[{count - 2 * half} lines cut]",
            _numbered_lines(lines, last - half + 1, last),
        )
    )


# The four answer lines, as "Field: value"; markdown bullets, a list
# number ("1." or "2)") and bold around the field name are tolerated.
_EXPLAIN_FIELD_RE = re.compile(
    r"^[\s*>-]*(?:\d+[.)]\s*)?[\s*]*"
    r"(row|verdict code|precondition|trial lines)"
    r"\s*\**\s*:\s*\**\s*(.*?)\s*$",
    re.IGNORECASE,
)

_EXPLAIN_ROW_NAME_RE = re.compile(r"^(.*?)\s*\((FAIL|NOT ASSESSED|PASS)\)$")


def _explain_row_json(answer: str, stats: dict, label: str) -> dict:
    """Castor's JSON reading of an explain-row answer.

    Args:
        answer: The agent's answer, tags and footer included.
        stats: The run's stats (model, steps, seconds).
        label: The log's path relative to the project root.

    Returns:
        row, verdict, verdict_code, precondition, trial_lines, verified,
        quote_ok and agent. Fields the answer lacks are None (trial_lines
        is then []).
    """
    fields, verified, quote_ok = _explain_row_fields(_answer_lines(answer))
    name, verdict = _explain_row_name(fields.get("row"))
    return {
        "row": name,
        "verdict": verdict,
        "verdict_code": _explain_verdict_code(fields.get("verdict code")),
        "precondition": fields.get("precondition") or None,
        "trial_lines": _explain_trial_lines(
            fields.get("trial lines", ""), label
        ),
        "verified": verified,
        "quote_ok": quote_ok,
        "agent": {k: stats.get(k) for k in ("model", "steps", "seconds")},
    }


def _explain_verdict_code(text: str | None) -> str | None:
    """The Verdict code line's path:line; None for "not found" or none."""
    if not text or text.lower().startswith("not found"):
        return None
    code = _CITATION_RE.search(text)
    return code.group(0) if code else None


def _explain_row_fields(
    lines: list[str],
) -> tuple[dict[str, str], bool, bool | None]:
    """The answer's fields (first of each wins), verified, quote_ok."""
    fields: dict[str, str] = {}
    verified, quote_ok = True, None
    for line in lines:
        clean, ok, quote = _strip_check_tags(line)
        match = _EXPLAIN_FIELD_RE.match(clean)
        if not match or match.group(1).lower() in fields:
            continue
        fields[match.group(1).lower()] = match.group(2)
        verified = verified and ok
        if quote is False:
            quote_ok = False
    return fields, verified, quote_ok


def _explain_row_name(text: str | None) -> tuple[str | None, str | None]:
    """(name, status) from "Row: <name> (FAIL)"; status None if absent."""
    if not text:
        return None, None
    match = _EXPLAIN_ROW_NAME_RE.match(text)
    if not match:
        return text, None
    return match.group(1), match.group(2)


def _explain_trial_lines(text: str, label: str) -> list[int]:
    """The log line numbers cited as `<log>:LINE`, deduped, at most 6."""
    name = re.escape(Path(label).name)
    cited = re.findall(rf"(?<![\w.-])(?:[\w./-]*/)?{name}:(\d+)", text)
    numbers = list(dict.fromkeys(int(n) for n in cited))
    return numbers[:NIM_EXPLAIN_ROW_TRIAL_LINES]


def _dispatch_explain_row_json(
    task: str,
    cwd: str,
    model: str | None,
    max_steps: int | None,
    label: str,
    row_name: str,
    row_status: str,
) -> str:
    """Run the explain-row agent; its answer as a JSON object string.

    `row` and `verdict` are the row Castor found, not the model's Row
    line, so a misnamed row in the answer cannot change them.
    """
    text, stats = _run_nim_agent(task, cwd, model, max_steps)
    if text.startswith("["):
        return json.dumps(_nim_error_json(text), ensure_ascii=False)
    obj = _explain_row_json(text, stats, label)
    obj["row"], obj["verdict"] = row_name, row_status
    return json.dumps(obj, ensure_ascii=False)
