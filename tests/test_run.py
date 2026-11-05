"""Tests for the run tier (castor_run_start and friends) and run compare.

Projects are tmp_path folders with tiny bash scripts; RUN_MINUTE_S and
RUN_POLL_S are patched so minutes become milliseconds. Every process a
test starts is ended by the test itself: Castor never kills one.
"""

import importlib
import json
import os
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
