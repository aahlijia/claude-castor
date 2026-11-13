"""Tests for scripts/usage_audit.py (local files only)."""

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "usage_audit", ROOT / "scripts" / "usage_audit.py"
)
assert _spec is not None and _spec.loader is not None
usage_audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(usage_audit)

SCOUT = "mcp__claude-castor__nim_scout"


def _turn(mid, uses=(), **usage):
    """An assistant line: one message id, its usage, its tool calls."""
    content = [
        {"type": "tool_use", "id": use_id, "name": name, "input": args}
        for use_id, name, args in uses
    ]
    return {
        "type": "assistant",
        "timestamp": "2026-11-13T10:00:00.000Z",
        "message": {"id": mid, "usage": usage, "content": content},
    }


def _result(use_id, content, at="2026-11-13T10:00:02.000Z"):
    block = {"type": "tool_result", "tool_use_id": use_id, "content": content}
    return {"type": "user", "timestamp": at, "message": {"content": [block]}}


def _write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


@pytest.fixture
def projects(tmp_path):
    """One session with two subagents: one edits, one only reads."""
    root = tmp_path / "projects"
    _write(
        root / "proj-a" / "s1.jsonl",
        [
            _turn(
                "m1",
                [("t1", "Bash", {"command": "cd /x && sed -n '1,9p' f.py"})],
                cache_read_input_tokens=1000,
                output_tokens=10,
            ),
            # The same message again: a second content block, same usage.
            _turn(
                "m1",
                [("t2", SCOUT, {"question": "where?"})],
                cache_read_input_tokens=1000,
                output_tokens=10,
            ),
            _result("t1", "x" * 400),
            _result(
                "t2",
                [{"type": "text", "text": "y" * 60}],
                at="2026-11-13T10:00:40.000Z",
            ),
            {"type": "summary", "summary": "not a message"},
            "not even an object",
        ],
    )
    sub = root / "proj-a" / "s1" / "subagents"
    _write(
        sub / "agent-edits.jsonl",
        [
            _turn(
                "e1",
                [("t3", "Edit", {"file_path": "f.py"})],
                cache_creation_input_tokens=100,
            ),
            _result("t3", "ok"),
        ],
    )
    _write(
        sub / "agent-reads.jsonl",
        [
            _turn("r1", [("t4", "Read", {"file_path": "f.py"})]),
            _result("t4", "z" * 50),
            _turn("r2", input_tokens=7),
        ],
    )
    return root


def _audit(root, **kwargs):
    return usage_audit.audit(usage_audit.transcripts(root, **kwargs))


def test_usage_is_counted_once_per_message(projects):
    main = _audit(projects).main
    assert main.transcripts == 1 and main.turns == 1
    assert main.tokens["cache_read_input_tokens"] == 1000
    assert main.tokens["output_tokens"] == 10


def test_subagent_transcripts_are_kept_apart(projects):
    sub = _audit(projects).sub
    assert sub.transcripts == 2 and sub.turns == 3
    assert sub.calls == {"Edit": 1, "Read": 1}
    assert sub.tokens["cache_creation_input_tokens"] == 100


def test_tool_results_are_sized_and_timed(projects):
    main = _audit(projects).main
    assert main.calls == {"Bash": 1, "castor:nim_scout": 1}
    assert main.chars == {"Bash": 400, "castor:nim_scout": 60}
    assert main.waits["castor:nim_scout"] == [40.0]


def test_bash_output_is_filed_under_the_first_command(projects):
    report = _audit(projects)
    assert report.bash_calls == {"sed": 1} and report.bash_chars["sed"] == 400


@pytest.mark.parametrize(
    "command, head",
    [
        ("sed -n '1,9p' f.py", "sed"),
        ("cd /repo && grep -n x f.py | head", "grep"),
        ("FOO=1 BAR=2 /usr/bin/cat f.py", "cat"),
        ("git -C /repo diff --stat", "git diff"),
        ("uv run --with pytest --with httpx pytest tests -q", "uv run pytest"),
        ("python3 scripts/nim_eval.py cases.json", "python3 nim_eval.py"),
        ("", "?"),
    ],
)
def test_command_head(command, head):
    assert usage_audit.command_head(command) == head


@pytest.mark.parametrize(
    "name, short",
    [
        (SCOUT, "castor:nim_scout"),
        ("mcp__plugin_dekko_dekko__outline", "mcp:plugin_dekko_dekko"),
        ("Read", "Read"),
    ],
)
def test_tool_name(name, short):
    assert usage_audit.tool_name(name) == short


def test_weight_uses_price_ratios():
    usage = {
        "input_tokens": 10,
        "output_tokens": 10,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 100,
    }
    assert usage_audit.weight(usage) == 10 + 50 + 10 + 125


def test_subagents_split_by_whether_they_edit(projects):
    report = _audit(projects)
    assert (report.editing.count, report.editing.turns) == (1, 1)
    assert (report.reading.count, report.reading.turns) == (1, 2)
    assert report.editing.weight == 125 and report.reading.weight == 7


def test_transcripts_filter_by_age_and_project(projects):
    _write(projects / "proj-b" / "old.jsonl", [_turn("o1")])
    old = time.time() - 40 * 86400
    os.utime(projects / "proj-b" / "old.jsonl", (old, old))
    recent = usage_audit.transcripts(projects, days=30)
    assert all(path.parent.name != "proj-b" for path in recent)
    assert len(list(usage_audit.transcripts(projects, days=60))) == 4
    only_b = usage_audit.transcripts(projects, days=60, project="proj-b")
    assert [path.name for path in only_b] == ["old.jsonl"]


def test_report_prints_counts_and_no_arguments(projects):
    text = usage_audit.render(_audit(projects), days=30)
    assert "1 sessions, 2 subagent transcripts" in text
    assert "castor:nim_scout" in text and "sed" in text
    assert "never edit" in text
    assert "f.py" not in text and "where?" not in text


def test_main_prints_the_report(projects, capsys):
    usage_audit.main(["--root", str(projects), "--days", "30"])
    assert "1 sessions, 2 subagent transcripts" in capsys.readouterr().out


def test_an_empty_root_reports_nothing_to_read(tmp_path, capsys):
    usage_audit.main(["--root", str(tmp_path)])
    assert "No transcripts" in capsys.readouterr().out
