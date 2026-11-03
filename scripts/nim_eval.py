"""Score Castor's NIM roles and plain tools against cases with known answers.

Each case names a role (review, scout or triage for the agent; explain or
summarize for plain calls), where its code comes from, and the findings,
ranges, log lines or key points a good answer holds. The script runs every
case under every model configuration. Agent answers are scored by their
path:line citations (lead precision, recall, must-catch hits); plain
answers by keywords alone (recall of known causes or key points).

    uv run --with httpx --with fastmcp python scripts/nim_eval.py \\
        scripts/eval/cases.json \\
        --config ultra=nvidia/nemotron-3-ultra-550b-a55b

NVIDIA_API_KEY or NVIDIA_API_KEYS must be set. Code is taken read-only
with `git archive` into ~/.cache/claude-castor/eval/snapshots/, so a case
can point at another repo without touching its working tree. Keep cases
for private repos outside this repo (for example under that cache dir).
Results are written as JSON beside the snapshots.

Case fields (JSON list of objects):
    name, role                 "review" | "scout" | "triage" | "explain"
                               | "summarize"
    repo, commit, paths        snapshot source (paths limit the archive)
    apply                      optional diff applied to the snapshot
    dir                        or a fixture directory instead of a repo
    diff_file, focus           review inputs
    question                   scout input (and optional for triage)
    log                        triage input, relative to the case's code
    error_file, files          explain inputs: a file holding the error
                               text, and source files to inline
    target                     summarize input, relative to the case's code
    expected                   [{id, locations: [{file, lines: [a, b]}],
                                 keywords, window, must}]; plain roles use
                                 keywords only
Relative paths are resolved against the cases file. The response cache is
off during a run, so repeated runs are independent.
"""

import argparse
import contextlib
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CITE_RE = re.compile(r"([\w./-]+\.\w+):(\d+)(?:-(\d+))?")
# A lead opens with its citation after list or emphasis markers; a line
# that only cites evidence in passing is part of the lead above it.
LEAD_PREFIX_RE = re.compile(r"^[\s>#*_`\-\d.)]*")
FOOTER_RE = re.compile(r"\[nim_agent: (\d+) steps")
QUOTES_RE = re.compile(r"quotes matched (\d+)/(\d+)")
DEFAULT_WINDOW = 10
# Stamped on every result row. Bump it whenever score() or score_plain()
# changes, so a saved file scored the old way is flagged and re-scored.
# 1: keyword-gated location matching. 2: plain roles scored by keyword.
SCORING_VERSION = 2
ROLE_STEPS = {"review": "NIM_REVIEW_STEPS", "scout": "NIM_SCOUT_STEPS"}
# Plain tool calls: no agent loop, scored by keywords over the whole answer.
PLAIN_ROLES = ("explain", "summarize")


def load_server(path: Path) -> ModuleType:
    """Import a server.py (the current one, or an older copy to compare)."""
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
    task, _, error = server._triage_task(
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


def run_case(case: dict, config: dict, server: ModuleType) -> dict:
    """Run one case under one configuration and score it."""
    if case["role"] in PLAIN_ROLES:
        return run_plain(case, config, server)
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
    of things it checked, not findings.
    """
    found: list[str] = []
    for line in text.splitlines():
        if line.startswith("[nim_agent:"):
            break
        if role == "review" and "no issues found" in line.lower():
            break
        body = LEAD_PREFIX_RE.sub("", line, count=1)
        if CITE_RE.match(body):
            found.append(line)
    return found


def matches(line: str, expected: dict) -> bool:
    """True when a lead names an expected item's location and substance.

    The location is a file (and line window when known); when the item
    lists keywords, the lead must also contain one, so a lead near the
    right lines about something else does not count.
    """
    keywords = [k.lower() for k in expected.get("keywords", [])]
    if keywords and not any(k in line.lower() for k in keywords):
        return False
    window = expected.get("window", DEFAULT_WINDOW)
    for path, start, end in CITE_RE.findall(line):
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
    hit: set[str] = set()
    matched = 0
    for line in found:
        ids = [e["id"] for e in case["expected"] if matches(line, e)]
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


def summarize(results: list[dict], label: str) -> str:
    """One table row for a configuration across its runs.

    Precision covers agent roles only; plain roles have no leads.
    """
    rows = [r for r in results if r["config"] == label and "leads" in r]
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


def rescore(path: Path, cases: list[dict]) -> None:
    """Score saved answers again, write them back in place, and report.

    Run it after editing the cases or the scorer; rows whose case is not
    given keep their old scores.
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
            scorer = score_plain if case["role"] in PLAIN_ROLES else score
            r.update(scorer(case, r["answer"]), role=case["role"])
            r["scoring"] = SCORING_VERSION
    path.write_text(json.dumps(results, indent=1))
    report(results, sorted({r["config"] for r in results}))


def stale_rows(results: list[dict]) -> int:
    """How many rows lack the current SCORING_VERSION."""
    return sum(r.get("scoring") != SCORING_VERSION for r in results)


def report(results: list[dict], labels: list[str]) -> None:
    """Print one line per run, then one summary row per configuration."""
    for r in results:
        if "error" in r:
            print(f"{r['config']:<24} {r['case']:<22} ERROR {r['error']}")
            continue
        print(
            f"{r['config']:<24} {r['case']:<22} leads {r['leads']:>2} "
            f"matched {r['matched']:>2} hit {','.join(r['hit']) or '-'} "
            f"{r['seconds']:.0f}s{' FAILED' if r['failed'] else ''}"
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
                raise ValueError(f"dekko must be on or off, got {value!r}")
            config["dekko"] = value == "on"
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
            "label=model[,step=model][,dekko=on|off]; repeatable "
            "(default: server default)"
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
    if args.rescore:
        rescore(args.rescore, cases)
        return
    configs = [parse_config(c) for c in args.config] or [
        {"label": "default", "model": None}
    ]
    for case in cases:  # extract snapshots before the parallel runs
        if "repo" in case or "dir" in case:
            snapshot(case, server)
    work = [
        (case, config)
        for config in configs
        for case in cases
        for _ in range(args.runs)
    ]
    with ThreadPoolExecutor(max(1, args.jobs)) as pool:
        results = list(pool.map(lambda w: run_case(w[0], w[1], server), work))
    report(results, [c["label"] for c in configs])
    out = cache_root(server) / f"results-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=1))
    print(f"\nresults: {out}")


if __name__ == "__main__":
    main()
