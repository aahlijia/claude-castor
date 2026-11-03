"""Tests for the scoring in scripts/nim_eval.py (no network)."""

import importlib.util
from pathlib import Path

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
