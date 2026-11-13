"""Score Castor's NIM roles and plain tools against cases with known answers.

Each case names a role (review, scout or triage for the agent; explain or
summarize for plain calls; edit for nim_edit; explain_row for the verdict
row explainer), where its code comes from,
and the findings, ranges, log lines or key points a good answer holds. The
script runs every case under every model configuration. Agent answers are
scored by their path:line citations (lead precision, recall, must-catch
hits); triage answers also by the causes Castor's own check keeps and
whether one sits on a known cause line; plain answers by keywords alone
(recall of known causes or key points); edit patches by applying them and
comparing the resulting files with those of the reference diff.

    uv run --with httpx --with fastmcp python scripts/nim_eval.py \\
        scripts/eval/cases.json \\
        --config ultra=nvidia/nemotron-3-ultra-550b-a55b

NVIDIA_API_KEY or NVIDIA_API_KEYS must be set. Code is taken read-only
with `git archive` into ~/.cache/claude-castor/eval/snapshots/, so a case
can point at another repo without touching its working tree. Keep cases
for private repos outside this repo (for example under that cache dir).
Results are written as JSON beside the snapshots. Edit rows keep the raw
model answers nim_edit parsed (first answer, then any repair answer) in
`raw_answers`, so a refusal can be diagnosed from the file. A run whose
edits were recorded with propose_edit parses no answer: its row has
`edit_tool` true and the refusals propose_edit gave in `edit_refusals`.

Case fields (JSON list of objects):
    name, role                 "review" | "scout" | "triage" | "explain"
                               | "summarize" | "edit" | "explain_row"
    repo, commit, paths        snapshot source (paths limit the archive)
    apply                      optional diff applied to the snapshot
    dir                        or a fixture directory instead of a repo
    diff_file, focus           review inputs
    question                   scout input (and optional for triage)
    log                        triage input, relative to the case's code
    error_file, files          explain inputs: a file holding the error
                               text, and source files to inline
    log_file, row, occurrence  explain_row inputs: a log kept outside the
                               snapshot (relative to the cases file),
                               copied to `log` in the snapshot; the row
                               name; which run of a repeated name
    target                     summarize input, relative to the case's code
    task, files, must_cover    edit inputs: the edit as briefed, the scope
                               fence, symbols whose uses must be edited
    reference_diff             edit: the known-good diff, applied to a
                               second copy to get the expected files
    check                      edit: optional shell command run in the
                               patched copy (300 s); pass or fail recorded
    expect                     edit: "patch" (default) or "refuse"
    expected                   [{id, locations: [{file, lines: [a, b]}],
                                 keywords, window, must}]; plain roles use
                                 keywords only
    expected[].cause_lines     triage: [[a, b], ...], the log lines that
                               show why the item failed. Castor's check of
                               the answer's causes is scored against them
Relative paths are resolved against the cases file. The response cache is
off during a run, so repeated runs are independent.
"""

import argparse
import contextlib
import hashlib
import importlib.util
import json
import math
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CITE_RE = re.compile(r"([\w./-]+\.\w+):(\d+)(?:-(\d+))?")
# "line 181" or "lines 129-130": how a triage answer cites its one log.
BARE_LINE_RE = re.compile(r"\blines?\s+(\d+)(?:\s*-\s*(\d+))?", re.IGNORECASE)
# A lead opens with its citation after list or emphasis markers; a line
# that only cites evidence in passing is part of the lead above it.
LEAD_PREFIX_RE = re.compile(r"^[\s>#*_`\-\d.)]*")
FOOTER_RE = re.compile(r"\[nim_agent: (\d+) steps")
QUOTES_RE = re.compile(r"quotes matched (\d+)/(\d+)")
DEFAULT_WINDOW = 10
# Stamped on every result row. Bump it whenever score() or score_plain()
# changes, so a saved file scored the old way is flagged and re-scored.
# 1: keyword-gated location matching. 2: plain roles scored by keyword.
# 3: edit role scored by patch against reference diff. 4: an edit run
# without a patch counts as refused only on a Castor refusal (see
# edit_refused); any other failure is an error row. A must_cover case
# under dekko=off is "needs dekko", left out of the correct K/N count.
# 5: a failed repair run or a stalled answer is an error row as well.
# 6: explain_row role scored by Verdict code site and Reading word.
# 7: a triage lead cites its log anywhere in the line (as path:line or a
# bare "line N"), and a triage row carries the causes Castor kept and the
# known cause lines they reach.
SCORING_VERSION = 7
ROLE_STEPS = {
    "review": "NIM_REVIEW_STEPS",
    "scout": "NIM_SCOUT_STEPS",
    "triage": "NIM_TRIAGE_STEPS",
    "edit": "NIM_EDIT_STEPS",
}
# Plain tool calls: no agent loop, scored by keywords over the whole answer.
PLAIN_ROLES = ("explain", "summarize")
# What counts as nim_edit refusing a case: a "[nim_edit: REFUSED" answer,
# or one of its own input checks (server._edit_input_error and
# _edit_args_error). Anything else without a patch (a timeout, a 429, a
# stall, a transport error, an empty answer, a missing key, a dekko map
# failure) is an error row, so a refuse case cannot pass on a failed run.
EDIT_REFUSED = "[nim_edit: REFUSED"
EDIT_INPUT_REFUSALS = (
    "[Error: task is empty.]",
    "[Error: files is empty;",
    "[Error: invalid files entry:",
    "[Error: invalid must_cover symbol:",
)
EDIT_CWD_REFUSAL = ("[Error: cwd ", " is not a directory.]")
# A refusal that hides a failed run is an error row too: the repair round's
# NIM request failed (server._edit_repair adds this line), or an answer was
# cut off by a stall (the note server._nim_cut_short appends), which can
# truncate a block into a refusal.
REPAIR_FAILED_LINE = "- repair run failed:"
CUT_OFF_NOTE = "[Note: response cut off "
# Score keys a row loses when it turns out to be an error.
EDIT_SCORE_KEYS = (
    "correct",
    "clean_error",
    "wrong_applies",
    "out_of_scope",
    "repair",
    "check_passed",
)


def load_server(path: Path) -> ModuleType:
    """Import a server (the current checkout, or an older copy to compare).

    A checkout with a `castor` package comes back as one namespace over its
    modules (`castor._namespace.view`); an older single-file server.py is
    imported as a module.
    """
    if (path.parent / "castor" / "app.py").is_file():
        sys.path.insert(0, str(path.parent))
        from castor._namespace import view

        return view()  # type: ignore[return-value]
    spec = importlib.util.spec_from_file_location("castor_server", path)
    if spec is None or spec.loader is None:
        sys.exit(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(module)
    return module


def cache_root(server: ModuleType) -> Path:
    """Where snapshots and results live."""
    return server._cache_dir() / "eval"


def snapshot(case: dict, server: ModuleType) -> Path:
    """The code a case runs against, extracted once and reused."""
    base = Path(case["_base"])
    if "dir" in case:
        return (base / case["dir"]).resolve()
    repo = (base / Path(case["repo"]).expanduser()).resolve()
    apply = (base / case["apply"]).resolve() if case.get("apply") else None
    key = hashlib.sha1(
        f"{repo}|{case['commit']}|{case.get('paths')}|{apply}".encode()
    ).hexdigest()[:12]
    target = cache_root(server) / "snapshots" / f"{case['commit']}-{key}"
    if target.is_dir():
        return target
    staging = target.with_name(target.name + ".tmp")
    staging.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "archive",
            case["commit"],
            "--",
            *case.get("paths", []),
        ],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["tar", "-x", "-C", str(staging)], input=archive.stdout, check=True
    )
    subprocess.run(["git", "init", "-q"], cwd=staging, check=True)
    if apply:
        subprocess.run(["git", "apply", str(apply)], cwd=staging, check=True)
    staging.rename(target)
    return target


def build_task(
    case: dict, cwd: Path, server: ModuleType
) -> tuple[str | None, str]:
    """The role's task text, or None with the error the role returned."""
    base = Path(case["_base"])
    role = case["role"]
    if role == "review":
        diff = str((base / case["diff_file"]).resolve())
        task, error = server._review_task(
            str(cwd), diff, None, case.get("focus")
        )
        return task, error or ""
    if role == "scout":
        return server._scout_task(case["question"]), ""
    if role == "explain_row":
        task, _, error = server._explain_row_task(
            str(cwd), case["log"], case["row"], case.get("occurrence", 0)
        )
        return task, error or ""
    task, _, error, _ = server._triage_task(
        str(cwd), case["log"], case.get("question")
    )
    return task, error or ""


@contextlib.contextmanager
def dekko_scope(config: dict, server: ModuleType) -> Iterator[None]:
    """Set the server's dekko override for a run when the config names one."""
    if config.get("dekko") is None:
        yield
        return
    token = server._dekko_override.set(config["dekko"])
    try:
        yield
    finally:
        server._dekko_override.reset(token)


def _prepare_dekko(config: dict, cwd: Path, server: ModuleType) -> None:
    """With dekko on, make sure the snapshot has a map before the run."""
    if config.get("dekko") is True:
        server._dekko_ensure_map(cwd)


def run_plain(case: dict, config: dict, server: ModuleType) -> dict:
    """Run one explain or summarize case through its tool and score it."""
    with dekko_scope(config, server):
        return _run_plain(case, config, server)


def _run_plain(case: dict, config: dict, server: ModuleType) -> dict:
    base = Path(case["_base"])
    started = time.monotonic()
    if case["role"] == "explain":
        error = (base / case["error_file"]).read_text()
        files = [str((base / f).resolve()) for f in case.get("files", [])]
        text = server._dispatch_nim(
            _explain_prompt(server, error), files=files, model=config["model"]
        )
    else:
        cwd = snapshot(case, server)
        _prepare_dekko(config, cwd, server)
        text = _tool_fn(server.nim_summarize)(
            str(cwd), case["target"], config["model"]
        )
    result = score_plain(case, text)
    result.update(
        scoring=SCORING_VERSION,
        case=case["name"],
        role=case["role"],
        config=config["label"],
        dekko=config.get("dekko"),
        seconds=round(time.monotonic() - started, 1),
        steps=None,
        quotes=[0, 0],
        failed=text.startswith("["),
        answer=text,
    )
    return result


def _tool_fn(tool: Any) -> Any:
    """Unwrap an @mcp.tool() object to its plain function."""
    return getattr(tool, "fn", tool)


def _explain_prompt(server: ModuleType, error: str) -> str:
    """The prompt nim_explain_error builds, captured without a request."""
    captured: dict[str, str] = {}
    real = server._dispatch_nim

    def capture(prompt: str, **_: Any) -> str:
        captured["prompt"] = prompt
        return ""

    server._dispatch_nim = capture
    try:
        _tool_fn(server.nim_explain_error)(error)
    finally:
        server._dispatch_nim = real
    return captured["prompt"]


def score_plain(case: dict, text: str) -> dict:
    """Which expected items the answer names, by keyword alone."""
    lower = text.lower()
    hit = {
        e["id"]
        for e in case["expected"]
        if any(k.lower() in lower for k in e.get("keywords", []))
    }
    musts = {e["id"] for e in case["expected"] if e.get("must")}
    return {
        "leads": 0,
        "matched": 0,
        "hit": sorted(hit),
        "known": len(case["expected"]),
        "must_hit": sorted(hit & musts),
        "musts": len(musts),
    }


def copy_log_file(case: dict, cwd: Path) -> None:
    """Put a case's log_file into the snapshot at its `log` path."""
    if "log_file" not in case:
        return
    source = (Path(case["_base"]) / case["log_file"]).resolve()
    target = cwd / case["log"]
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def _path_matches(path: str, expected: str) -> bool:
    """A bare name matches by basename; a path by itself or a / suffix."""
    if "/" not in path:
        return path == Path(expected).name
    return path == expected or path.endswith("/" + expected)


def snapshot_with_log(case: dict, server: ModuleType) -> Path:
    """The case's code with its log_file added, in a copy of its own.

    The shared code snapshot (or fixture) is never written to. The copy's
    name carries a key of the log path and the log_file's content hash.
    """
    base = snapshot(case, server)
    if "log_file" not in case:
        return base
    source = (Path(case["_base"]) / case["log_file"]).resolve()
    digest = hashlib.sha1(source.read_bytes()).hexdigest()
    key = hashlib.sha1(f"{case['log']}|{digest}".encode()).hexdigest()[:12]
    target = cache_root(server) / "snapshots" / f"{base.name}-log-{key}"
    if target.is_dir():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=target.parent, prefix=".log-"))
    shutil.copytree(base, staging, symlinks=True, dirs_exist_ok=True)
    copy_log_file(case, staging)
    try:
        staging.rename(target)
    except OSError:  # another run won the race; its copy is identical
        shutil.rmtree(staging, ignore_errors=True)
    return target


def _site_hit(cited: str, case: dict) -> bool:
    """Whether the first citation overlaps an expected location's window."""
    match = CITE_RE.search(cited)
    if not match:
        return False
    path = match.group(1).removeprefix("./")
    first = int(match.group(2))
    last = int(match.group(3) or first)
    site = case["expected"][0]
    window = site.get("window", 3)
    for expected in site["locations"]:
        low, high = expected["lines"]
        if _path_matches(path, expected["file"]) and (
            first <= high + window and last >= low - window
        ):
            return True
    return False


def score_explain_row(answer: str, case: dict) -> dict:
    """Score an explain_row answer: does it name the verdict code's site.

    A "Verdict code: not found" is a site miss. A bracketed answer is an
    error.
    """
    if answer.startswith("["):
        return {"site_hit": False, "error": True}
    code = re.search(r"^Verdict code:(.*)$", answer, re.M | re.I)
    if code and code.group(1).strip().lower().startswith("not found"):
        code = None
    return {
        "site_hit": bool(code) and _site_hit(code.group(1), case),
        "error": False,
    }


def run_explain_row_case(case: dict, config: dict, server: ModuleType) -> dict:
    """Run one explain_row case through nim_explain_row and score it."""
    started = time.monotonic()
    try:
        with dekko_scope(config, server):
            cwd = snapshot_with_log(case, server)
            _prepare_dekko(config, cwd, server)
            text = _tool_fn(server.nim_explain_row)(
                str(cwd),
                case["log"],
                case["row"],
                case.get("occurrence", 0),
                "text",
                config["model"],
                None,
            )
    except Exception as exc:  # a failed run is an error row, not a crash
        text = f"[Error: {type(exc).__name__}: {exc}]"
    result = score_explain_row(text, case)
    result.update(
        scoring=SCORING_VERSION,
        case=case["name"],
        role="explain_row",
        config=config["label"],
        dekko=config.get("dekko"),
        seconds=round(time.monotonic() - started, 1),
        answer=text,
    )
    return result


def print_tasks(cases: list[dict], out: Path, server: ModuleType) -> None:
    """Write each case's task text and snapshot path; no NIM requests.

    For a blind screen of explain-row cases: `out/<name>.txt` holds the
    task the agent would get, and
    `out/index.json` lists `{name, snapshot, task_file}` per case, with
    `error` in place of `task_file` when the task cannot be built.
    """
    out.mkdir(parents=True, exist_ok=True)
    index = []
    for case in cases:
        entry: dict[str, str] = {"name": case["name"]}
        try:
            cwd = snapshot_with_log(case, server)
            entry["snapshot"] = str(cwd)
            task, error = build_task(case, cwd, server)
        except Exception as exc:  # one bad case is an entry, not a crash
            task, error = None, f"[Error: {type(exc).__name__}: {exc}]"
        if task is None:
            entry["error"] = error
        else:
            task_file = out / f"{case['name']}.txt"
            task_file.write_text(task)
            entry["task_file"] = str(task_file)
        index.append(entry)
    (out / "index.json").write_text(json.dumps(index, indent=1))
    print(f"tasks: {out} ({len(index)} cases)")


def run_case(case: dict, config: dict, server: ModuleType) -> dict:
    """Run one case under one configuration and score it."""
    if case["role"] in PLAIN_ROLES:
        return run_plain(case, config, server)
    if case["role"] == "explain_row":
        return run_explain_row_case(case, config, server)
    if case["role"] == "edit":
        return run_edit_case(case, config, server)
    with dekko_scope(config, server):
        return _run_agent_case(case, config, server)


def _run_agent_case(case: dict, config: dict, server: ModuleType) -> dict:
    cwd = snapshot(case, server)
    _prepare_dekko(config, cwd, server)
    task, error = build_task(case, cwd, server)
    if task is None:
        return {
            "case": case["name"],
            "config": config["label"],
            "dekko": config.get("dekko"),
            "error": error,
        }
    steps = getattr(server, ROLE_STEPS.get(case["role"], ""), None) or 8
    started = time.monotonic()
    if hasattr(server, "_run_nim_agent"):
        text, stats = server._run_nim_agent(
            task, str(cwd), config["model"], steps, None, config.get("step")
        )
    else:
        text = server._dispatch_nim_agent(
            task, str(cwd), config["model"], steps
        )
        stats = {}
    seconds = round(time.monotonic() - started, 1)
    result = score(case, text)
    if case["role"] == "triage":
        result.update(triage_causes(case, cwd, text, server))

    result.update(
        scoring=SCORING_VERSION,
        case=case["name"],
        role=case["role"],
        config=config["label"],
        dekko=config.get("dekko"),
        seconds=seconds,
        steps=stats.get("steps") or footer_steps(text),
        quotes=stats.get("quotes") or footer_quotes(text),
        failed=text.startswith("["),
        answer=text,
    )
    return result


def edit_refused(text: str) -> bool:
    """True when nim_edit's result text is one of Castor's own refusals."""
    head, tail = EDIT_CWD_REFUSAL
    return (
        text.startswith(EDIT_REFUSED)
        or text.startswith(EDIT_INPUT_REFUSALS)
        or (text.startswith(head) and text.endswith(tail))
    )


def edit_failure(text: str) -> str:
    """The error-row message for a nim_edit run that neither patched nor
    refused."""
    first = text.strip().splitlines()[0] if text.strip() else "empty answer"
    return f"nim_edit failed: {first[:300]}"


def edit_run_failure(
    text: str, patched: bool, raw_answers: list[str]
) -> str | None:
    """Why an edit run is an error row instead of a scored one, or None.

    Args:
        text: nim_edit's result text.
        patched: Whether the run produced a patch.
        raw_answers: The model answers nim_edit parsed during the run.
    """
    for line in text.splitlines():
        if line.startswith(REPAIR_FAILED_LINE):
            return f"nim_edit failed: {line[2:302]}"
    if any(CUT_OFF_NOTE in answer for answer in raw_answers):
        return "nim_edit failed: an answer was cut off by a stall"
    if not patched and not edit_refused(text):
        return edit_failure(text)
    return None


def _as_error_row(row: dict, message: str) -> None:
    """Turn a scored edit row into an error row in place."""
    for key in EDIT_SCORE_KEYS:
        row.pop(key, None)
    row["error"] = message


def score_edit(case: dict, outcome: dict) -> dict:
    """Bucket one edit run: correct, clean_error or wrong_applies.

    Args:
        case: The case; supplies `expect` when the facts do not.
        outcome: The gathered facts: refused, trees_equal, touched_paths,
            reference_paths, apply_failed, check_passed, repair.
    """
    expect = outcome.get("expect") or case.get("expect", "patch")
    refused = outcome["refused"]
    if expect == "refuse":
        correct, clean_error = refused, False
    else:
        correct = (
            not refused
            and bool(outcome["trees_equal"])
            and not outcome["apply_failed"]
        )
        clean_error = refused
    touched = set(outcome["touched_paths"])
    return {
        "correct": correct,
        "clean_error": clean_error,
        "wrong_applies": not correct and not clean_error,
        "out_of_scope": len(touched - set(outcome["reference_paths"])),
        "repair": outcome["repair"],
        "check_passed": outcome["check_passed"],
    }


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run git quietly in cwd with a throwaway identity."""
    return subprocess.run(
        ["git", "-c", "user.email=eval@local", "-c", "user.name=eval", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _commit_base(root: Path) -> None:
    """Make the copy a repo with one commit so `git apply` has a base."""
    steps = [["add", "-A"], ["commit", "-q", "-m", "base"]]
    if not (root / ".git").exists():
        steps.insert(0, ["init", "-q"])
    for args in steps:
        done = _git(args, root)
        if done.returncode:
            raise EditCaseError(f"git {args[0]} failed: {done.stderr.strip()}")


def numstat_paths(diff: Path, cwd: Path) -> set[str]:
    """Every path a diff touches, as `git apply --numstat -z` reports it.

    A rename lists both its old and new path (the old one is read from the
    `rename from` line, so a quoted old path keeps its quotes). An
    unreadable diff gives an empty set.
    """
    done = subprocess.run(
        ["git", "apply", "--numstat", "-z", str(diff)],
        cwd=cwd,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    if done.returncode:
        return set()
    tokens = done.stdout.decode("utf-8", errors="replace").split("\0")
    paths: set[str] = set()
    i = 0
    while i < len(tokens) and tokens[i]:
        path = tokens[i].split("\t", 2)[-1]
        if path:
            paths.add(path)
            i += 1
        else:  # rename: the record ends at the tab, old and new follow
            paths.update(tokens[i + 1 : i + 3])
            i += 3
    # numstat names only the destination of a rename; add the source.
    for line in diff.read_text(errors="replace").splitlines():
        if line.startswith("rename from "):
            paths.add(line[len("rename from ") :])
    return paths


def _trees_equal(work: Path, expected: Path, paths: set[str]) -> bool:
    """True when every path holds the same bytes (or is absent) in both."""

    def read(root: Path, rel: str) -> bytes | None:
        path = root / rel
        return path.read_bytes() if path.is_file() else None

    return all(read(work, p) == read(expected, p) for p in paths)


def _run_check(command: str, cwd: Path) -> bool:
    """Run the case's check command in the patched copy."""
    try:
        done = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return False
    return done.returncode == 0


def _edit_error(case: dict, config: dict, message: str) -> dict:
    return {
        "case": case["name"],
        "config": config["label"],
        "dekko": config.get("dekko"),
        "role": "edit",
        "error": message,
    }


# Raw nim_edit answers seen by the current thread's run (rows run on a
# thread pool), filled by the wrapper capture_answers installs.
_answer_capture = threading.local()
_capture_lock = threading.Lock()


def _install_answer_capture(server: ModuleType) -> None:
    """Wrap server._parse_edit_blocks once so each call's answer is kept.

    The wrapper stays installed and records only while this thread has a
    run open, so concurrent runs never see each other's answers.
    """
    with _capture_lock:
        real = getattr(server, "_parse_edit_blocks", None)
        if real is None or getattr(server, "_eval_capture", False):
            return

        def parse(answer: str) -> Any:
            bucket = getattr(_answer_capture, "answers", None)
            if bucket is not None:
                bucket.append(answer)
            return real(answer)

        server._parse_edit_blocks = parse
        server._eval_capture = True


@contextlib.contextmanager
def capture_answers(server: ModuleType) -> Iterator[list[str]]:
    """Collect the raw answers nim_edit parses during one run."""
    _install_answer_capture(server)
    answers: list[str] = []
    _answer_capture.answers = answers
    try:
        yield answers
    finally:
        _answer_capture.answers = None


def needs_dekko(case: dict, dekko: bool | None) -> bool:
    """True for a must_cover case under a dekko=off configuration."""
    return dekko is False and bool(case.get("must_cover"))


def _needs_dekko_row(case: dict, config: dict) -> dict:
    """The row for a case that cannot run without dekko (no request)."""
    return {
        "case": case["name"],
        "config": config["label"],
        "dekko": config.get("dekko"),
        "role": "edit",
        "needs_dekko": True,
        "scoring": SCORING_VERSION,
    }


def run_edit_case(case: dict, config: dict, server: ModuleType) -> dict:
    """Run one nim_edit case on a copy of its snapshot and score the patch.

    A must_cover case under dekko=off is not run: nim_edit would refuse
    it for want of the dekko map, so it gets a "needs dekko" row.
    """
    if needs_dekko(case, config.get("dekko")):
        return _needs_dekko_row(case, config)
    with dekko_scope(config, server), capture_answers(server) as answers:
        source = snapshot(case, server)
        tmp = Path(tempfile.mkdtemp(prefix="nim-edit-eval-"))
        try:
            row = _run_edit_case(case, config, server, source, tmp, answers)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    row["raw_answers"] = list(answers)
    return row


class EditCaseError(Exception):
    """A case that cannot be scored (bad reference, broken copy)."""


def _run_edit_case(
    case: dict,
    config: dict,
    server: ModuleType,
    source: Path,
    tmp: Path,
    answers: list[str],
) -> dict:
    work, expected = tmp / "work", tmp / "expected"
    try:
        reference = _prepare_copies(case, source, work, expected)
    except EditCaseError as exc:
        return _edit_error(case, config, str(exc))
    _prepare_dekko(config, work, server)
    started = time.monotonic()
    text, stats = server._run_nim_edit(
        case["task"],
        str(work),
        case.get("files"),
        case.get("must_cover"),
        config["model"],
        getattr(server, ROLE_STEPS["edit"], None),
    )
    seconds = round(time.monotonic() - started, 1)
    failure = edit_run_failure(text, stats.get("outcome") == "patch", answers)
    if failure is not None:
        row = _edit_error(case, config, failure)
        row.update(scoring=SCORING_VERSION, seconds=seconds, answer=text)
        row.update(edit_tool_stats(stats))
        return row
    facts = _edit_facts(case, work, expected, reference, stats)
    result = score_edit(case, facts)
    result.update(
        scoring=SCORING_VERSION,
        case=case["name"],
        role="edit",
        config=config["label"],
        dekko=config.get("dekko"),
        seconds=seconds,
        steps=stats.get("steps"),
        expect=facts["expect"],
        facts={
            **facts,
            "touched_paths": sorted(facts["touched_paths"]),
            "reference_paths": sorted(facts["reference_paths"]),
        },
        answer=text,
        **edit_tool_stats(stats),
    )
    return result


def edit_tool_stats(stats: dict) -> dict:
    """The run's propose_edit facts for its row (kept, not printed)."""
    return {
        "edit_tool": bool(stats.get("edit_tool")),
        "edit_refusals": list(stats.get("edit_refusals") or []),
    }


def _prepare_copies(
    case: dict, source: Path, work: Path, expected: Path
) -> Path | None:
    """Copy the snapshot twice, commit both, apply the reference diff.

    Returns:
        The reference diff's path, or None for a refuse case without one.

    Raises:
        EditCaseError: When a copy cannot be committed, a patch case has no
            reference, or the reference touches nothing or fails to apply.
    """
    name = case.get("reference_diff")
    if name is None and case.get("expect") != "refuse":
        raise EditCaseError("patch case has no reference_diff")
    for root in (work, expected):
        shutil.copytree(source, root, symlinks=True)
        _commit_base(root)
    if name is None:
        return None
    reference = (Path(case["_base"]) / name).resolve()
    if not numstat_paths(reference, expected):
        raise EditCaseError("reference diff touches no files")
    applied = _git(["apply", str(reference)], expected)
    if applied.returncode:
        raise EditCaseError(
            f"reference diff does not apply: {applied.stderr.strip()}"
        )
    return reference


def _edit_facts(
    case: dict,
    work: Path,
    expected: Path,
    reference: Path | None,
    stats: dict,
) -> dict:
    """Apply NIM's patch in the work copy and gather what scoring needs."""
    reference_paths = numstat_paths(reference, work) if reference else set()
    facts: dict[str, Any] = {
        "expect": case.get("expect", "patch"),
        "refused": stats.get("outcome") != "patch",
        "trees_equal": None,
        "touched_paths": set(),
        "reference_paths": reference_paths,
        "apply_failed": False,
        "check_passed": None,
        "repair": bool(stats.get("repair")),
    }
    if facts["refused"]:
        return facts
    patch = Path(stats["patch"])
    facts["touched_paths"] = numstat_paths(patch, work)
    if (
        not facts["touched_paths"]
        or _git(["apply", str(patch)], work).returncode
    ):
        facts["apply_failed"] = True
        return facts
    if reference:
        facts["trees_equal"] = _trees_equal(
            work, expected, facts["touched_paths"] | reference_paths
        )
    if case.get("check"):
        facts["check_passed"] = _run_check(case["check"], work)
    return facts


def footer_steps(text: str) -> int | None:
    """Step count from an answer's footer, for servers without stats."""
    match = FOOTER_RE.search(text)
    return int(match.group(1)) if match else None


def footer_quotes(text: str) -> list[int]:
    """[matched, checked] quotes from an answer's footer."""
    match = QUOTES_RE.search(text)
    return [int(match.group(1)), int(match.group(2))] if match else [0, 0]


def leads(text: str, role: str) -> list[str]:
    """Answer lines that cite path:line, before the footer.

    A review's "No issues found." ends its leads: what follows is the list
    of things it checked, not findings. A triage answer states totals and
    warnings in a sentence that ends with its citation, often as a bare
    "line N", so there a line citing either form anywhere is a lead.
    """
    found: list[str] = []
    for line in text.splitlines():
        if line.startswith("[nim_agent:"):
            break
        if role == "review" and "no issues found" in line.lower():
            break
        if _cites(line, role):
            found.append(line)
    return found


def _cites(line: str, role: str) -> bool:
    """True when an answer line counts as citing path:line for its role."""
    if role == "triage":
        return bool(CITE_RE.search(line) or BARE_LINE_RE.search(line))

    body = LEAD_PREFIX_RE.sub("", line, count=1)
    return CITE_RE.match(body) is not None


def citations(line: str, log: str | None = None) -> list[tuple[str, str, str]]:
    """Every (path, first line, last line or "") a lead cites.

    Args:
        line: One answer line.
        log: A triage case's log. A triage answer has one file to cite,
            so its bare "line N" and "lines N-M" count as citing `log`.
    """
    cited = CITE_RE.findall(line)
    if log is not None:
        cited += [(log, a, b) for a, b in BARE_LINE_RE.findall(line)]

    return cited


def matches(line: str, expected: dict, log: str | None = None) -> bool:
    """True when a lead names an expected item's location and substance.

    The location is a file (and line window when known); when the item
    lists keywords, the lead must also contain one, so a lead near the
    right lines about something else does not count. `log` is a triage
    case's log, for leads that cite it by line number alone.
    """
    keywords = [k.lower() for k in expected.get("keywords", [])]
    if keywords and not any(k in line.lower() for k in keywords):
        return False
    window = expected.get("window", DEFAULT_WINDOW)
    for path, start, end in citations(line, log):
        name = Path(path).name
        a, b = int(start), int(end or start)
        for loc in expected.get("locations", []):
            lines = loc.get("lines")
            if Path(loc["file"]).name != name:
                continue
            if lines is None or (
                a - window <= lines[-1] and b + window >= lines[0]
            ):
                return True
    return False


def score(case: dict, text: str) -> dict:
    """Leads, how many hit an expected item, and which items were hit."""
    found = leads(text, case["role"])
    log = case.get("log") if case["role"] == "triage" else None
    hit: set[str] = set()
    matched = 0
    for line in found:
        ids = [e["id"] for e in case["expected"] if matches(line, e, log)]
        matched += bool(ids)
        hit.update(ids)
    musts = {e["id"] for e in case["expected"] if e.get("must")}
    return {
        "leads": len(found),
        "matched": matched,
        "hit": sorted(hit),
        "known": len(case["expected"]),
        "must_hit": sorted(hit & musts),
        "musts": len(musts),
    }


def triage_failures(
    case: dict, cwd: Path, text: str, server: ModuleType
) -> list[dict] | None:
    """Castor's checked reading of the items in a triage answer.

    Args:
        case: The triage case; `log` is relative to `cwd`.
        cwd: The case's code, as the agent saw it.
        text: The agent's answer.
        server: The server under test.

    Returns:
        The `failures` of the server's JSON triage answer, or None when
        the answer is an error or the server has no such check (a copy
        from before causes were bound to log text).
    """
    read = getattr(server, "_read_triage_log", None)
    parse = getattr(server, "_triage_json", None)
    if read is None or parse is None or text.startswith("["):
        return None

    lines, fmt = read(str(cwd), cwd / case["log"])
    return parse(text, {}, case["log"], lines, fmt)["failures"]


def score_causes(case: dict, failures: list[dict]) -> dict:
    """How many causes Castor kept, and which known causes they reach.

    A known cause is an expected item with `cause_lines`. It is found
    when a kept cause sits on one of those lines, whichever item cited
    it, so a row's own clipped reason does not count as finding it.
    """
    kept = [f for f in failures if f.get("cause_shown")]
    known = [e for e in case["expected"] if e.get("cause_lines")]
    cited = {f["cause_line"] for f in kept if f.get("cause_line")}
    found = [
        e["id"]
        for e in known
        if any(a <= n <= b for a, b in e["cause_lines"] for n in cited)
    ]
    return {
        "causes": {
            "items": len(failures),
            "kept": len(kept),
            "row": sum(f.get("cause_source") == "row" for f in kept),
            "log": sum(f.get("cause_source") == "log" for f in kept),
        },
        "cause_hit": sorted(found),
        "cause_known": len(known),
    }


def triage_causes(
    case: dict, cwd: Path, text: str, server: ModuleType
) -> dict:
    """The cause score of a triage answer; {} when it cannot be checked."""
    failures = triage_failures(case, cwd, text, server)
    return {} if failures is None else score_causes(case, failures)


def summarize(results: list[dict], label: str) -> str:
    """One table row for a configuration across its runs.

    Precision covers agent roles only; plain roles have no leads.
    """
    explains = [
        r
        for r in results
        if r["config"] == label and r.get("role") == "explain_row"
    ]
    main = _summarize_main(results, label)
    if not explains:
        return main or summarize_rows([], label)
    explain_lines = summarize_explain_row(explains, label)
    return f"{main}\n{explain_lines}" if main else explain_lines


def _summarize_main(results: list[dict], label: str) -> str:
    """The review, scout, triage and edit lines ("" when there are none)."""
    mine = [r for r in results if r["config"] == label]
    edits = [r for r in mine if r.get("role") == "edit"]
    rows = [r for r in mine if "leads" in r]
    caused = [r for r in rows if "causes" in r]
    lines = []
    if rows:
        lines.append(summarize_rows(rows, label))
    if caused:
        lines.append(summarize_causes(caused, label))
    if edits:
        lines.append(summarize_edit(edits, label))

    return "\n".join(lines)


def summarize_causes(rows: list[dict], label: str) -> str:
    """The triage line: causes Castor kept, and known causes they reach."""
    total = {
        key: sum(r["causes"][key] for r in rows)
        for key in ("items", "kept", "row", "log")
    }
    found = sum(len(r["cause_hit"]) for r in rows)
    known = sum(r["cause_known"] for r in rows)
    return (
        f"{label:<24} triage causes kept {total['kept']}/{total['items']} "
        f"(row {total['row']}, log {total['log']})  "
        f"real cause found {found}/{known}"
    )


def _causes_note(row: dict) -> str:
    """One run's cause score for its report line ("" without one)."""
    if "causes" not in row:
        return ""

    causes = row["causes"]
    return (
        f" causes kept {causes['kept']}/{causes['items']} "
        f"found {len(row['cause_hit'])}/{row['cause_known']}"
    )


def explain_row_bars(n: int) -> dict[str, int]:
    """The gate bars for one run of n cases: site on 80%, no errors."""
    return {"site": math.ceil(0.8 * n), "errors": 0}


def _explain_run_line(rows: list[dict], label: str, run: int) -> str:
    n = len(rows)
    return (
        f"{label:<24} explain_row run {run}: "
        f"site {sum(r['site_hit'] for r in rows)}/{n}, "
        f"errors {sum(r['error'] for r in rows)}"
    )


def _explain_run_misses(rows: list[dict], run: int) -> list[str]:
    """The gate bars one run misses (`explain_row_bars` for its size)."""
    bars = explain_row_bars(len(rows))
    site = sum(r["site_hit"] for r in rows)
    errors = sum(r["error"] for r in rows)
    misses = []
    if site < bars["site"]:
        misses.append(f"site {site} < {bars['site']} in run {run}")
    if errors > bars["errors"]:
        misses.append(f"errors {errors} > 0 in run {run}")
    return misses


def summarize_explain_row(rows: list[dict], label: str) -> str:
    """One line per run of a configuration, then the gate verdict."""
    runs: dict[int, list[dict]] = {}
    for r in rows:
        runs.setdefault(r.get("run", 1), []).append(r)
    lines, misses = [], []
    for run in sorted(runs):
        lines.append(_explain_run_line(runs[run], label, run))
        misses += _explain_run_misses(runs[run], run)
    verdict = "pass" if not misses else f"fail ({'; '.join(misses)})"
    lines.append(f"{label:<24} explain_row gate: {verdict}")
    return "\n".join(lines)


def summarize_edit(rows: list[dict], label: str) -> str:
    """One table row for a configuration's edit runs (errors counted)."""
    errors = sum("error" in r for r in rows)
    needs = sum(bool(r.get("needs_dekko")) for r in rows)
    rows = [r for r in rows if "error" not in r and not r.get("needs_dekko")]
    if not rows:
        return (
            f"{label:<24} edit no completed runs  errors {errors}  "
            f"needs_dekko {needs}"
        )
    patch = [r for r in rows if r.get("expect", "patch") != "refuse"]
    refuse = [r for r in rows if r.get("expect") == "refuse"]
    right = sum(r["correct"] for r in patch)
    share = f" ({right / len(patch):.0%})" if patch else ""
    steps = [r["steps"] for r in rows if r["steps"]]
    return (
        f"{label:<24} edit runs {len(rows):>2}  "
        f"correct {right}/{len(patch)}{share}  "
        f"wrong_applies {sum(r['wrong_applies'] for r in rows)}  "
        f"out_of_scope {sum(r['out_of_scope'] for r in rows)}  "
        f"refusals refused {sum(r['correct'] for r in refuse)}/{len(refuse)}  "
        f"{statistics.median(r['seconds'] for r in rows):>4.0f}s  "
        f"{statistics.median(steps) if steps else 0:.1f} steps  "
        f"errors {errors}  needs_dekko {needs}"
    )


def summarize_rows(rows: list[dict], label: str) -> str:
    """The row for agent and plain runs; see summarize."""
    if not rows:
        return f"{label:<24} no completed runs"
    cited = [r for r in rows if r.get("role") not in PLAIN_ROLES]
    leads_n = sum(r["leads"] for r in cited)
    matched = sum(r["matched"] for r in cited)
    hit = sum(len(r["hit"]) for r in rows)
    known = sum(r["known"] for r in rows)
    must = sum(len(r["must_hit"]) for r in rows)
    musts = sum(r["musts"] for r in rows)
    q_ok = sum(r["quotes"][0] for r in rows)
    q_all = sum(r["quotes"][1] for r in rows)
    secs = sum(r["seconds"] for r in rows) / len(rows)
    steps = [r["steps"] for r in rows if r["steps"]]
    mean_steps = sum(steps) / len(steps) if steps else 0
    failed = sum(r["failed"] for r in rows)
    precision = (
        f"precision {matched}/{leads_n} ({matched / leads_n:.0%})  "
        if cited and leads_n
        else ("precision -  " if not cited else "precision 0/0  ")
    )
    return (
        f"{label:<24} runs {len(rows):>2}  failed {failed}  {precision}"
        f"recall {hit}/{known}  must {must}/{musts}  "
        f"quotes {q_ok}/{q_all}  {secs:>4.0f}s  {mean_steps:.1f} steps"
    )


def rescore(
    path: Path, cases: list[dict], server: ModuleType | None = None
) -> None:
    """Score saved answers again, write them back in place, and report.

    Run it after editing the cases or the scorer; rows whose case is not
    given keep their old scores. A triage row gets its cause score only
    when `server` is given, since that check is the server's.
    """
    by_name = {c["name"]: c for c in cases}
    results = json.loads(path.read_text())
    stale = stale_rows(results)
    if stale:
        print(
            f"warning: {stale} of {len(results)} rows in {path.name} were "
            f"scored by an older scorer (current {SCORING_VERSION}); "
            "re-scoring",
            file=sys.stderr,
        )
    for r in results:
        case = by_name.get(r.get("case"))
        if case and "answer" in r:
            _rescore_row(r, case, server)
    path.write_text(json.dumps(results, indent=1))
    report(results, sorted({r["config"] for r in results}))


def _rescore_row(row: dict, case: dict, server: ModuleType | None) -> None:
    """Score one saved answer again, in place."""
    role = case["role"]
    if role == "edit":
        _rescore_edit(row, case)
        return

    if role == "explain_row":
        row.update(score_explain_row(row["answer"], case), role=role)
    else:
        scorer = score_plain if role in PLAIN_ROLES else score
        row.update(scorer(case, row["answer"]), role=role)

    if role == "triage" and server is not None:
        cwd = snapshot_with_log(case, server)
        row.update(triage_causes(case, cwd, row["answer"], server))

    row["scoring"] = SCORING_VERSION


def _rescore_edit(row: dict, case: dict) -> None:
    """Score a saved edit row again; a failed run becomes an error row.

    Rows from scorer 3 and older set facts["refused"] for any run without
    a patch, so a refusal is re-read from the answer text. A failed
    repair run or a stalled answer (edit_run_failure) also makes an error
    row. A must_cover case run under dekko=off becomes a "needs dekko" row.
    """
    failure = None
    if "facts" in row:
        failure = edit_run_failure(
            row["answer"],
            not row["facts"]["refused"],
            row.get("raw_answers") or [],
        )
    if needs_dekko(case, row.get("dekko")):
        for key in EDIT_SCORE_KEYS:
            row.pop(key, None)
        row.pop("error", None)
        row["needs_dekko"] = True
    elif "facts" not in row:
        return
    elif failure is not None:
        _as_error_row(row, failure)
    else:
        row.update(score_edit(case, row["facts"]), role="edit")
    row["scoring"] = SCORING_VERSION


def stale_rows(results: list[dict]) -> int:
    """How many rows lack the current SCORING_VERSION."""
    return sum(r.get("scoring") != SCORING_VERSION for r in results)


def report(results: list[dict], labels: list[str]) -> None:
    """Print one line per run, then one summary row per configuration."""
    for r in results:
        if r.get("role") == "explain_row":
            print(
                f"{r['config']:<24} {r['case']:<22} explain_row "
                f"run {r.get('run', 1)} "
                f"site {int(r['site_hit'])} "
                f"{'ERROR ' if r['error'] else ''}{r['seconds']:.0f}s"
            )
            continue
        if r.get("needs_dekko"):
            print(f"{r['config']:<24} {r['case']:<22} edit needs dekko")
            continue
        if "error" in r:
            print(f"{r['config']:<24} {r['case']:<22} ERROR {r['error']}")
            continue
        if r.get("role") == "edit":
            bucket = next(
                k for k in ("correct", "clean_error", "wrong_applies") if r[k]
            )
            print(
                f"{r['config']:<24} {r['case']:<22} edit {bucket} "
                f"out_of_scope {r['out_of_scope']} {r['seconds']:.0f}s"
            )
            continue
        print(
            f"{r['config']:<24} {r['case']:<22} leads {r['leads']:>2} "
            f"matched {r['matched']:>2} hit {','.join(r['hit']) or '-'} "
            f"{r['seconds']:.0f}s{_causes_note(r)}"
            f"{' FAILED' if r['failed'] else ''}"
        )
    print()
    for label in labels:
        print(summarize(results, label))


def parse_config(raw: str) -> dict:
    """label=model[,step=model] into a configuration dict."""
    label, _, spec = raw.partition("=")
    model, *rest = spec.split(",")
    config: dict[str, Any] = {"label": label, "model": model or None}
    for item in rest:
        key, _, value = item.partition("=")
        if key == "step":
            config["step"] = value
        elif key == "dekko":
            if value not in ("on", "off"):
                raise ValueError(f"{key} must be on or off, got {value!r}")
            config[key] = value == "on"
    return config


def select_cases(
    cases: list[dict], only: list[str], roles: list[str]
) -> list[dict]:
    """Cases whose name is in only and whose role is in roles (empty = all)."""
    return [
        c
        for c in cases
        if (not only or c["name"] in only)
        and (not roles or c["role"] in roles)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("cases", type=Path, nargs="+")
    parser.add_argument(
        "--config",
        action="append",
        default=[],
        help=(
            "label=model[,step=model][,dekko=on|off]; "
            "repeatable (default: server default)"
        ),
    )
    parser.add_argument("--server", type=Path, default=ROOT / "server.py")
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument(
        "--role",
        action="append",
        default=[],
        help="keep only cases with this role; repeatable",
    )
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument(
        "--print-tasks",
        type=Path,
        help="write each case's task text to this folder; no requests",
    )
    parser.add_argument(
        "--rescore",
        type=Path,
        help="re-score a saved results file against the cases; no requests",
    )
    args = parser.parse_args()

    server = load_server(args.server.resolve())
    server._cache_get = lambda _key: None  # every run asks NIM afresh
    cases = []
    for path in args.cases:
        for case in json.loads(path.read_text()):
            case["_base"] = str(path.resolve().parent)
            cases.append(case)
    cases = select_cases(cases, args.only, args.role)
    if args.print_tasks:
        print_tasks(cases, args.print_tasks.expanduser(), server)
        return
    if args.rescore:
        rescore(args.rescore, cases, server)
        return
    configs = [parse_config(c) for c in args.config] or [
        {"label": "default", "model": None}
    ]
    for case in cases:  # extract snapshots before the parallel runs
        if "repo" in case or "dir" in case:
            snapshot_with_log(case, server)
    work = [
        (case, config, run)
        for config in configs
        for case in cases
        for run in range(1, args.runs + 1)
    ]
    with ThreadPoolExecutor(max(1, args.jobs)) as pool:
        results = list(pool.map(lambda w: run_case(w[0], w[1], server), work))
    for (_, _, run), row in zip(work, results, strict=True):
        if row.get("role") == "explain_row":
            row["run"] = run
    report(results, [c["label"] for c in configs])
    out = cache_root(server) / f"results-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=1))
    print(f"\nresults: {out}")


if __name__ == "__main__":
    main()
