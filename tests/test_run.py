"""Tests for the run tier (castor_run_start and friends) and run compare.

Projects are tmp_path folders with tiny bash scripts; RUN_MINUTE_S and
RUN_POLL_S are patched so minutes become milliseconds. Every process a
test starts is ended by the test itself: Castor never kills one.
"""

import importlib
import json
import sys
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
