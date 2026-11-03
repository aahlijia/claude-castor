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
