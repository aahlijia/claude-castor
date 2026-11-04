"""Tests for the scoring in scripts/nim_eval.py (no network)."""

import contextvars
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "nim_eval", ROOT / "scripts" / "nim_eval.py"
)
assert _spec is not None and _spec.loader is not None
nim_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nim_eval)

CASE = {
    "name": "c",
    "role": "review",
    "expected": [
        {
            "id": "bug",
            "must": True,
            "window": 3,
            "keywords": ["prefix"],
            "locations": [{"file": "src/server.py", "lines": [40, 41]}],
        },
        {
            "id": "file-only",
            "locations": [{"file": "run.sh", "lines": None}],
            "keywords": ["inputcheck"],
        },
    ],
}


def test_leads_open_with_their_citation():
    answer = (
        "## Critical\n"
        "- **server.py:42 - prefix check**\n"
        "  - Result: see `tests/test_x.py:9` for the case\n"
        "1. run.sh:7 - inputcheck runs too early\n"
        "[nim_agent: 3 steps, 2 tool calls, model m]\n"
        "- server.py:1 - after the footer\n"
    )
    assert nim_eval.leads(answer, "review") == [
        "- **server.py:42 - prefix check**",
        "1. run.sh:7 - inputcheck runs too early",
    ]


def test_review_leads_stop_at_no_issues_found():
    answer = "No issues found. Checked server.py:10 and run.sh:4."
    assert nim_eval.leads(answer, "review") == []
    assert nim_eval.leads("- run.log:3 - FAIL x", "triage") == [
        "- run.log:3 - FAIL x"
    ]


def test_match_needs_location_and_a_keyword():
    bug = CASE["expected"][0]
    assert nim_eval.matches("- server.py:43 - prefix check", bug)
    assert not nim_eval.matches("- server.py:43 - unrelated", bug)
    assert not nim_eval.matches("- server.py:50 - prefix check", bug)
    assert not nim_eval.matches("- other.py:41 - prefix check", bug)
    only_file = CASE["expected"][1]
    assert nim_eval.matches("- run.sh:900 - inputcheck order", only_file)


def test_score_counts_leads_hits_and_musts():
    answer = (
        "- server.py:40-41 - prefix check lets a sibling through\n"
        "- server.py:40 - prefix again, same finding\n"
        "- server.py:200 - invented\n"
    )
    result = nim_eval.score(CASE, answer)
    assert result["leads"] == 3 and result["matched"] == 2
    assert result["hit"] == ["bug"] and result["must_hit"] == ["bug"]
    assert result["known"] == 2 and result["musts"] == 1


def test_parse_config_reads_model_and_step_model():
    assert nim_eval.parse_config("a=vendor/big") == {
        "label": "a",
        "model": "vendor/big",
    }
    assert nim_eval.parse_config("b=vendor/big,step=vendor/fast") == {
        "label": "b",
        "model": "vendor/big",
        "step": "vendor/fast",
    }


def test_plain_cases_score_by_keyword_only():
    case = {
        "name": "s",
        "role": "summarize",
        "expected": [
            {"id": "cache", "must": True, "keywords": ["use_cache"]},
            {"id": "jobs", "keywords": ["gemini_poll"]},
        ],
    }
    result = nim_eval.score_plain(case, '{"key_points": ["Pass USE_CACHE"]}')
    assert result["hit"] == ["cache"] and result["must_hit"] == ["cache"]
    assert result["leads"] == 0 and result["known"] == 2


def test_report_leaves_precision_out_for_plain_roles():
    row = {
        "config": "a",
        "role": "explain",
        "leads": 0,
        "matched": 0,
        "hit": ["x"],
        "known": 1,
        "must_hit": ["x"],
        "musts": 1,
        "quotes": [0, 0],
        "seconds": 3.0,
        "steps": None,
        "failed": False,
    }
    line = nim_eval.summarize([row], "a")
    assert "precision -" in line and "recall 1/1" in line


def test_rescore_flags_old_files_and_writes_the_version(tmp_path, capsys):
    saved = tmp_path / "results.json"
    row = {"case": "c", "config": "a", "answer": "- server.py:41 - prefix"}
    saved.write_text(
        json.dumps(
            [
                {
                    **row,
                    "seconds": 1.0,
                    "quotes": [0, 0],
                    "steps": 1,
                    "failed": False,
                }
            ]
        )
    )
    nim_eval.rescore(saved, [CASE])
    assert "older scorer" in capsys.readouterr().err
    rows = json.loads(saved.read_text())
    assert rows[0]["scoring"] == nim_eval.SCORING_VERSION
    assert rows[0]["hit"] == ["bug"]
    nim_eval.rescore(saved, [CASE])
    assert "older scorer" not in capsys.readouterr().err


def test_parse_config_reads_dekko():
    assert nim_eval.parse_config("s=nvidia/x,dekko=off") == {
        "label": "s",
        "model": "nvidia/x",
        "dekko": False,
    }
    assert nim_eval.parse_config("s=m,dekko=on")["dekko"] is True
    assert "dekko" not in nim_eval.parse_config("s=m")
    with pytest.raises(ValueError):
        nim_eval.parse_config("s=m,dekko=maybe")


def _fake_server(seen: dict) -> SimpleNamespace:
    override = contextvars.ContextVar("o", default=None)

    def scout_task(_question):
        seen["at_task"] = override.get()
        return "task"

    def run_agent(*_a):
        seen["at_run"] = override.get()
        return "- a.py:1 - x", {"steps": 1}

    def ensure_map(root):
        seen["map"] = root
        return True

    return SimpleNamespace(
        _dekko_override=override,
        _dekko_ensure_map=ensure_map,
        _scout_task=scout_task,
        _run_nim_agent=run_agent,
    )


def _dir_case(tmp_path):
    return {
        "name": "c",
        "role": "scout",
        "question": "q",
        "dir": ".",
        "_base": str(tmp_path),
        "expected": [],
    }


def test_run_case_scopes_the_dekko_override(tmp_path):
    seen: dict = {}
    server = _fake_server(seen)
    config = {"label": "on", "model": None, "dekko": True}
    result = nim_eval.run_case(_dir_case(tmp_path), config, server)
    assert seen["at_task"] is True and seen["at_run"] is True
    assert seen["map"] == tmp_path.resolve()
    assert result["dekko"] is True
    assert server._dekko_override.get() is None  # reset afterwards


def test_run_case_leaves_override_unset_without_dekko_key(tmp_path):
    seen: dict = {}
    server = _fake_server(seen)
    config = {"label": "d", "model": None}
    result = nim_eval.run_case(_dir_case(tmp_path), config, server)
    assert seen["at_run"] is None and "map" not in seen
    assert result["dekko"] is None


def test_run_case_off_sets_false_without_building_map(tmp_path):
    seen: dict = {}
    server = _fake_server(seen)
    config = {"label": "off", "model": None, "dekko": False}
    nim_eval.run_case(_dir_case(tmp_path), config, server)
    assert seen["at_run"] is False and "map" not in seen


def test_select_cases_filters_by_name_and_role():
    cases = [
        {"name": "a", "role": "scout"},
        {"name": "b", "role": "review"},
        {"name": "c", "role": "triage"},
    ]
    assert nim_eval.select_cases(cases, [], []) == cases
    by_name = nim_eval.select_cases(cases, ["a", "b"], [])
    assert [c["name"] for c in by_name] == ["a", "b"]
    by_role = nim_eval.select_cases(cases, [], ["review", "triage"])
    assert [c["name"] for c in by_role] == ["b", "c"]
    assert nim_eval.select_cases(cases, ["a"], ["review"]) == []


def _facts(**over):
    base = {
        "expect": "patch",
        "refused": False,
        "trees_equal": True,
        "touched_paths": {"a.py"},
        "reference_paths": {"a.py"},
        "apply_failed": False,
        "check_passed": None,
        "repair": False,
    }
    return {**base, **over}


def _buckets(result):
    return [
        k for k in ("correct", "clean_error", "wrong_applies") if result[k]
    ]


def test_score_edit_identical_trees_is_correct():
    result = nim_eval.score_edit({}, _facts(repair=True, check_passed=True))
    assert _buckets(result) == ["correct"]
    assert result["repair"] is True and result["check_passed"] is True


def test_score_edit_different_trees_is_wrong_applies():
    result = nim_eval.score_edit({}, _facts(trees_equal=False))
    assert _buckets(result) == ["wrong_applies"]


def test_score_edit_refusal_on_patch_case_is_clean_error():
    result = nim_eval.score_edit(
        {}, _facts(refused=True, trees_equal=None, touched_paths=set())
    )
    assert _buckets(result) == ["clean_error"]


def test_score_edit_refusal_on_refuse_case_is_correct():
    result = nim_eval.score_edit(
        {}, _facts(expect="refuse", refused=True, trees_equal=None)
    )
    assert _buckets(result) == ["correct"]


def test_score_edit_patch_on_refuse_case_is_wrong_applies():
    result = nim_eval.score_edit({}, _facts(expect="refuse"))
    assert _buckets(result) == ["wrong_applies"]


def test_score_edit_counts_out_of_scope_paths():
    result = nim_eval.score_edit(
        {}, _facts(touched_paths={"a.py", "b.py"}, trees_equal=False)
    )
    assert result["out_of_scope"] == 1


def test_score_edit_apply_failure_is_wrong_applies():
    result = nim_eval.score_edit({}, _facts(apply_failed=True))
    assert _buckets(result) == ["wrong_applies"]


def test_score_edit_takes_expect_from_the_case_when_absent():
    facts = _facts(refused=True, trees_equal=None)
    del facts["expect"]
    result = nim_eval.score_edit({"expect": "refuse"}, facts)
    assert _buckets(result) == ["correct"]


def test_summarize_edit_rows_prints_the_edit_line():
    def row(name, expect, correct, wrong, oos, secs, steps):
        return {
            "config": "a",
            "role": "edit",
            "case": name,
            "expect": expect,
            "correct": correct,
            "clean_error": False,
            "wrong_applies": wrong,
            "out_of_scope": oos,
            "seconds": secs,
            "steps": steps,
        }

    rows = [
        row("p1", "patch", True, False, 0, 10.0, 4),
        row("p2", "patch", False, True, 2, 30.0, 8),
        row("r1", "refuse", True, False, 0, 20.0, 6),
    ]
    line = nim_eval.summarize(rows, "a")
    assert "correct 1/2 (50%)" in line
    assert "wrong_applies 1" in line
    assert "out_of_scope 2" in line
    assert "refusals refused 1/1" in line
    assert "20s" in line and "6.0 steps" in line


def _edit_fixture(tmp_path, new_text):
    """A fixture dir, a reference diff, and a fake server writing a patch."""
    code = tmp_path / "code"
    code.mkdir()
    (code / "a.txt").write_text("one\n")
    ref = (
        "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
        "@@ -1 +1 @@\n-one\n+two\n"
    )
    (tmp_path / "ref.diff").write_text(ref)
    patch = tmp_path / "nim.diff"
    patch.write_text(ref.replace("+two", "+" + new_text))
    case = {
        "name": "e",
        "role": "edit",
        "_base": str(tmp_path),
        "dir": "code",
        "task": "t",
        "reference_diff": "ref.diff",
        "check": "grep -q . a.txt",
    }

    def run(task, cwd, files, cover, model, steps):
        return "ok", {"outcome": "patch", "patch": str(patch), "steps": 3}

    return case, SimpleNamespace(
        _run_nim_edit=run,
        NIM_EDIT_STEPS=12,
        _dekko_override=contextvars.ContextVar("o", default=None),
    )


def test_run_edit_case_scores_matching_patch_correct(tmp_path):
    case, server = _edit_fixture(tmp_path, "two")
    row = nim_eval.run_edit_case(case, {"label": "a", "model": None}, server)
    assert row["correct"] and row["check_passed"] is True
    assert row["facts"]["touched_paths"] == ["a.txt"]
    assert (tmp_path / "code" / "a.txt").read_text() == "one\n"


def test_run_edit_case_scores_different_patch_wrong_applies(tmp_path):
    case, server = _edit_fixture(tmp_path, "three")
    row = nim_eval.run_edit_case(case, {"label": "a", "model": None}, server)
    assert row["wrong_applies"] and not row["correct"]


CONFIG = {"label": "a", "model": None}


def _answer(server, **stats):
    refused = stats.get("outcome") == "refused"
    text = "[nim_edit: REFUSED]\n- x" if refused else "text"
    server._run_nim_edit = lambda *a: (text, stats)


def test_run_edit_case_refusal_on_patch_case_is_clean_error(tmp_path):
    case, server = _edit_fixture(tmp_path, "two")
    _answer(server, outcome="refused", patch=None, steps=2)
    row = nim_eval.run_edit_case(case, CONFIG, server)
    assert row["clean_error"] and not row["wrong_applies"]


def test_run_edit_case_copies_the_edit_tool_stats(tmp_path, capsys):
    case, server = _edit_fixture(tmp_path, "two")
    refusals = ["edit refused: overlaps block 1 in a.txt"]
    _answer(
        server,
        outcome="patch",
        patch=str(tmp_path / "nim.diff"),
        steps=2,
        edit_tool=True,
        edit_refusals=refusals,
    )
    row = nim_eval.run_edit_case(case, CONFIG, server)
    assert row["correct"]
    assert row["edit_tool"] is True and row["edit_refusals"] == refusals
    nim_eval.report([row], ["a"])
    assert "overlaps block" not in capsys.readouterr().out


def test_run_edit_case_defaults_the_edit_tool_stats(tmp_path):
    case, server = _edit_fixture(tmp_path, "two")
    row = nim_eval.run_edit_case(case, CONFIG, server)
    assert row["edit_tool"] is False and row["edit_refusals"] == []


def test_run_edit_case_unappliable_patch_is_wrong(tmp_path):
    case, server = _edit_fixture(tmp_path, "two")
    bad = tmp_path / "bad.diff"
    bad.write_text(
        "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
        "@@ -1 +1 @@\n-missing\n+two\n"
    )
    _answer(server, outcome="patch", patch=str(bad), steps=2)
    row = nim_eval.run_edit_case(case, CONFIG, server)
    assert row["wrong_applies"] and row["facts"]["apply_failed"]


def test_run_edit_case_extra_file_is_out_of_scope(tmp_path):
    case, server = _edit_fixture(tmp_path, "two")
    (tmp_path / "code" / "b.txt").write_text("x\n")
    extra = tmp_path / "extra.diff"
    extra.write_text(
        (tmp_path / "ref.diff").read_text()
        + "diff --git a/b.txt b/b.txt\n--- a/b.txt\n+++ b/b.txt\n"
        "@@ -1 +1 @@\n-x\n+y\n"
    )
    _answer(server, outcome="patch", patch=str(extra), steps=2)
    row = nim_eval.run_edit_case(case, CONFIG, server)
    assert row["wrong_applies"] and row["out_of_scope"] == 1


def test_run_edit_case_headerless_reference_still_compares(tmp_path):
    case, server = _edit_fixture(tmp_path, "two")
    (tmp_path / "code" / "b.txt").write_text("x\n")
    (tmp_path / "ref.diff").write_text(
        "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n"
        "--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-x\n+y\n"
    )
    row = nim_eval.run_edit_case(case, CONFIG, server)
    assert row["wrong_applies"]  # NIM's patch misses b.txt


def test_run_edit_case_refuse_case_needs_no_reference(tmp_path):
    case, server = _edit_fixture(tmp_path, "two")
    del case["reference_diff"]
    case["expect"] = "refuse"
    _answer(server, outcome="refused", patch=None, steps=1)
    assert nim_eval.run_edit_case(case, CONFIG, server)["correct"]


def test_run_edit_case_patch_case_without_reference_is_an_error(tmp_path):
    case, server = _edit_fixture(tmp_path, "two")
    del case["reference_diff"]
    row = nim_eval.run_edit_case(case, CONFIG, server)
    assert "reference_diff" in row["error"]


def test_run_edit_case_reference_touching_nothing_is_an_error(tmp_path):
    case, server = _edit_fixture(tmp_path, "two")
    (tmp_path / "ref.diff").write_text("")
    row = nim_eval.run_edit_case(case, CONFIG, server)
    assert "touches no files" in row["error"]


def test_numstat_paths_lists_both_sides_of_a_rename(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    diff = tmp_path / "r.diff"
    diff.write_text(
        "diff --git a/old name.md b/new.md\nsimilarity index 100%\n"
        "rename from old name.md\nrename to new.md\n"
    )
    paths = nim_eval.numstat_paths(diff, tmp_path)
    assert paths == {"old name.md", "new.md"}


@pytest.mark.parametrize(
    ("text", "refused"),
    [
        ("[nim_edit: REFUSED]\n- block 1: denied", True),
        ("[nim_edit: REFUSED after repair round]\n- x", True),
        ("[Error: task is empty.]", True),
        ("[Error: cwd '/x' is not a directory.]", True),
        ("[Error: files is empty; omit it to allow all of cwd]", True),
        ("[Error: invalid files entry: -x]", True),
        ("[Error: invalid must_cover symbol: a b]", True),
        ("[NVIDIA NIM is rate-limited; try again in 30s]", False),
        ("[NVIDIA NIM request timed out after 60s]", False),
        ("[NVIDIA NIM is not configured: set NVIDIA_API_KEY ...]", False),
        ("[Error: must_cover needs the dekko map: crash]", False),
        ("", False),
    ],
)
def test_edit_refused_counts_only_castor_refusals(text, refused):
    assert nim_eval.edit_refused(text) is refused


def test_run_edit_case_failed_nim_run_is_an_error_row(tmp_path):
    case, server = _edit_fixture(tmp_path, "two")
    case["expect"] = "refuse"
    del case["reference_diff"]
    server._run_nim_edit = lambda *a: (
        "[NVIDIA NIM request timed out after 60s]",
        {"outcome": "refused", "patch": None, "steps": 1},
    )
    row = nim_eval.run_edit_case(case, CONFIG, server)
    assert "timed out" in row["error"] and "correct" not in row


def test_rescore_turns_an_old_failed_run_into_an_error_row(tmp_path):
    case = {"name": "e", "role": "edit", "expect": "refuse"}
    saved = tmp_path / "results.json"
    row = {
        "case": "e",
        "config": "a",
        "role": "edit",
        "scoring": 3,
        "answer": "[NVIDIA NIM is rate-limited; try again in 30s]",
        "facts": _facts(
            expect="refuse",
            refused=True,
            trees_equal=None,
            touched_paths=[],
            reference_paths=[],
        ),
        "correct": True,
        "seconds": 1.0,
        "steps": 1,
    }
    refused = {
        **row,
        "case": "r",
        "answer": "[nim_edit: REFUSED]\n- x",
    }
    saved.write_text(json.dumps([row, refused]))
    nim_eval.rescore(saved, [case, {**case, "name": "r"}])
    failed, kept = json.loads(saved.read_text())
    assert "rate-limited" in failed["error"] and "correct" not in failed
    assert kept["correct"] is True and "error" not in kept


def test_must_cover_case_under_dekko_off_needs_dekko(tmp_path, capsys):
    case, server = _edit_fixture(tmp_path, "two")
    case["must_cover"] = ["f"]

    def no_run(*_a):
        raise AssertionError("nim_edit must not run")

    server._run_nim_edit = no_run
    off = {"label": "off", "model": None, "dekko": False}
    row = nim_eval.run_edit_case(case, off, server)
    assert row["needs_dekko"] is True and "error" not in row
    assert "correct" not in row
    patched = {
        "config": "off",
        "role": "edit",
        "case": "p",
        "expect": "patch",
        "correct": True,
        "clean_error": False,
        "wrong_applies": False,
        "out_of_scope": 0,
        "seconds": 5.0,
        "steps": 2,
    }
    line = nim_eval.summarize([row, patched], "off")
    assert "correct 1/1" in line and "needs_dekko 1" in line
    assert "errors 0" in line
    nim_eval.report([row], ["off"])
    assert "e" in capsys.readouterr().out.split("needs dekko")[0]


def test_must_cover_case_runs_with_dekko_on(tmp_path):
    case, server = _edit_fixture(tmp_path, "two")
    case["must_cover"] = ["f"]
    on = {"label": "on", "model": None, "dekko": True}
    server._dekko_ensure_map = lambda _root: True
    assert nim_eval.run_edit_case(case, on, server)["correct"]


def test_rescore_marks_an_old_dekko_off_must_cover_row(tmp_path):
    case = {"name": "e", "role": "edit", "must_cover": ["f"]}
    saved = tmp_path / "results.json"
    row = {
        "case": "e",
        "config": "off",
        "dekko": False,
        "role": "edit",
        "answer": "[Error: must_cover needs the dekko map]",
        "facts": {**_facts(refused=True), "touched_paths": []},
        "clean_error": True,
        "seconds": 1.0,
        "steps": 0,
    }
    row["facts"]["reference_paths"] = []
    saved.write_text(json.dumps([row]))
    nim_eval.rescore(saved, [case])
    (kept,) = json.loads(saved.read_text())
    assert kept["needs_dekko"] is True
    assert "clean_error" not in kept and "error" not in kept


@pytest.fixture
def nim_stub(tmp_path, monkeypatch):
    """The real server against test_nim's loopback NIM stub."""
    import test_nim

    stub = test_nim.Stub()
    monkeypatch.setenv("CASTOR_NIM_BASE_URL", stub.url)
    monkeypatch.setenv("NVIDIA_API_KEY", test_nim.KEY)
    monkeypatch.delenv("NVIDIA_API_KEYS", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("CASTOR_NIM_DEKKO", "0")
    # The eval wraps the parser; undo that for the other tests.
    server = test_nim.server
    monkeypatch.setattr(
        server, "_parse_edit_blocks", server._parse_edit_blocks
    )
    monkeypatch.setattr(server, "_eval_capture", False, raising=False)
    test_nim.server._nim_reset_state()
    yield stub, test_nim
    test_nim.server._nim_reset_state()
    stub.close()


def test_run_edit_case_keeps_the_raw_answers(tmp_path, nim_stub):
    stub, test_nim = nim_stub
    case, _ = _edit_fixture(tmp_path, "two")
    bad = "<<<EDIT a.txt\n>>>NEW\ntwo\n>>>END"
    good = "<<<EDIT a.txt\n<<<OLD\none\n>>>NEW\ntwo\n>>>END"
    for text in (bad, good):
        stub.queue.append((200, {}, test_nim._completion(text)))
    row = nim_eval.run_edit_case(case, CONFIG, test_nim.server)
    assert row["correct"] and row["repair"]
    assert row["raw_answers"] == [bad, good]
    nim_eval.report([row], ["a"])


_BAD = "<<<EDIT a.txt\n>>>NEW\ntwo\n>>>END"
_GOOD = "<<<EDIT a.txt\n<<<OLD\none\n>>>NEW\ntwo\n>>>END"


def test_refuse_case_with_a_429_in_the_repair_run_is_an_error(
    tmp_path, nim_stub, monkeypatch
):
    stub, test_nim = nim_stub
    monkeypatch.setattr(test_nim.server, "NIM_BACKOFF_BASE", 0.01)
    monkeypatch.setattr(test_nim.server, "NIM_BACKOFF_JITTER", 0.0)
    case, _ = _edit_fixture(tmp_path, "two")
    case["expect"] = "refuse"
    del case["reference_diff"]
    stub.queue.append((200, {}, test_nim._completion(_BAD)))
    for _ in range(10):
        stub.queue.append((429, {"Retry-After": "0"}, {"detail": "no"}))
    row = nim_eval.run_edit_case(case, CONFIG, test_nim.server)
    assert "repair run failed" in row["answer"]
    assert "error" in row and "correct" not in row
    assert "clean_error" not in row


def test_patch_case_with_a_stalled_answer_is_an_error(
    tmp_path, nim_stub, monkeypatch
):
    stub, test_nim = nim_stub
    monkeypatch.setenv("CASTOR_NIM_TIMEOUT", "0.2")
    monkeypatch.setattr(test_nim.server, "NIM_AGENT_ANSWER_TIMEOUT", 0.2)
    case, _ = _edit_fixture(tmp_path, "two")
    # The tool step runs out of tokens, so the answer step (no tools) runs
    # and stalls after a complete block: ok text with the cut-off note.
    stub.queue.append((200, {}, test_nim._completion("", finish="length")))
    sse = {"__sse__": [test_nim._delta(_GOOD)], "stall_after": 1}
    stub.queue.append((200, {}, {**sse, "stall": 0.6}))
    row = nim_eval.run_edit_case(case, CONFIG, test_nim.server)
    assert nim_eval.CUT_OFF_NOTE in row["raw_answers"][0]
    assert "error" in row and "correct" not in row
    line = nim_eval.summarize([row], "a")
    assert "no completed runs" in line and "errors 1" in line


def _saved_edit_row(answer, raw, **facts):
    return {
        "case": "e",
        "config": "a",
        "role": "edit",
        "scoring": 4,
        "answer": answer,
        "raw_answers": raw,
        "facts": {
            **_facts(**facts),
            "touched_paths": ["a.txt"],
            "reference_paths": ["a.txt"],
        },
        "seconds": 1.0,
        "steps": 1,
    }


def test_rescore_applies_the_repair_and_stall_rules(tmp_path):
    case = {"name": "e", "role": "edit", "expect": "refuse"}
    repair = _saved_edit_row(
        "[nim_edit: REFUSED after repair round]\n- block 1: old text is "
        "empty\n- repair run failed: [NVIDIA NIM error: rate limited]",
        [_BAD],
        expect="refuse",
        refused=True,
        trees_equal=None,
    )
    stall = _saved_edit_row(
        "[nim_edit: OK, 1 files, 1 blocks, repair round: no]",
        [_GOOD + "\n\n[Note: response cut off after a stall of 0s.]"],
    )
    clean = _saved_edit_row(
        "[nim_edit: REFUSED]\n- x",
        [_BAD],
        expect="refuse",
        refused=True,
        trees_equal=None,
    )
    saved = tmp_path / "results.json"
    saved.write_text(json.dumps([repair, stall, clean]))
    nim_eval.rescore(saved, [case])
    rows = json.loads(saved.read_text())
    assert ["error" in r for r in rows] == [True, True, False]
    assert ["correct" in r for r in rows] == [False, False, True]


def test_summarize_edit_counts_error_rows():
    rows = [
        {"config": "a", "role": "edit", "case": "x", "error": "boom"},
        {
            "config": "a",
            "role": "edit",
            "expect": "patch",
            "correct": True,
            "wrong_applies": False,
            "out_of_scope": 0,
            "seconds": 5.0,
            "steps": 2,
        },
    ]
    line = nim_eval.summarize(rows, "a")
    assert "errors 1" in line and "correct 1/1" in line


def test_build_task_triage_case_returns_task(tmp_path):
    import importlib
    import sys

    sys.path.insert(0, str(ROOT))
    server = importlib.import_module("server")
    (tmp_path / "run.log").write_text("FAIL x\n")
    case = {"_base": str(tmp_path), "role": "triage", "log": "run.log"}
    task, error = nim_eval.build_task(case, tmp_path, server)
    assert error == "" and task is not None and "run.log" in task
    case["log"] = "missing.log"
    task, error = nim_eval.build_task(case, tmp_path, server)
    assert task is None and error.startswith("[Error")


EXPLAIN_ROW_CASE = {
    "name": "r",
    "role": "explain_row",
    "reading": "harness",
    "expected": [
        {
            "id": "site",
            "window": 3,
            "locations": [
                {"file": "Tools/playtest/flow.sh", "lines": [414, 414]},
                {"file": "Tools/playtest/flow.sh", "lines": [88, 88]},
            ],
        }
    ],
}


def _row_answer(code: str, reading: str) -> str:
    return (
        "Row: runstate equal (FAIL)\n"
        f"Verdict code: {code}\n"
        "Precondition: none\n"
        "Trial lines: run.log:4\n"
        f"Reading: {reading}\n"
    )


def test_score_explain_row_hit_within_three_lines():
    answer = _row_answer(
        "Tools/playtest/flow.sh:91 - cat_verdict", "harness - x"
    )
    got = nim_eval.score_explain_row(answer, EXPLAIN_ROW_CASE)
    assert got == {
        "site_hit": True,
        "reading_right": True,
        "reading_wrong": False,
        "error": False,
    }
    far = _row_answer("Tools/playtest/flow.sh:93 - x", "harness - x")
    assert not nim_eval.score_explain_row(far, EXPLAIN_ROW_CASE)["site_hit"]


def test_score_explain_row_wrong_file_is_miss():
    answer = _row_answer("Tools/playtest/other.sh:414 - x", "harness - x")
    got = nim_eval.score_explain_row(answer, EXPLAIN_ROW_CASE)
    assert got["site_hit"] is False and got["error"] is False
    none = _row_answer("not found", "harness - x")
    got = nim_eval.score_explain_row(none, EXPLAIN_ROW_CASE)
    assert got["site_hit"] is False and got["error"] is False


def test_score_explain_row_unclear_is_neither():
    answer = _row_answer("Tools/playtest/flow.sh:414 - x", "unclear - x")
    got = nim_eval.score_explain_row(answer, EXPLAIN_ROW_CASE)
    assert got["reading_right"] is False and got["reading_wrong"] is False
    assert got["site_hit"] is True


def test_score_explain_row_swapped_is_wrong():
    answer = _row_answer("Tools/playtest/flow.sh:414 - x", "game - x")
    got = nim_eval.score_explain_row(answer, EXPLAIN_ROW_CASE)
    assert got["reading_right"] is False and got["reading_wrong"] is True
    err = nim_eval.score_explain_row("[Error: rate limited]", EXPLAIN_ROW_CASE)
    assert err["error"] is True and err["site_hit"] is False


def test_log_file_copied_into_snapshot(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "a.log").write_text("FAIL x\n")
    snap = tmp_path / "snap"
    snap.mkdir()
    case = {
        "_base": str(tmp_path),
        "log_file": "logs/a.log",
        "log": "Library/flow/run.log",
    }
    nim_eval.copy_log_file(case, snap)
    assert (snap / "Library/flow/run.log").read_text() == "FAIL x\n"
    nim_eval.copy_log_file({"_base": str(tmp_path), "log": "x"}, snap)


def _hit(code: str, reading: str = "harness - x") -> dict:
    return nim_eval.score_explain_row(
        _row_answer(code, reading), EXPLAIN_ROW_CASE
    )


def test_explain_row_range_overlap_hit_and_miss():
    assert _hit("Tools/playtest/flow.sh:80-85 - x")["site_hit"]
    assert _hit("Tools/playtest/flow.sh:416-430 - x")["site_hit"]
    assert not _hit("Tools/playtest/flow.sh:92-100 - x")["site_hit"]


def test_explain_row_not_found_with_citation_is_miss():
    got = _hit("not found (see Tools/playtest/flow.sh:414)")
    assert got["site_hit"] is False and got["error"] is False


def test_explain_row_first_citation_counts_and_bare_name_hits():
    assert not _hit("other.sh:1 then Tools/playtest/flow.sh:414")["site_hit"]
    assert _hit("flow.sh:91 - x")["site_hit"]
    assert not _hit("playtest/flow2.sh:91 - x")["site_hit"]


def _rows(
    run: int, site: int, right: int, wrong: int, errors: int = 0
) -> list[dict]:
    return [
        {
            "config": "c",
            "role": "explain_row",
            "run": run,
            "site_hit": i < site,
            "reading_right": i < right,
            "reading_wrong": 12 - i <= wrong,
            "error": i < errors,
        }
        for i in range(12)
    ]


def test_explain_row_summary_per_run_and_gate_pass():
    rows = _rows(1, 10, 8, 1) + _rows(2, 12, 12, 0)
    lines = nim_eval.summarize(rows, "c").splitlines()
    assert (
        "explain_row run 1: site 10/12, reading right 8/12, wrong 1"
        in (lines[0])
    )
    assert "run 2: site 12/12" in lines[1]
    assert lines[2].endswith("explain_row gate: pass")


def test_explain_row_summary_gate_fail_names_bar_and_run():
    rows = _rows(1, 12, 12, 0) + _rows(2, 9, 12, 0)
    last = nim_eval.summarize(rows, "c").splitlines()[-1]
    assert "gate: fail (site 9 < 10 in run 2)" in last
    rows = _rows(1, 12, 12, 2, errors=1)
    last = nim_eval.summarize(rows, "c").splitlines()[-1]
    assert "wrong 2 > 1 in run 1" in last and "errors 1 > 0 in run 1" in last


def test_snapshot_with_log_leaves_base_untouched(tmp_path):
    server = SimpleNamespace(_cache_dir=lambda: tmp_path / "cache")
    fixture = tmp_path / "fx"
    fixture.mkdir()
    (fixture / "a.sh").write_text("check\n")
    (tmp_path / "x.log").write_text("FAIL x\n")
    case = {
        "_base": str(tmp_path),
        "dir": "fx",
        "log_file": "x.log",
        "log": "Library/flow/r.log",
    }
    snap = nim_eval.snapshot_with_log(case, server)
    assert snap != fixture
    assert (snap / "Library/flow/r.log").read_text() == "FAIL x\n"
    assert not (fixture / "Library").exists()
    assert nim_eval.snapshot_with_log(case, server) == snap


def test_summary_keeps_main_lines_beside_explain_row():
    review = {
        "config": "c",
        "role": "review",
        "leads": 2,
        "matched": 1,
        "hit": ["x"],
        "known": 1,
        "must_hit": [],
        "musts": 0,
        "quotes": [0, 0],
        "seconds": 3.0,
        "steps": 2,
        "failed": False,
    }
    text = nim_eval.summarize([review, *_rows(1, 12, 12, 0)], "c")
    lines = text.splitlines()
    assert "precision" in lines[0] and "explain_row" not in lines[0]
    assert "explain_row run 1" in lines[1]
    assert lines[-1].endswith("explain_row gate: pass")


def _explain_server(seen):
    override = contextvars.ContextVar("o", default=None)

    def explain_row(*_a):
        seen["at_run"] = override.get()
        return "[Error: x]"

    def ensure_map(root):
        seen["map"] = root
        return True

    return SimpleNamespace(
        _dekko_override=override,
        _dekko_ensure_map=ensure_map,
        _cache_dir=lambda: seen["cache"],
        nim_explain_row=explain_row,
    )


def _explain_case(tmp_path):
    (tmp_path / "fx").mkdir()
    return {
        "name": "e",
        "role": "explain_row",
        "dir": "fx",
        "_base": str(tmp_path),
        "log": "r.log",
        "row": "x",
        "expected_site": {"file": "a.sh", "lines": [1, 2]},
    }


@pytest.mark.parametrize("dekko", [True, False, None])
def test_explain_row_case_honours_the_dekko_setting(tmp_path, dekko):
    seen = {"cache": tmp_path / "cache"}
    server = _explain_server(seen)
    config = {"label": "c", "model": None}
    if dekko is not None:
        config["dekko"] = dekko
    result = nim_eval.run_explain_row_case(
        _explain_case(tmp_path), config, server
    )
    assert seen["at_run"] is dekko
    assert ("map" in seen) is (dekko is True)
    assert result["dekko"] is dekko
    assert server._dekko_override.get() is None
