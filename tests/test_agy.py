"""Tests for the Antigravity path, with agy replaced by a fake subprocess.

They pin what Castor sends to agy and how it reads the reply. Whether a real
agy still behaves this way is `scripts/check_agy_surface.py` and live testing.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castor._namespace import view  # noqa: E402

server = view()


class FakeAgy:
    """Stands in for `subprocess.run`, recording each agy call."""

    def __init__(self):
        self.calls = []
        self.kwargs = []
        self.stdout = "answer\n"
        self.stderr = ""
        self.code = 0
        self.error = None
        self.by_flag = {}

    def run(self, args, **kwargs):
        self.calls.append(args)
        self.kwargs.append(kwargs)
        if self.error is not None:
            raise self.error
        for flag, stdout in self.by_flag.items():
            if flag in args:
                return subprocess.CompletedProcess(args, 0, stdout, "")
        return subprocess.CompletedProcess(
            args, self.code, self.stdout, self.stderr
        )


@pytest.fixture
def agy(tmp_path, monkeypatch):
    """A fake agy and an empty cache; real git still runs."""
    fake = FakeAgy()
    real_run = subprocess.run

    def run(args, **kwargs):
        if args[:1] == ["git"]:
            return real_run(args, **kwargs)
        return fake.run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return fake


def _result_event(**result):
    """One stream-json line holding agy's terminal result event."""
    return json.dumps({"event": "result", "result": result}) + "\n"


def _collect(job_id):
    """Poll a background job until it finishes."""
    for _ in range(200):
        out = server.gemini_poll(job_id)
        if "still running" not in out:
            return out
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never finished")


def _job_id(started):
    return started.split("job ", 1)[1].split(".", 1)[0]


# --- the command line -------------------------------------------------------


def test_prompt_sits_right_after_print():
    cmd = server._build_agy_cmd("ask", False, None, None, None, False)
    assert cmd[:4] == [
        "agy",
        "--print",
        "ask",
        f"--print-timeout={server.PRINT_TIMEOUT}",
    ]


def test_slash_commands_are_always_disabled():
    cmd = server._build_agy_cmd("ask", False, None, None, None, False)
    assert "--disable-slash-commands" in cmd


def test_cwd_and_extra_dirs_become_add_dir():
    cmd = server._build_agy_cmd("ask", False, "/p", ["/q"], None, False)
    pairs = list(zip(cmd, cmd[1:]))
    assert ("--add-dir", "/p") in pairs
    assert ("--add-dir", "/q") in pairs


def test_trust_wins_over_sandbox():
    cmd = server._build_agy_cmd("ask", True, "/p", None, None, True)
    assert "--dangerously-skip-permissions" in cmd
    assert "--sandbox" not in cmd


def test_sandbox_auto_approves_inside_the_sandbox():
    cmd = server._build_agy_cmd("ask", False, "/p", None, None, True)
    assert "--sandbox" in cmd
    assert "--dangerously-skip-permissions" in cmd


def test_no_tier_requests_no_permissions():
    cmd = server._build_agy_cmd("ask", False, "/p", None, None, False)
    assert "--sandbox" not in cmd
    assert "--dangerously-skip-permissions" not in cmd


def test_json_schema_switches_to_stream_json():
    cmd = server._build_agy_cmd(
        "ask", False, None, None, None, False, json_schema="{}"
    )
    pairs = list(zip(cmd, cmd[1:]))
    assert ("--output-format", "stream-json") in pairs
    assert ("--json-schema", "{}") in pairs


# --- running agy and reading the outcome ------------------------------------


def test_run_closes_stdin_and_sets_cwd(agy, tmp_path):
    assert server._run_agy("q", cwd=str(tmp_path)) == "answer"
    assert agy.kwargs[0]["stdin"] is subprocess.DEVNULL
    assert agy.kwargs[0]["cwd"] == str(tmp_path)


def test_missing_cli_gives_install_hint(agy):
    agy.error = FileNotFoundError()
    out = server._run_agy("q")
    assert out.startswith("[Error: `agy` CLI not found")
    assert server.INSTALL_CMD in out


def test_timeout_is_bracketed(agy):
    agy.error = subprocess.TimeoutExpired("agy", 1)
    assert server._run_agy("q").startswith("[Error: Antigravity timed out")


def test_signed_out_output_gives_auth_hint(agy):
    agy.stdout = "Please sign in to continue"
    out = server._run_agy("q")
    assert out.startswith("[Not signed in to Antigravity")


def test_nonzero_exit_reports_stderr(agy):
    agy.code, agy.stderr = 1, "boom"
    assert server._run_agy("q") == "[Antigravity error]\nboom"


def test_exit_zero_auto_denial_is_explained(agy):
    agy.stdout, agy.stderr = "", "tool auto-denied in print mode"
    assert server._run_agy("q") == server.DENIED_MSG


def test_exit_zero_empty_output_is_an_error(agy):
    agy.stdout = ""
    assert server._run_agy("q") == "[No response from Antigravity]"


def test_stream_json_denied_actions_are_explained(agy):
    agy.stdout = _result_event(response="", denied_actions=[{"tool": "sh"}])
    assert server._run_agy("q", json_schema="{}") == server.DENIED_MSG


# --- dispatch: cache and sessions -------------------------------------------


def test_trust_without_cwd_never_runs(agy):
    assert server._dispatch("q", trust=True).startswith("[Error: cwd")
    assert agy.calls == []


def test_success_is_cached(agy):
    assert server._dispatch("q") == "answer"
    assert server._dispatch("q") == "answer"
    assert len(agy.calls) == 1


def test_bracketed_errors_are_not_cached(agy):
    agy.code, agy.stderr = 1, "boom"
    server._dispatch("q")
    server._dispatch("q")
    assert len(agy.calls) == 2


def test_use_cache_false_always_runs(agy):
    server._dispatch("q", use_cache=False)
    server._dispatch("q", use_cache=False)
    assert len(agy.calls) == 2


def test_trust_runs_are_not_cached(agy, tmp_path):
    server._dispatch("q", trust=True, cwd=str(tmp_path))
    server._dispatch("q", trust=True, cwd=str(tmp_path))
    assert len(agy.calls) == 2


def test_session_replays_earlier_turns(agy, tmp_path):
    server._dispatch("first question", cwd=str(tmp_path), session="s")
    server._dispatch("second question", cwd=str(tmp_path), session="s")
    replayed = agy.calls[1][2]
    assert "first question" in replayed
    assert "second question" in replayed


def test_failed_turn_is_not_appended(agy, tmp_path):
    agy.code, agy.stderr = 1, "boom"
    server._dispatch("q", cwd=str(tmp_path), session="s")
    assert server._load_session(str(tmp_path), "s").get("turns", []) == []


def test_session_compaction_uses_agy_summarizer(agy, tmp_path, monkeypatch):
    seen = []

    def summarize(text):
        seen.append(text)
        return "short summary"

    monkeypatch.setattr(server, "_summarize_text", summarize)
    monkeypatch.setattr(server, "SESSION_BUDGET_CHARS", 10)
    for n in range(server.SESSION_KEEP_VERBATIM + 1):
        server._dispatch(f"question {n}", cwd=str(tmp_path), session="s")
    assert seen
    session = server._load_session(str(tmp_path), "s")
    assert session["summary"] == "short summary"


# --- workflow tools: structured output --------------------------------------


def test_summarize_reads_structured_output(agy, tmp_path):
    agy.stdout = _result_event(
        response="ignored",
        structured_output={"summary": "s", "key_points": ["k"]},
    )
    out = server.gemini_summarize(str(tmp_path), "README.md")
    assert json.loads(out) == {"summary": "s", "key_points": ["k"]}
    assert "--sandbox" in agy.calls[0]
    assert "--json-schema" in agy.calls[0]


def test_summarize_falls_back_to_response_text(agy, tmp_path):
    reply = json.dumps({"summary": "from text", "key_points": []})
    agy.stdout = _result_event(response=f"Here it is:\n{reply}")
    out = server.gemini_summarize(str(tmp_path), "README.md")
    assert json.loads(out)["summary"] == "from text"


def test_semantic_search_unwraps_hits(agy, tmp_path):
    hit = {"path": "a.py", "line": 3, "reason": "auth"}
    agy.stdout = _result_event(
        response="ignored", structured_output={"hits": [hit]}
    )
    out = server.gemini_semantic_search(str(tmp_path), "where is auth")
    assert json.loads(out) == [hit]


def test_workflow_tool_passes_errors_through(agy, tmp_path):
    agy.code, agy.stderr = 1, "boom"
    out = server.gemini_summarize(str(tmp_path), "README.md")
    assert out == "[Antigravity error]\nboom"


# --- background jobs --------------------------------------------------------


def test_start_then_poll_returns_the_answer(agy):
    job = _job_id(server.gemini_start("background q", use_cache=False))
    assert _collect(job) == "answer"


def test_failed_job_is_reported_as_failed(agy):
    agy.code, agy.stderr = 1, "boom"
    job = _job_id(server.gemini_start("background q", use_cache=False))
    out = _collect(job)
    assert out.startswith(f"[Job {job} failed after")
    assert "boom" in out


def test_start_with_trust_needs_cwd(agy):
    assert server.gemini_start("q", trust=True).startswith("[Error: cwd")
    assert agy.calls == []


def test_poll_of_unknown_job_says_so():
    assert server.gemini_poll("nope").startswith("[Unknown job nope")


def test_jobs_lists_a_finished_job(agy):
    job = _job_id(server.gemini_start("listed q", use_cache=False))
    _collect(job)
    assert job in server.gemini_jobs()


# --- sessions and the cache -------------------------------------------------


def test_session_tools_list_show_and_delete(agy, tmp_path):
    cwd = str(tmp_path)
    server._dispatch("q", cwd=cwd, session="work")
    assert server.gemini_sessions(cwd).startswith("work  turns=1")
    assert json.loads(server.gemini_session_show(cwd, "work"))["turns"]
    assert (
        server.gemini_session_delete(cwd, "work") == "Deleted session 'work'."
    )
    assert server.gemini_sessions(cwd) == "[No sessions for this project.]"


def test_reset_clears_the_default_session(agy, tmp_path):
    cwd = str(tmp_path)
    server._dispatch("q", cwd=cwd, session=server.DEFAULT_SESSION)
    assert server.gemini_reset(cwd).startswith("Default session cleared")
    assert server.gemini_reset(cwd).startswith("No default session")


def test_reset_needs_cwd():
    assert server.gemini_reset().startswith("[Error: cwd is required")


def test_cache_clear_keeps_run_records(agy):
    server._dispatch("q")
    runs = server._cache_dir() / "runs"
    runs.mkdir(parents=True)
    record = runs / "run-1.json"
    record.write_text("{}")
    assert "Cleared 1 cached" in server.gemini_cache_clear()
    assert record.exists()
    server._dispatch("q")
    assert len(agy.calls) == 2


# --- status -----------------------------------------------------------------


def test_status_when_agy_is_missing(agy):
    agy.error = FileNotFoundError()
    assert server.gemini_status().startswith("NOT INSTALLED")


def test_status_when_signed_out(agy):
    agy.stdout = "not signed in"
    assert server.gemini_status().startswith("NOT SIGNED IN")


def test_status_ready_includes_the_nim_line(agy, monkeypatch):
    agy.by_flag = {"--version": "1.4.12\n"}
    monkeypatch.setattr(server, "_nim_api_keys", lambda: ["key"])
    out = server.gemini_status()
    assert out.startswith("READY")
    assert "1.4.12" in out
    assert "nvidia_nim: key set" in out
