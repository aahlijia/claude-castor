"""Tests for the run tier (castor_run_start and friends) and run compare.

Projects are tmp_path folders with tiny bash scripts; RUN_MINUTE_S and
RUN_POLL_S are patched so minutes become milliseconds. Every process a
test starts is ended by the test itself: Castor never kills one.
"""

import importlib
import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
server = importlib.import_module("server")


def _script(path: Path, body: str) -> Path:
    """Write an executable bash script."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)
    return path


def _write_config(root: Path, config: dict) -> None:
    (root / ".castor").mkdir(exist_ok=True)
    (root / ".castor" / "run.json").write_text(json.dumps(config))


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project allowed to run t/*.sh, with t/ok.sh."""
    root = tmp_path / "proj"
    root.mkdir()
    _write_config(root, {"allow": ["t/*.sh"]})
    _script(root / "t" / "ok.sh", "echo ok\n")
    monkeypatch.setenv("CASTOR_RUN_ROOTS", str(root))
    monkeypatch.delenv("CASTOR_RUN", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return root


# --- Task 1: run config and command check -----------------------------------


def test_run_disabled_without_roots(tmp_path, monkeypatch):
    monkeypatch.delenv("CASTOR_RUN_ROOTS", raising=False)
    monkeypatch.delenv("CASTOR_RUN", raising=False)
    assert "CASTOR_RUN_ROOTS" in server._run_enabled_for(tmp_path)


def test_run_root_must_be_listed(tmp_path, monkeypatch):
    monkeypatch.delenv("CASTOR_RUN", raising=False)
    monkeypatch.setenv("CASTOR_RUN_ROOTS", str(tmp_path / "other"))
    assert server._run_enabled_for(tmp_path) is not None
    monkeypatch.setenv("CASTOR_RUN_ROOTS", f"/nope:{tmp_path}")
    assert server._run_enabled_for(tmp_path / "lane") is None


def test_run_off_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("CASTOR_RUN_ROOTS", str(tmp_path))
    monkeypatch.setenv("CASTOR_RUN", "0")
    assert "CASTOR_RUN=0" in server._run_enabled_for(tmp_path)


def test_run_refused_on_windows(tmp_path, monkeypatch):
    monkeypatch.setenv("CASTOR_RUN_ROOTS", str(tmp_path))
    monkeypatch.delenv("CASTOR_RUN", raising=False)
    monkeypatch.setattr(server.sys, "platform", "win32")
    assert "POSIX" in server._run_enabled_for(tmp_path)


def test_run_config_missing_and_defaults(project):
    (project / ".castor" / "run.json").unlink()
    cfg, error = server._run_config(project)
    assert cfg is None and ".castor/run.json" in error
    _write_config(project, {"allow": ["t/*.sh"]})
    cfg, error = server._run_config(project)
    assert error is None
    assert cfg.allow == ("t/*.sh",)
    assert cfg.env_file is None
    assert cfg.log_line.pattern == "^log: (.+)$"
    assert cfg.overdue_command is None
    assert cfg.grace_minutes == 10
    assert cfg.max_hours == 8
    assert cfg.max_running == 4
    assert cfg.flags == ()


def test_run_config_reads_flags_and_overdue(project):
    _script(project / "t" / "dialog.sh", "echo dialog: none\n")
    _write_config(
        project,
        {
            "allow": ["t/*.sh"],
            "env_file": "lane.env",
            "overdue_command": ["t/dialog.sh", "--quiet"],
            "flags": [
                {"name": "restarted", "pattern": "^RESTARTED "},
                {"name": "refused", "pattern": "REFUSED", "stop": True},
            ],
        },
    )
    cfg, error = server._run_config(project)
    assert error is None
    assert cfg.env_file == "lane.env"
    assert cfg.overdue_command == ("t/dialog.sh", "--quiet")
    assert [(f.name, f.stop) for f in cfg.flags] == [
        ("restarted", False),
        ("refused", True),
    ]
    assert cfg.flags[0].pattern.search("RESTARTED lane A")


@pytest.mark.parametrize(
    ("config", "named"),
    [
        ({"allow": ["t/*.sh"], "alow": []}, "alow"),
        ({"allow": ["t/*.sh"], "log_line": "("}, "log_line"),
        (
            {"allow": ["t/*.sh"], "flags": [{"name": "x", "pattern": "["}]},
            "flags",
        ),
        ({"allow": []}, "allow"),
        ({"allow": ["t/*.sh"], "max_running": 0}, "max_running"),
        ({"allow": ["t/*.sh"], "max_hours": -1}, "max_hours"),
        ({"allow": ["t/*.sh"], "overdue_command": ["../x.sh"]}, "overdue"),
        ({"allow": ["t/*.sh"], "flags": [{"pattern": "x"}]}, "flags"),
    ],
)
def test_run_config_rejects_bad_values(project, config, named):
    _write_config(project, config)
    cfg, error = server._run_config(project)
    assert cfg is None and named in error


def test_run_config_rejects_bad_json(project):
    (project / ".castor" / "run.json").write_text("{nope")
    cfg, error = server._run_config(project)
    assert cfg is None and "JSON" in error


def test_run_resolves_allowed_command(project):
    cfg, _ = server._run_config(project)
    argv, error = server._run_resolve_command(
        project, cfg, ["t/ok.sh", "mansion", "--fast"]
    )
    assert error is None
    assert argv == [
        str((project / "t" / "ok.sh").resolve()),
        "mansion",
        "--fast",
    ]


def test_run_refuses_symlink_and_dotdot_escape(project, tmp_path):
    cfg, _ = server._run_config(project)
    evil = _script(tmp_path / "evil.sh", "touch pwned\n")
    (project / "t" / "link.sh").symlink_to(evil)
    for command in (
        ["t/../../evil.sh"],
        ["t/link.sh"],
        [str(evil)],
    ):
        argv, error = server._run_resolve_command(project, cfg, command)
        assert argv is None and "outside" in error, command


def test_run_refuses_unlisted_nonexec_and_missing(project):
    cfg, _ = server._run_config(project)
    _script(project / "u" / "other.sh", "echo\n")
    (project / "t" / "plain.sh").write_text("echo\n")
    (project / "t" / "dir.sh").mkdir()
    cases = {
        "u/other.sh": "allow",
        "t/plain.sh": "executable",
        "t/missing.sh": "not a file",
        "t/dir.sh": "not a file",
    }
    for command, reason in cases.items():
        argv, error = server._run_resolve_command(project, cfg, [command])
        assert argv is None and reason in error, command
    argv, error = server._run_resolve_command(project, cfg, [])
    assert argv is None and "empty" in error


# --- Task 2: run records -----------------------------------------------------


def _row(status: str, name: str, detail: str) -> str:
    """A verdict.sh line: `%-13s %-34s %s`."""
    return f"{status:<13} {name:<34} {detail}"


TABLE = "\n".join(
    [
        "  trial 1: ok",
        _row("PASS", "tops: a", "3 of 3"),
        "@case 1700000000 PASS tops: a",
        "  trial 1: off",
        _row("FAIL", "tops: b", "1 of 3"),
        "@case 1700000001 FAIL tops: b",
        "--- verdicts ---",
        _row("PASS", "tops: a", "3 of 3"),
        _row("FAIL", "tops: b", "1 of 3"),
        "total 2, fail 1, not assessed 0",
        "",
    ]
)


@pytest.fixture
def fast(monkeypatch):
    """Minutes become milliseconds and the monitor ticks quickly."""
    monkeypatch.setattr(server, "RUN_MINUTE_S", 0.001)
    monkeypatch.setattr(server, "RUN_POLL_S", 0.02)


@pytest.fixture
def sleeper():
    """A long-lived child process; killed and reaped at teardown."""
    proc = subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL)
    yield proc
    proc.kill()
    proc.wait()


def _new_record(project: Path, **over: object) -> dict:
    cfg, _ = server._run_config(project)
    rec = server._run_new_record(project, cfg, ["t/ok.sh"], "ok", 5, False)
    rec.update(over)
    return rec


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"], stdin=subprocess.DEVNULL)
    proc.wait()
    return proc.pid


def _join_monitor(run_id: str) -> None:
    thread = server._run_monitors.get(run_id)
    if thread is not None:
        thread.join(timeout=5)


def _poll_json(run_id: str, timeout: float = 5.0) -> dict:
    """Poll a run until it is not running; the final object."""
    end = time.monotonic() + timeout
    while True:
        out = json.loads(server.gemini_poll(run_id))
        if out["status"] != "running" or time.monotonic() > end:
            return out
        time.sleep(0.02)


def test_run_record_round_trip(project):
    rec = _new_record(project, pid=12, pid_start="x")
    assert server._RUN_ID_RE.match(rec["run"])
    assert rec["status"] == "running" and rec["result"] is None
    assert rec["root"] == str(project.resolve())
    server._run_record_write(rec)
    assert server._run_record_read(rec["run"]) == rec
    assert Path(rec["capture"]).parent == server._runs_dir()


def test_run_record_read_refuses_odd_ids(project):
    assert server._run_record_read("run-../../etc") is None
    assert server._run_record_read("run-deadbeef") is None
    assert server.gemini_poll("run-deadbeef") == "[Unknown run run-deadbeef]"


def test_run_records_filter_by_root_newest_first(project, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    old = _new_record(project, started_at=100.0)
    new = _new_record(project, started_at=200.0)
    elsewhere = _new_record(project, started_at=300.0, root=str(other))
    for rec in (old, new, elsewhere):
        server._run_record_write(rec)
    ids = [r["run"] for r in server._run_records(project)]
    assert ids == [new["run"], old["run"]]
    every = [r["run"] for r in server._run_records(None)]
    assert every == [elsewhere["run"], new["run"], old["run"]]


def test_run_records_prune_old(project):
    stale = _new_record(project)
    server._run_record_write(stale)
    Path(stale["capture"]).write_text("old\n")
    month = time.time() - 31 * 86400
    for path in (Path(stale["capture"]), server._run_record_path(stale)):
        os.utime(path, (month, month))
    server._run_record_write(_new_record(project))
    assert server._run_record_read(stale["run"]) is None
    assert not Path(stale["capture"]).exists()


def test_pid_start_gone_is_none():
    assert server._pid_start(_dead_pid()) is None


def test_run_alive_requires_matching_start(sleeper):
    start = server._pid_start(sleeper.pid)
    assert start
    assert server._run_alive({"pid": sleeper.pid, "pid_start": start})
    assert not server._run_alive({"pid": sleeper.pid, "pid_start": "x"})
    assert not server._run_alive({"pid": None, "pid_start": start})


def test_poll_after_restart_reattaches(project, fast, sleeper):
    rec = _new_record(
        project, pid=sleeper.pid, pid_start=server._pid_start(sleeper.pid)
    )
    rec["config"]["max_hours"] = 1000  # 60 s under `fast`
    server._run_record_write(rec)
    Path(rec["capture"]).write_text("  trial 1: going\n")
    out = json.loads(server.gemini_poll(rec["run"]))
    try:
        assert out["status"] == "running" and out["pid"] == sleeper.pid
        assert set(out) == {
            "run",
            "status",
            "elapsed_s",
            "pid",
            "flags",
            "overdue",
        }
        assert server._run_monitors[rec["run"]].is_alive()
    finally:
        sleeper.kill()
        sleeper.wait()
        _join_monitor(rec["run"])
    assert _poll_json(rec["run"])["status"] == "lost"


def test_poll_after_restart_detects_pid_reuse(project, fast, sleeper):
    rec = _new_record(project, pid=sleeper.pid, pid_start="Mon Jan  1 1999")
    server._run_record_write(rec)
    Path(rec["capture"]).write_text(TABLE)
    out = _poll_json(rec["run"])
    assert out["status"] == "done" and out["exit_code"] is None
    assert out["verdict"] == "failed"
    assert out["rows"] == [
        {
            "row": "tops: b",
            "verdict": "FAIL",
            "detail": "1 of 3",
            "log_line": 5,
        }
    ]
    assert out["totals"] == {"total": 2, "fail": 1, "not_assessed": 0}
    assert sleeper.poll() is None


def test_poll_after_restart_lost(project, fast):
    rec = _new_record(project, pid=_dead_pid(), pid_start="x")
    server._run_record_write(rec)
    Path(rec["capture"]).write_text("")
    out = _poll_json(rec["run"])
    assert out["status"] == "lost" and out["exit_code"] is None
    assert out["verdict"] is None and out["rows"] == []


# --- Task 3: launcher and monitor -------------------------------------------


@pytest.fixture
def started():
    """Runs a test starts; each process group is ended at teardown."""
    recs: list[dict] = []
    yield recs
    for rec in recs:
        try:
            os.killpg(rec["pid"], signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        _join_monitor(rec["run"])
        try:
            os.waitpid(rec["pid"], os.WNOHANG)
        except ChildProcessError:
            pass


def _start(
    project: Path,
    started: list,
    command: list[str],
    expect: int = 5,
    triage: bool = False,
) -> dict:
    cfg, error = server._run_config(project)
    assert error is None, error
    argv, error = server._run_resolve_command(project, cfg, command)
    assert error is None, error
    rec, error = server._run_begin(
        project, cfg, argv, command, "t", expect, triage
    )
    assert error is None, error
    started.append(rec)
    return rec


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_run_args_reach_script_literally(project, fast, started):
    _script(project / "t" / "args.sh", "printf '%s\\n' \"$@\" > args.txt\n")
    args = ["; touch pwned", "$(id)", "a'b"]
    rec = _start(project, started, ["t/args.sh", *args])
    assert _poll_json(rec["run"])["status"] == "done"
    assert (project / "args.txt").read_text().splitlines() == args
    assert not (project / "pwned").exists()


def test_run_sources_env_file_and_scrubs_secrets(
    project, fast, started, monkeypatch
):
    (project / "lane.env").write_text("export LANE=b\n")
    _write_config(project, {"allow": ["t/*.sh"], "env_file": "lane.env"})
    _script(
        project / "t" / "env.sh",
        'echo "$LANE ${NVIDIA_API_KEY:-none} ${CASTOR_RUN_ROOTS:-none}"\n',
    )
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-secret")
    rec = _start(project, started, ["t/env.sh"])
    _poll_json(rec["run"])
    assert Path(rec["capture"]).read_text() == "b none none\n"


def test_run_refuses_env_file_outside_project(project, tmp_path):
    (tmp_path / "out.env").write_text("export X=1\n")
    _write_config(
        project, {"allow": ["t/*.sh"], "env_file": str(tmp_path / "out.env")}
    )
    cfg, _ = server._run_config(project)
    argv, _ = server._run_resolve_command(project, cfg, ["t/ok.sh"])
    rec, error = server._run_begin(
        project, cfg, argv, ["t/ok.sh"], "t", 5, False
    )
    assert rec is None and "env_file" in error


def test_run_own_session(project, fast, started):
    _script(project / "t" / "sleep.sh", "sleep 30\n")
    rec = _start(project, started, ["t/sleep.sh"])
    assert os.getsid(rec["pid"]) == rec["pid"]


def test_run_result_reads_table_log_and_exit(project, fast, started):
    log = "\n".join(
        [
            "header line",
            "  trial 1: off",
            _row("FAIL", "tops: b", "1 of 3"),
            "@case 1700000001 FAIL tops: b",
            "total 1, fail 1, not assessed 0",
            "",
        ]
    )
    (project / "full.log").write_text(log)
    fail_row = _row("FAIL", "tops: b", "1 of 3")
    _script(
        project / "t" / "suite.sh",
        "mkdir -p Library/s && cp full.log Library/s/x.log\n"
        f"printf '%s\\n' '--- verdicts ---' '{fail_row}'"
        " 'total 1, fail 1, not assessed 0' 'log: Library/s/x.log'\n"
        "exit 1\n",
    )
    rec = _start(project, started, ["t/suite.sh"])
    out = _poll_json(rec["run"])
    assert out["status"] == "done" and out["exit_code"] == 1
    assert out["verdict"] == "failed"
    assert out["log"] == "Library/s/x.log"
    assert out["rows"] == [
        {
            "row": "tops: b",
            "verdict": "FAIL",
            "detail": "1 of 3",
            "log_line": 3,
        }
    ]
    assert out["table"] == "\n".join(
        [
            "--- verdicts ---",
            _row("FAIL", "tops: b", "1 of 3"),
            "total 1, fail 1, not assessed 0",
        ]
    )
    assert out["command"] == ["t/suite.sh"] and out["triage"] is None


def test_monitor_matches_flags_on_whole_lines(project, fast, started):
    _write_config(
        project,
        {
            "allow": ["t/*.sh"],
            "flags": [{"name": "editor_restarted", "pattern": "^RESTA"}],
        },
    )
    _script(
        project / "t" / "flag.sh",
        "printf 'RESTA'; sleep 0.2; printf 'RTED lane A age 26 h\\n'\n",
    )
    rec = _start(project, started, ["t/flag.sh"])
    out = _poll_json(rec["run"])
    assert out["flags"] == [
        {"name": "editor_restarted", "line": "RESTARTED lane A age 26 h"}
    ]


def test_stop_flag_stops_watching(project, fast, started):
    _write_config(
        project,
        {
            "allow": ["t/*.sh"],
            "flags": [
                {
                    "name": "refused",
                    "pattern": "^start\\.sh: REFUSED",
                    "stop": True,
                }
            ],
        },
    )
    _script(
        project / "t" / "refuse.sh",
        "echo 'start.sh: REFUSED editor age 30 h'\nsleep 30\n",
    )
    rec = _start(project, started, ["t/refuse.sh"])
    out = _poll_json(rec["run"])
    assert out["status"] == "stopped" and out["pid"] == rec["pid"]
    assert out["flags"][0]["name"] == "refused"
    assert _alive(rec["pid"])


def test_overdue_runs_command_once_and_keeps_watching(project, fast, started):
    _script(
        project / "t" / "dialog.sh",
        "echo x >> count.txt\necho 'dialog: none'\n",
    )
    _write_config(
        project,
        {
            "allow": ["t/*.sh"],
            "overdue_command": ["t/dialog.sh"],
            "grace_minutes": 0.001,
            "max_hours": 1000,
        },
    )
    _script(project / "t" / "slow.sh", "sleep 1\necho finished\n")
    rec = _start(project, started, ["t/slow.sh"], expect=1)
    end = time.monotonic() + 5
    seen = None
    while time.monotonic() < end:
        out = json.loads(server.gemini_poll(rec["run"]))
        if out["status"] == "running" and out["overdue"]:
            seen = out["overdue"]
            break
        time.sleep(0.02)
    assert seen is not None
    assert seen["output"] == "dialog: none\n" and seen["exit_code"] == 0
    final = _poll_json(rec["run"])
    assert final["status"] == "done" and final["overdue"] == seen
    assert (project / "count.txt").read_text() == "x\n"


def test_overdue_skipped_once_the_table_is_out(project, fast, started):
    _script(project / "t" / "dialog.sh", "echo x >> count.txt\n")
    _write_config(
        project,
        {
            "allow": ["t/*.sh"],
            "overdue_command": ["t/dialog.sh"],
            "grace_minutes": 0.001,
        },
    )
    _script(
        project / "t" / "tail.sh",
        "echo 'total 1, fail 0, not assessed 0'\nsleep 1\n",
    )
    rec = _start(project, started, ["t/tail.sh"], expect=400)
    assert _poll_json(rec["run"])["overdue"] is None
    assert not (project / "count.txt").exists()


def test_monitor_gives_up_without_killing(project, fast, started):
    _write_config(project, {"allow": ["t/*.sh"], "max_hours": 0.00001})
    _script(project / "t" / "sleep.sh", "sleep 30\n")
    rec = _start(project, started, ["t/sleep.sh"])
    out = _poll_json(rec["run"])
    assert out["status"] == "gave_up" and out["pid"] == rec["pid"]
    assert _alive(rec["pid"])


def test_triage_runs_only_on_fail_or_not_assessed(
    project, fast, started, monkeypatch
):
    calls = []
    reply = {"value": json.dumps({"verdict": "failed", "rows": []})}

    def fake(task, cwd, model, max_steps, label, log):
        calls.append((label, str(log)))
        return reply["value"]

    monkeypatch.setattr(server, "_dispatch_triage_json", fake)
    for name, status in (("pass", "PASS"), ("fail", "FAIL")):
        (project / f"{name}.log").write_text(
            f"{_row(status, 'tops: b', '1 of 3')}\n"
            f"@case 1700000001 {status} tops: b\n"
        )
        _script(
            project / "t" / f"{name}.sh",
            f"echo 'log: {name}.log'\n",
        )
    passed = _poll_json(
        _start(project, started, ["t/pass.sh"], triage=True)["run"]
    )
    assert passed["triage"] is None and calls == []
    failed = _poll_json(
        _start(project, started, ["t/fail.sh"], triage=True)["run"]
    )
    assert calls == [("fail.log", str((project / "fail.log").resolve()))]
    assert failed["triage"] == {"verdict": "failed", "rows": []}
    quiet = _poll_json(_start(project, started, ["t/fail.sh"])["run"])
    assert quiet["triage"] is None and len(calls) == 1
    reply["value"] = json.dumps(
        {"error": "rate_limited", "retry_after_s": 5, "message": "x"}
    )
    limited = _poll_json(
        _start(project, started, ["t/fail.sh"], triage=True)["run"]
    )
    assert limited["triage"] == {
        "error": "rate_limited",
        "retry_after_s": 5,
        "message": "x",
    }


def test_attach_after_finish_leaves_the_result(project, fast, started):
    rec = _start(project, started, ["t/ok.sh"])
    first = _poll_json(rec["run"])
    _join_monitor(rec["run"])
    thread = server._run_attach(rec["run"])
    thread.join(timeout=5)
    assert json.loads(server.gemini_poll(rec["run"])) == first


# --- Task 4: run tools -----------------------------------------------------

_STARTED_RE = re.compile(
    r"^Run (run-[0-9a-f]{8}) started: (.+); collect it with "
    r'gemini_poll\("run-[0-9a-f]{8}"\)\.$'
)


def _tool_start(project: Path, started: list, command: list[str]) -> str:
    """castor_run_start; the run id. Its process is ended at teardown."""
    out = server.castor_run_start(str(project), command, 5)
    match = _STARTED_RE.match(out)
    assert match, out
    started.append(server._run_record_read(match.group(1)))
    return match.group(1)


def test_run_tools_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"castor_run_start", "castor_runs"} <= names


def test_run_start_and_poll_end_to_end(project, fast, started):
    out = server.castor_run_start(str(project), ["t/ok.sh", "a b"], 5)
    match = _STARTED_RE.match(out)
    assert match and match.group(2) == "ok.sh a b"
    started.append(server._run_record_read(match.group(1)))
    final = _poll_json(match.group(1))
    assert final["status"] == "done" and final["exit_code"] == 0
    assert final["verdict"] == "passed"
    assert final["command"] == ["t/ok.sh", "a b"]


def test_run_start_takes_a_label(project, fast, started):
    out = server.castor_run_start(
        str(project), ["t/ok.sh"], 5, triage=False, label="lane A tops"
    )
    assert "started: lane A tops;" in out
    started.append(server._run_record_read(_STARTED_RE.match(out).group(1)))


def test_run_start_refusals(project, monkeypatch, tmp_path):
    def start(command: list, expect: int = 5, cwd: str = str(project)) -> str:
        return server.castor_run_start(cwd, command, expect)

    assert "empty" in start([])
    assert "expect_minutes" in start(["t/ok.sh"], 0)
    assert "allow" in start(["u/x.sh"]) or "not a file" in start(["u/x.sh"])
    assert "not a directory" in start(["t/ok.sh"], cwd=str(tmp_path / "no"))
    assert "absolute" in start(["t/ok.sh"], cwd="proj")
    (project / ".castor" / "run.json").unlink()
    assert ".castor/run.json" in start(["t/ok.sh"])
    monkeypatch.delenv("CASTOR_RUN_ROOTS")
    out = start(["t/ok.sh"])
    assert out.startswith("[Error:") and "CASTOR_RUN_ROOTS" in out


def test_run_start_respects_max_running(project, fast, started):
    _write_config(
        project, {"allow": ["t/*.sh"], "max_running": 1, "max_hours": 1000}
    )
    _script(project / "t" / "sleep.sh", "sleep 30\n")
    first = _tool_start(project, started, ["t/sleep.sh"])
    out = server.castor_run_start(str(project), ["t/sleep.sh"], 5)
    assert out.startswith("[Error:") and first in out
    assert "max_running" in out


def test_castor_runs_lists_newest_first(project, fast, started):
    assert server.castor_runs(str(project)) == "No runs on record."
    first = _tool_start(project, started, ["t/ok.sh"])
    _poll_json(first)
    time.sleep(0.01)
    second = _tool_start(project, started, ["t/ok.sh", "x"])
    _poll_json(second)
    lines = server.castor_runs(str(project)).splitlines()
    assert [ln.split()[0] for ln in lines] == [second, first]
    assert lines[0].split()[1] == "done"
    assert lines[0].endswith(" passed")
    assert "ok.sh x" in lines[0]
    assert server.castor_runs().splitlines()[0].startswith(second)


# --- Task 5: compare parser and classes -------------------------------------


def _log(*rows: tuple) -> list[str]:
    """A stamped suite log: rows of (status, name, detail[, trials])."""
    lines: list[str] = []
    for i, row in enumerate(rows):
        status, name, detail = row[:3]
        lines += list(row[3]) if len(row) > 3 else [f"  trial: {name}"]
        lines.append(_row(status, name, detail))
        lines.append(
            f"@case {1700000000 + i} {status.replace(' ', '_')} {name}"
        )
    return lines


def _classes(*statuses: str, same_code: bool = False) -> str:
    runs = [
        (f"run{i + 1}", _log((st, "tops: x", "1 of 3")))
        for i, st in enumerate(statuses)
    ]
    return server._compare_rows(runs, same_code=same_code)[0].klass


def test_compare_flaky():
    assert _classes("PASS", "FAIL", "PASS") == "flaky"


def test_compare_regressed():
    assert _classes("PASS", "PASS", "FAIL") == "regressed"


def test_compare_fixed():
    assert _classes("FAIL", "PASS", "PASS") == "fixed"
    assert _classes("FAIL", "FAIL", "PASS") == "fixed"


def test_compare_same_code_makes_one_switch_flaky():
    assert _classes("FAIL", "PASS", "PASS", same_code=True) == "flaky"
    assert _classes("FAIL", "FAIL", same_code=True) == "failing"


def test_compare_failing():
    assert _classes("FAIL", "FAIL") == "failing"
    assert _classes("FAIL", "NOT ASSESSED", "FAIL") == "failing"


def test_compare_not_assessed_keeps_reason():
    runs = [
        ("run1", _log(("PASS", "tops: x", "3 of 3"))),
        ("run2", _log(("NOT ASSESSED", "tops: x", "no janitor spawned"))),
    ]
    rows = server._compare_rows(runs)
    assert rows[0].klass == "not_assessed"
    text = server._compare_text(rows)
    assert "run2 NOT ASSESSED no janitor spawned" in text


def test_compare_numbers_changed_ignores_decimals():
    def klass(a: str, b: str) -> str:
        runs = [
            ("run1", _log(("PASS", "tops: x", a))),
            ("run2", _log(("PASS", "tops: x", b))),
        ]
        return server._compare_rows(runs)[0].klass

    assert klass("3 of 3 trials 0.12 s", "3 of 3 trials 0.13 s") == "same"
    assert klass("3 of 3 trials 0.12 s", "2 of 3 trials 0.12 s") == (
        "numbers_changed"
    )
    assert server._mask_decimals("took 0.12 s, 3 of 4, 10.5") == (
        "took # s, 3 of 4, #"
    )


def test_compare_missing_by_occurrence():
    runs = [
        (
            "run1",
            _log(("PASS", "tops: x", "1"), ("PASS", "tops: x", "1")),
        ),
        ("run2", _log(("PASS", "tops: x", "1"))),
    ]
    rows = server._compare_rows(runs)
    assert [(r.name, r.occurrence, r.klass) for r in rows] == [
        ("tops: x", 1, "missing"),
        ("tops: x", 0, "same"),
    ]
    assert rows[0].runs[1].row is None


def test_compare_same_counted_not_listed():
    runs = [
        ("run1", _log(("PASS", "a", "1"), ("PASS", "b", "1"))),
        ("run2", _log(("PASS", "a", "1"), ("FAIL", "b", "1"))),
    ]
    rows = server._compare_rows(runs)
    assert [(r.name, r.klass) for r in rows] == [
        ("b", "regressed"),
        ("a", "same"),
    ]
    text = server._compare_text(rows)
    assert "a [same]" not in text and "b [regressed]" in text
    assert text.splitlines()[-1] == "rows: 1 regressed, 1 same"


def test_compare_sorts_by_class_then_name():
    runs = [
        (
            "run1",
            _log(
                ("FAIL", "z fixed", "1"),
                ("PASS", "b flaky", "1"),
                ("PASS", "y regressed", "1"),
                ("PASS", "a regressed", "1"),
            ),
        ),
        (
            "run2",
            _log(
                ("PASS", "z fixed", "1"),
                ("FAIL", "b flaky", "1"),
                ("FAIL", "y regressed", "1"),
                ("FAIL", "a regressed", "1"),
            ),
        ),
        (
            "run3",
            _log(
                ("PASS", "z fixed", "1"),
                ("PASS", "b flaky", "1"),
                ("FAIL", "y regressed", "1"),
                ("FAIL", "a regressed", "1"),
            ),
        ),
    ]
    rows = server._compare_rows(runs)
    assert [(r.name, r.klass) for r in rows] == [
        ("a regressed", "regressed"),
        ("y regressed", "regressed"),
        ("b flaky", "flaky"),
        ("z fixed", "fixed"),
    ]


def test_compare_text_trial_diff_and_cap():
    first = [f"  trial {i}: on top 0.{i}1 s" for i in range(13)]
    later = [f"  trial {i}: off after {i} s" for i in range(13)]
    timing = [f"  trial {i}: on top 0.{i}9 s" for i in range(13)]
    runs = [
        ("run1", _log(("PASS", "tops: x", "13 of 13", first))),
        ("run2", _log(("FAIL", "tops: x", "0 of 13", later))),
        ("run3", _log(("PASS", "tops: x", "13 of 13", timing))),
    ]
    text = server._compare_text(server._compare_rows(runs))
    lines = text.splitlines()
    assert lines[0] == "tops: x [flaky]"
    assert lines[1:4] == [
        "  run1 PASS 13 of 13",
        "  run2 FAIL 0 of 13",
        "  run3 PASS 13 of 13",
    ]
    assert lines[4] == "  run2 trial lines that differ from run1:"
    assert lines[5] == "      trial 0: off after 0 s"
    assert lines[16] == "      trial 11: off after 11 s"
    assert lines[17] == "    [1 more]"
    assert lines[18] == "  run3 trial lines match run1 apart from timings"
    assert lines[-1] == "rows: 1 flaky"


def test_compare_text_absent_run_and_no_rows():
    runs = [
        ("run1", _log(("FAIL", "tops: x", "1"))),
        ("run2", []),
    ]
    text = server._compare_text(server._compare_rows(runs))
    assert "  run2 (absent)" in text.splitlines()
    assert server._compare_text([]) == "rows: none differ"


# --- Task 6: castor_compare_runs ---------------------------------------------


def _write_log(path: Path, *rows: tuple) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(_log(*rows)) + "\n")
    return str(path)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )


def test_compare_tool_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "castor_compare_runs" in names


def test_compare_needs_two_logs(tmp_path):
    one = _write_log(tmp_path / "a.log", ("PASS", "x", "1"))
    out = server.castor_compare_runs(str(tmp_path), [one])
    assert out.startswith("[Error:") and "two logs" in out
    out = server.castor_compare_runs("rel", [one, one])
    assert out.startswith("[Error:") and "absolute" in out


def test_compare_labels_logs_by_stamp(tmp_path):
    a = _write_log(
        tmp_path / "Library" / "s" / "tops-20261104-010203.log",
        ("PASS", "x", "1"),
    )
    _write_log(tmp_path / "b.log", ("FAIL", "x", "1"))
    out = server.castor_compare_runs(str(tmp_path), [a, "b.log"])
    assert "  20261104-010203 PASS 1" in out.splitlines()
    assert "  run2 FAIL 1" in out.splitlines()
    assert out.splitlines()[-1] == "rows: 1 regressed"


def test_compare_log_in_sibling_worktree_allowed(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    _git(main, "config", "user.email", "t@example.com")
    _git(main, "config", "user.name", "t")
    (main / "f.txt").write_text("x\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "init")
    _git(main, "worktree", "add", "-q", str(tmp_path / "lane"))
    a = _write_log(main / "a.log", ("PASS", "x", "1"))
    b = _write_log(tmp_path / "lane" / "b.log", ("FAIL", "x", "1"))
    out = server.castor_compare_runs(str(main), [a, b])
    assert not out.startswith("[Error"), out
    assert "x [regressed]" in out


def test_compare_log_outside_refused(tmp_path):
    proj = tmp_path / "proj"
    a = _write_log(proj / "a.log", ("PASS", "x", "1"))
    b = _write_log(tmp_path / "elsewhere.log", ("FAIL", "x", "1"))
    out = server.castor_compare_runs(str(proj), [a, b])
    assert out.startswith("[Error:") and "outside" in out
    out = server.castor_compare_runs(str(proj), [a, "missing.log"])
    assert out.startswith("[Error:") and "not a file" in out


def test_compare_rows_filter(tmp_path):
    a = _write_log(
        tmp_path / "a.log", ("PASS", "tops: x", "1"), ("PASS", "doors: y", "1")
    )
    b = _write_log(
        tmp_path / "b.log", ("FAIL", "tops: x", "1"), ("FAIL", "doors: y", "1")
    )
    out = server.castor_compare_runs(str(tmp_path), [a, b], rows=["tops:"])
    assert "tops: x [regressed]" in out and "doors" not in out
    assert out.splitlines()[-1] == "rows: 1 regressed"


def test_compare_same_code_flag(tmp_path):
    a = _write_log(tmp_path / "a.log", ("FAIL", "x", "1"))
    b = _write_log(tmp_path / "b.log", ("PASS", "x", "1"))
    c = _write_log(tmp_path / "c.log", ("PASS", "x", "1"))
    logs = [a, b, c]
    assert "x [fixed]" in server.castor_compare_runs(str(tmp_path), logs)
    assert "x [flaky]" in server.castor_compare_runs(
        str(tmp_path), logs, same_code=True
    )


def test_compare_makes_no_nim_request(tmp_path, monkeypatch):
    def no_request(*_a: object, **_k: object) -> None:
        raise AssertionError("castor_compare_runs called NIM")

    monkeypatch.setattr(server, "_nim_request", no_request)
    a = _write_log(tmp_path / "a.log", ("PASS", "x", "1"))
    b = _write_log(tmp_path / "b.log", ("FAIL", "x", "1"))
    assert "x [regressed]" in server.castor_compare_runs(str(tmp_path), [a, b])
