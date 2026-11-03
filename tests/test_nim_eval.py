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


def _fake_server(seen: dict):
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


def test_unquote_path_handles_c_style_quoting():
    unquote = nim_eval.unquote_path
    assert unquote("plain.py") == "plain.py"
    assert unquote('"my notes.md"') == "my notes.md"
    assert unquote('"a\\tb\\"c\\\\d"') == 'a\tb"c\\d'
    assert unquote('"caf\\303\\251.md"') == "café.md"


def test_diff_paths_reads_plain_spaced_and_quoted_headers():
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "diff --git a/docs/my notes.md b/docs/my notes.md\n"
        'diff --git "a/caf\\303\\251.md" "b/caf\\303\\251.md"\n'
    )
    assert nim_eval.diff_paths(diff) == {"x.py", "docs/my notes.md", "café.md"}


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
