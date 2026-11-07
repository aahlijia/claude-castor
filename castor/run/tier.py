import fnmatch
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


from castor.app import (
    logger,
    mcp,
)
from castor.common import (
    RUN_CONFIG_FILE,
    _cache_dir,
    _nim_patience,
    _read_bytes,
    _run_regex,
    _worktree_roots,
)
from castor.logs import (
    DEFAULT_LOG_FORMAT,
    LogFormat,
    _clean_lines,
    _is_totals_line,
    _log_format_from,
    _verdict_rows,
    _verdict_totals,
)
from castor.nim.roles import (
    NIM_TRIAGE_STEPS,
    _dispatch_triage_json,
    _role_bad_input,
    _rows_json,
    _triage_task,
)


RUN_CONFIG_KEYS = frozenset(
    {
        "allow",
        "env_file",
        "log_line",
        "overdue_command",
        "grace_minutes",
        "max_hours",
        "max_running",
        "flags",
        "log_format",
    }
)

RUN_DEFAULT_LOG_LINE = "^log: (.+)$"

RUN_DEFAULT_GRACE_MINUTES = 10.0

RUN_DEFAULT_MAX_HOURS = 8.0

RUN_DEFAULT_MAX_RUNNING = 4


@dataclass(frozen=True)
class RunFlag:
    """A console line worth reporting; `stop` ends the watch on a match."""

    name: str
    pattern: re.Pattern
    stop: bool


@dataclass(frozen=True)
class RunConfig:
    """A project's .castor/run.json, checked and with defaults filled in."""

    allow: tuple[str, ...]
    env_file: str | None
    log_line: re.Pattern
    overdue_command: tuple[str, ...] | None
    grace_minutes: float
    max_hours: float
    max_running: int
    flags: tuple[RunFlag, ...]
    log_format: LogFormat = DEFAULT_LOG_FORMAT


def _run_enabled_for(root: Path) -> str | None:
    """None when runs are allowed under `root`, else the reason they are not.

    Args:
        root: The project root a run would start in.

    Returns:
        None, or a sentence naming the switch that refuses it.
    """
    if sys.platform == "win32":
        return "the run tier is POSIX only (it uses ps and process groups)."
    if os.environ.get("CASTOR_RUN") == "0":
        return "the run tier is off (CASTOR_RUN=0)."
    raw = os.environ.get("CASTOR_RUN_ROOTS", "")
    roots = [Path(p).expanduser() for p in raw.split(os.pathsep) if p]
    if not roots:
        return (
            "CASTOR_RUN_ROOTS is not set: list the project roots allowed "
            "to run commands in the castor MCP registration."
        )
    relative = [str(r) for r in roots if not r.is_absolute()]
    if relative:
        return f"CASTOR_RUN_ROOTS entries must be absolute: {relative}."
    target = root.resolve()
    for allowed in roots:
        if target in _worktree_roots(allowed):
            return None
    return (
        f"{str(root)!r} is not a CASTOR_RUN_ROOTS entry or a git worktree "
        "of one."
    )


def _run_config(root: Path) -> tuple[RunConfig | None, str | None]:
    """Read and check `root/.castor/run.json`.

    Args:
        root: The project root.

    Returns:
        (config, None), or (None, a sentence naming the bad key).
    """
    path = root / RUN_CONFIG_FILE
    if not path.is_file():
        return None, f"no {RUN_CONFIG_FILE} in {str(root)!r}; no runs."
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"{RUN_CONFIG_FILE} is not valid JSON: {exc}"
    if not isinstance(data, dict):
        return None, f"{RUN_CONFIG_FILE} must hold a JSON object."
    unknown = sorted(set(data) - RUN_CONFIG_KEYS)
    if unknown:
        return None, f"unknown key(s) in {RUN_CONFIG_FILE}: {unknown}"
    try:
        cfg = _run_config_from(data)
    except ValueError as exc:
        return None, f"{RUN_CONFIG_FILE}: {exc}"
    if cfg.overdue_command:
        _, error = _run_resolve_program(root, cfg.overdue_command[0])
        if error:
            return None, f"{RUN_CONFIG_FILE}: overdue_command: {error}"
    return cfg, None


def _run_config_from(data: dict) -> RunConfig:
    """Build a RunConfig from the file's object; ValueError names a key."""
    allow = _run_str_list(data.get("allow"), "allow")
    if not allow:
        raise ValueError("allow must list at least one glob.")
    env_file = data.get("env_file")
    if env_file is not None and not isinstance(env_file, str):
        raise ValueError("env_file must be a string.")
    overdue = data.get("overdue_command")
    if overdue is not None:
        overdue = tuple(_run_str_list(overdue, "overdue_command"))
    return RunConfig(
        allow=tuple(allow),
        env_file=env_file or None,
        log_line=_run_regex(
            data.get("log_line", RUN_DEFAULT_LOG_LINE), "log_line"
        ),
        overdue_command=overdue or None,
        grace_minutes=_run_number(
            data, "grace_minutes", RUN_DEFAULT_GRACE_MINUTES, minimum=0
        ),
        max_hours=_run_number(data, "max_hours", RUN_DEFAULT_MAX_HOURS),
        max_running=int(
            _run_number(data, "max_running", RUN_DEFAULT_MAX_RUNNING)
        ),
        flags=tuple(_run_flags(data.get("flags", []))),
        log_format=_log_format_from(data.get("log_format")),
    )


def _run_str_list(value: Any, key: str) -> list[str]:
    """A list of non-empty strings, or ValueError naming `key`."""
    if not isinstance(value, list) or not all(
        isinstance(v, str) and v for v in value
    ):
        raise ValueError(f"{key} must be a list of non-empty strings.")
    return value


def _run_number(
    data: dict, key: str, default: float, minimum: float | None = None
) -> float:
    """A number above 0 (or at least `minimum`), or ValueError naming key."""
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number.")
    if (minimum is None and value <= 0) or (
        minimum is not None and value < minimum
    ):
        raise ValueError(f"{key} is out of range: {value}.")
    return float(value)


def _run_flags(value: Any) -> list[RunFlag]:
    """The flags list, or ValueError naming the bad entry."""
    if not isinstance(value, list):
        raise ValueError("flags must be a list.")
    flags = []
    for i, entry in enumerate(value):
        key = f"flags[{i}]"
        if not isinstance(entry, dict) or not isinstance(
            entry.get("name"), str
        ):
            raise ValueError(f"{key} needs a name and a pattern.")
        extra = sorted(set(entry) - {"name", "pattern", "stop"})
        if extra:
            raise ValueError(f"{key} has unknown key(s): {extra}")
        stop = entry.get("stop", False)
        if not isinstance(stop, bool):
            raise ValueError(f"{key}.stop must be true or false.")
        pattern = _run_regex(entry.get("pattern"), f"{key}.pattern")
        flags.append(RunFlag(entry["name"], pattern, stop))
    return flags


def _run_resolve_program(
    root: Path, program: str
) -> tuple[Path | None, str | None]:
    """Resolve a program to an executable file inside the project.

    Args:
        root: The project root.
        program: The path, relative to `root` or absolute.

    Returns:
        (resolved path, None), or (None, the reason it is refused).
    """
    base = root.resolve()
    path = Path(program).expanduser()
    resolved = (path if path.is_absolute() else base / path).resolve()
    if not resolved.is_relative_to(base):
        return None, f"{program!r} resolves outside the project."
    if not resolved.is_file():
        return None, f"{program!r} is not a file."
    if not os.access(resolved, os.X_OK):
        return None, f"{program!r} is not executable."
    return resolved, None


def _run_resolve_command(
    root: Path, cfg: RunConfig, command: list[str]
) -> tuple[list[str] | None, str | None]:
    """Check a command against the allowlist and resolve its path.

    Args:
        root: The project root.
        cfg: The project's run config.
        command: The program (relative to `root` or absolute) and its
            arguments.

    Returns:
        (argv with the program as its resolved absolute path, None), or
        (None, the reason it is refused).
    """
    if not command or not command[0]:
        return None, "the command is empty."
    resolved, error = _run_resolve_program(root, command[0])
    if resolved is None:
        return None, error
    rel = resolved.relative_to(root.resolve()).as_posix()
    if not any(fnmatch.fnmatchcase(rel, glob) for glob in cfg.allow):
        return None, f"{rel!r} is not in {RUN_CONFIG_FILE}'s allow list."
    return [str(resolved), *command[1:]], None


# Run records live beside their capture files under the cache, so a run
# survives a server restart: gemini_poll reads the record and reattaches a
# monitor when the process is still the one Castor started.
RUNS_DIR = "runs"

RUN_RECORD_DAYS = 30

_RUN_ID_RE = re.compile(r"^run-[0-9a-f]{8}$")

RUN_MINUTE_S = 60.0  # seconds in a config minute; tests shrink it

RUN_HOUR_S = 3600.0  # seconds in a config hour; tests shrink it less

RUN_POLL_S = 2.0  # monitor tick

RUN_ATTACH_WAIT_S = 2.0  # a poll waits this long on a monitor it attached

RUN_PS_TIMEOUT = 5.0

_RUN_EXIT_VERDICTS = {0: "passed", 1: "failed", 2: "inconclusive"}

_run_monitors: dict[str, threading.Thread] = {}

_run_monitors_lock = threading.Lock()


def _runs_dir() -> Path:
    """Where run records and capture files live."""
    return _cache_dir() / RUNS_DIR


def _run_config_dict(cfg: RunConfig) -> dict:
    """The config as run.json holds it; each record keeps a copy."""
    return {
        "allow": list(cfg.allow),
        "env_file": cfg.env_file,
        "log_line": cfg.log_line.pattern,
        "overdue_command": (
            list(cfg.overdue_command) if cfg.overdue_command else None
        ),
        "grace_minutes": cfg.grace_minutes,
        "max_hours": cfg.max_hours,
        "max_running": cfg.max_running,
        "flags": [
            {"name": f.name, "pattern": f.pattern.pattern, "stop": f.stop}
            for f in cfg.flags
        ],
    }


def _run_new_record(
    root: Path,
    cfg: RunConfig,
    command: list[str],
    label: str,
    expect_minutes: int,
    triage: bool,
) -> dict:
    """A fresh record for a run about to start (no pid yet)."""
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    return {
        "run": run_id,
        "status": "running",
        "root": str(root.resolve()),
        "command": list(command),
        "label": label,
        "pid": None,
        "pid_start": None,
        "capture": str(_runs_dir() / f"{run_id}.out"),
        "started_at": time.time(),
        "finished_at": None,
        "expect_minutes": expect_minutes,
        "triage": triage,
        "config": _run_config_dict(cfg),
        "offset": 0,
        "flags": [],
        "overdue": None,
        "result": None,
    }


def _run_record_path(rec: dict) -> Path:
    """The record file, beside the run's capture file."""
    return Path(rec["capture"]).with_suffix(".json")


def _run_record_write(rec: dict) -> None:
    """Write a record atomically, pruning records past RUN_RECORD_DAYS."""
    path = _run_record_path(rec)
    path.parent.mkdir(parents=True, exist_ok=True)
    _run_prune(path.parent)
    tmp = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _run_prune(folder: Path) -> None:
    """Delete records and captures older than RUN_RECORD_DAYS."""
    cutoff = time.time() - RUN_RECORD_DAYS * 86400
    try:
        for entry in folder.iterdir():
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
    except OSError:
        return


def _run_record_load(path: Path) -> dict | None:
    """One record file's object, or None when it is unreadable."""
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return rec if isinstance(rec, dict) else None


def _run_record_read(run_id: str) -> dict | None:
    """The record for a run id, or None (unknown or malformed id)."""
    if not _RUN_ID_RE.match(run_id):
        return None
    return _run_record_load(_runs_dir() / f"{run_id}.json")


def _run_records(root: Path | None) -> list[dict]:
    """Records under `root` (all when None), newest first."""
    folder = _runs_dir()
    if not folder.is_dir():
        return []
    want = str(root.resolve()) if root is not None else None
    recs = [
        rec
        for rec in map(_run_record_load, sorted(folder.glob("run-*.json")))
        if rec is not None and (want is None or rec.get("root") == want)
    ]
    return sorted(recs, key=lambda r: r.get("started_at") or 0, reverse=True)


def _pid_start(pid: int) -> str | None:
    """A process's start time as ps prints it, or None when it is gone."""
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=RUN_PS_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = out.stdout.strip()
    return text if out.returncode == 0 and text else None


def _run_alive(rec: dict) -> bool:
    """True when the record's pid is alive and is the process it started."""
    pid = rec.get("pid")
    if not pid:
        return False
    start = _pid_start(pid)
    return start is not None and start == rec.get("pid_start")


def _run_poll(run_id: str) -> str:
    """gemini_poll for a run: the result, or progress while it runs.

    A run whose monitor is not in this server (a restart) gets one
    attached; a poll waits briefly for it, so a run that ended while no
    one watched comes back finished.
    """
    rec = _run_record_read(run_id)
    if rec is None:
        return f"[Unknown run {run_id}]"
    if rec["status"] == "running":
        thread = _run_attach(run_id)
        if thread is not None:
            thread.join(timeout=RUN_ATTACH_WAIT_S)
            rec = _run_record_read(run_id) or rec
    if rec["status"] != "running":
        return json.dumps(rec["result"], ensure_ascii=False)
    return json.dumps(_run_progress(rec), ensure_ascii=False)


def _run_progress(rec: dict) -> dict:
    """The object gemini_poll returns while a run is going."""
    return {
        "run": rec["run"],
        "status": "running",
        "elapsed_s": int(time.time() - rec["started_at"]),
        "pid": rec["pid"],
        "flags": rec.get("flags", []),
        "overdue": rec.get("overdue"),
    }


def _run_attach(
    run_id: str, proc: subprocess.Popen | None = None
) -> threading.Thread | None:
    """Start a monitor for a run unless one is already watching it.

    The monitor holds an exclusive lock on the run's lock file for its
    life, so a second Castor process (another Claude session) never
    watches the same run; the kernel drops the lock when its holder
    dies, so a restarted server can take over.
    """
    with _run_monitors_lock:
        current = _run_monitors.get(run_id)
        if current is not None and current.is_alive():
            return None
        lock = _run_lock(run_id)
        if lock is None:
            return None
        thread = threading.Thread(
            target=_run_monitor_locked,
            args=(run_id, proc, lock),
            name=f"castor-{run_id}",
            daemon=True,
        )
        _run_monitors[run_id] = thread
        thread.start()
    return thread


def _run_lock(run_id: str) -> int | None:
    """An fd holding the run's exclusive lock, or None when it is held."""
    import fcntl  # POSIX only, like the run tier

    folder = _runs_dir()
    folder.mkdir(parents=True, exist_ok=True)
    fd = os.open(folder / f"{run_id}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _run_monitor_locked(
    run_id: str, proc: subprocess.Popen | None, lock: int
) -> None:
    """_run_monitor, releasing the run's lock when it returns."""
    try:
        _run_monitor(run_id, proc)
    finally:
        os.close(lock)


def _run_exit(
    rec: dict, proc: subprocess.Popen | None
) -> tuple[bool, int | None]:
    """(ended, exit code); the code is None for a reattached run."""
    if proc is not None:
        code = proc.poll()
        return code is not None, code
    return not _run_alive(rec), None


def _run_capture_lines(rec: dict) -> list[str]:
    """The run's console output so far, one string per line."""
    data = _read_bytes(Path(rec["capture"])) or b""
    return _clean_lines(data.decode("utf-8", errors="replace").splitlines())


def _run_has_table(lines: list[str], fmt: LogFormat) -> bool:
    """True when the output reached a verdict table or a totals line."""
    return (
        _run_table(lines, fmt) is not None
        or _verdict_totals(lines, fmt) is not None
    )


def _run_table(lines: list[str], fmt: LogFormat) -> str | None:
    """The last reprint block (`--- verdicts ---`) through its totals line."""
    if fmt.reprint is None:
        return None
    starts = [i for i, ln in enumerate(lines) if ln == fmt.reprint]
    if not starts:
        return None
    block = lines[starts[-1] :]
    for i, line in enumerate(block):
        if _is_totals_line(line, fmt):
            return "\n".join(block[: i + 1])
    return "\n".join(block).rstrip()


def _run_finish(
    rec: dict, cfg: RunConfig, exit_code: int | None, status: str
) -> None:
    """Record a run's end: status, finish time and result."""
    rec["finished_at"] = time.time()
    rec["status"] = status
    result = _run_result(
        rec, cfg, exit_code, status, rec.get("flags", []), rec.get("overdue")
    )
    if rec.get("triage") and result["rows"]:
        full = _run_log_file(Path(rec["root"]), result["log"])
        result["triage"] = (
            _run_triage(Path(rec["root"]), str(full))
            if full is not None
            else {
                "error": "bad_input",
                "message": "the run printed no log inside the project",
            }
        )
    rec["result"] = result
    _run_record_write(rec)


def _run_result(
    rec: dict,
    cfg: RunConfig,
    exit_code: int | None,
    status: str,
    flags: list[dict],
    overdue: dict | None,
) -> dict:
    """The final object gemini_poll returns for a run.

    Rows and totals come from the full log when its `log:` line resolves
    inside the project, else from the console output.
    """
    capture = _run_capture_lines(rec)
    log = _run_log_name(capture, cfg)
    full = _run_log_file(Path(rec["root"]), log)
    lines = capture
    if full is not None:
        data = _read_bytes(full) or b""
        lines = _clean_lines(
            data.decode("utf-8", errors="replace").splitlines()
        )
    fmt = cfg.log_format
    totals = _verdict_totals(lines, fmt)
    result = {
        "run": rec["run"],
        "status": status,
        "command": rec["command"],
        "cwd": rec["root"],
        "exit_code": exit_code,
        "wall_s": round(
            (rec["finished_at"] or time.time()) - rec["started_at"]
        ),
        "verdict": _run_verdict(exit_code, totals),
        "totals": totals,
        "rows": _rows_json(_verdict_rows(lines, fmt)),
        "table": _run_table(capture, fmt),
        "log": log,
        "flags": flags,
        "overdue": overdue,
        "triage": None,
    }
    if status in ("stopped", "gave_up"):
        result["pid"] = rec["pid"]
    return result


def _run_verdict(exit_code: int | None, totals: dict | None) -> str | None:
    """From the exit status (0, 1, 2), else from the totals."""
    if exit_code in _RUN_EXIT_VERDICTS:
        return _RUN_EXIT_VERDICTS[exit_code]
    if totals is None:
        return None
    if totals["fail"] > 0:
        return "failed"
    if totals["not_assessed"] > 0:
        return "inconclusive"
    return "passed"


def _run_log_name(lines: list[str], cfg: RunConfig) -> str | None:
    """The log path the run printed (last `log_line` match), or None."""
    for line in reversed(lines):
        match = cfg.log_line.search(line)
        if match:
            text = match.group(1) if match.groups() else match.group(0)
            return text.strip() or None
    return None


def _run_log_file(root: Path, log: str | None) -> Path | None:
    """The printed log as a file inside the project, or None."""
    if not log:
        return None
    path = Path(log).expanduser()
    full = (path if path.is_absolute() else root / path).resolve()
    if full.is_relative_to(root.resolve()) and full.is_file():
        return full
    return None


# The child sources the env file (when one is set) and execs the command:
# arguments are positional parameters, never parsed by a shell.
RUN_BASH = '[ -n "$1" ] && . "$1"; shift; exec "$@"'

RUN_SCRUBBED_PREFIXES = ("NVIDIA_", "CASTOR_")

RUN_MAX_FLAGS = 50  # flag matches kept per run

RUN_FLAG_LINE_CHARS = 300

RUN_OVERDUE_WAIT_S = 60.0  # the overdue command is left alone after this

RUN_OVERDUE_OUTPUT_CHARS = 4000


def _run_child_env() -> dict[str, str]:
    """The server's environment minus every NVIDIA_* and CASTOR_* name."""
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(RUN_SCRUBBED_PREFIXES)
    }


def _run_env_file(root: Path, cfg: RunConfig) -> tuple[str | None, str | None]:
    """(absolute env file or None, None), or (None, error) when it is bad.

    The path is made absolute so bash's `.` never searches PATH for it.
    """
    if not cfg.env_file:
        return None, None
    base = root.resolve()
    path = Path(cfg.env_file).expanduser()
    full = (path if path.is_absolute() else base / path).resolve()
    if not full.is_relative_to(base) or not full.is_file():
        return None, (
            f"env_file {cfg.env_file!r} is not a file inside the project."
        )
    return str(full), None


def _run_launch(
    root: Path, argv: list[str], capture: Path, env_file: str | None
) -> subprocess.Popen:
    """Start the command in its own session, output to the capture file."""
    capture.parent.mkdir(parents=True, exist_ok=True)
    with capture.open("wb") as out:
        return subprocess.Popen(
            ["bash", "-c", RUN_BASH, "castor-run", env_file or "", *argv],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=_run_child_env(),
        )


def _run_begin(
    root: Path,
    cfg: RunConfig,
    argv: list[str],
    command: list[str],
    label: str,
    expect_minutes: int,
    triage: bool,
) -> tuple[dict | None, str | None]:
    """Start a checked command, record it and attach its monitor.

    Args:
        root: The project root.
        cfg: The project's run config.
        argv: The command from _run_resolve_command.
        command: The command as the caller gave it (kept in the record).
        label: A short name for listings.
        expect_minutes: When the run should be over; overdue after this
            plus the config's grace minutes.
        triage: Triage the log when a row fails or is not assessed.

    Returns:
        (record, None), or (None, the reason it did not start).
    """
    env_file, error = _run_env_file(root, cfg)
    if error:
        return None, error
    rec = _run_new_record(root, cfg, command, label, expect_minutes, triage)
    try:
        proc = _run_launch(root, argv, Path(rec["capture"]), env_file)
    except OSError as exc:
        return None, f"could not start the command: {exc}"
    rec["pid"] = proc.pid
    rec["pid_start"] = _pid_start(proc.pid)
    _run_record_write(rec)
    _run_attach(rec["run"], proc)
    return rec, None


class _RunTail:
    """Reads a capture file's new complete lines from a byte offset.

    A line still being written stays in `carry` until its newline
    arrives, so a flag never matches a fragment, and never twice.
    """

    def __init__(self, path: Path, offset: int) -> None:
        self.path = path
        self.offset = offset
        self.carry = b""

    def lines(self, final: bool) -> list[str]:
        """The lines completed since the last call (all of them if final)."""
        try:
            with self.path.open("rb") as fh:
                fh.seek(self.offset)
                new = fh.read()
        except OSError:
            new = b""
        self.offset += len(new)
        parts = (self.carry + new).split(b"\n")
        self.carry = parts.pop()
        if final and self.carry:
            parts.append(self.carry)
            self.carry = b""
        return [
            p.decode("utf-8", errors="replace").rstrip("\r") for p in parts
        ]

    def consumed(self) -> int:
        """The offset of the first byte not yet read as a whole line."""
        return self.offset - len(self.carry)


def _run_monitor(run_id: str, proc: subprocess.Popen | None) -> None:
    """Watch one run until it ends, a stop flag shows, or max_hours passes.

    Args:
        run_id: The run.
        proc: The process when this server started it; None when the
            monitor was reattached (there is then no exit code).
    """
    rec = _run_record_read(run_id)
    if rec is None or rec["status"] != "running":
        return  # a poll raced the previous monitor's last write
    _nim_patience.on = True  # triage at the end may wait for NIM
    cfg = _run_config_from(rec["config"])
    try:
        exit_code, status = _run_watch(rec, cfg, proc)
    except Exception as exc:  # a dead monitor must not leave "running"
        logger.exception("run monitor %s failed", run_id)
        rec.setdefault("flags", []).append(
            {"name": "monitor_error", "line": str(exc)[:RUN_FLAG_LINE_CHARS]}
        )
        exit_code, status = None, "lost"
    _run_finish(rec, cfg, exit_code, status)


def _run_watch(
    rec: dict, cfg: RunConfig, proc: subprocess.Popen | None
) -> tuple[int | None, str]:
    """The monitor loop: (exit code, final status)."""
    tail = _RunTail(Path(rec["capture"]), rec.get("offset", 0))
    start = rec["started_at"]
    overdue_at = start + (
        (rec["expect_minutes"] + cfg.grace_minutes) * RUN_MINUTE_S
    )
    give_up_at = start + cfg.max_hours * RUN_HOUR_S
    while True:
        ended, exit_code = _run_exit(rec, proc)
        stop = _run_scan(rec, cfg, tail, final=ended)
        if ended:
            lines = _run_capture_lines(rec)
            if proc is None and not _run_has_table(lines, cfg.log_format):
                return None, "lost"
            return exit_code, "done"
        if stop:
            return None, "stopped"
        now = time.time()
        if now >= overdue_at and _run_wants_overdue(rec, cfg):
            rec["overdue"] = _run_overdue(rec, cfg)
            _run_record_write(rec)
        if now >= give_up_at:
            return None, "gave_up"
        time.sleep(RUN_POLL_S)


def _run_scan(rec: dict, cfg: RunConfig, tail: _RunTail, final: bool) -> bool:
    """Match flags on new whole lines; True when a stop flag matched."""
    found = False
    stop = False
    for line in tail.lines(final):
        for flag in cfg.flags:
            if not flag.pattern.search(line):
                continue
            stop = stop or flag.stop
            if len(rec["flags"]) < RUN_MAX_FLAGS:
                rec["flags"].append(
                    {"name": flag.name, "line": line[:RUN_FLAG_LINE_CHARS]}
                )
                found = True
    if found:
        rec["offset"] = tail.consumed()
        _run_record_write(rec)
    return stop


def _run_wants_overdue(rec: dict, cfg: RunConfig) -> bool:
    """True once: an overdue command is set and no table is out yet."""
    if rec.get("overdue") is not None or not cfg.overdue_command:
        return False
    return not _run_has_table(_run_capture_lines(rec), cfg.log_format)


def _run_overdue(rec: dict, cfg: RunConfig) -> dict:
    """Run the overdue command once: {at_s, exit_code, output}.

    It is left running (never killed) when it takes over
    RUN_OVERDUE_WAIT_S; exit_code is then None.
    """
    root = Path(rec["root"])
    at_s = int(time.time() - rec["started_at"])
    off = _run_enabled_for(root)
    if off:
        return {"at_s": at_s, "exit_code": None, "output": f"not run: {off}"}
    program, error = _run_resolve_program(root, cfg.overdue_command[0])
    if program is None:
        return {"at_s": at_s, "exit_code": None, "output": error}
    argv = [str(program), *cfg.overdue_command[1:]]
    out_path = Path(rec["capture"]).with_suffix(".overdue")
    try:
        with out_path.open("wb") as out:
            proc = subprocess.Popen(
                argv,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=_run_child_env(),
            )
        code = proc.wait(timeout=RUN_OVERDUE_WAIT_S)
    except subprocess.TimeoutExpired:
        code = None
    except OSError as exc:
        return {"at_s": at_s, "exit_code": None, "output": str(exc)}
    data = _read_bytes(out_path) or b""
    text = data.decode("utf-8", errors="replace")
    return {
        "at_s": at_s,
        "exit_code": code,
        "output": text[:RUN_OVERDUE_OUTPUT_CHARS],
    }


def _run_triage(root: Path, log: str) -> dict:
    """Triage JSON for the run's log (phase 1's role), or an error object."""
    task, cwd, error, path = _triage_task(str(root), log, None)
    if error or task is None or path is None:
        return json.loads(_role_bad_input(error, "json"))
    label = str(path.relative_to(cwd))
    try:
        text = _dispatch_triage_json(
            task, cwd, None, NIM_TRIAGE_STEPS, label, path
        )
        return json.loads(text)
    except Exception as exc:  # triage is a bonus; the run result stands
        return {"error": "failed", "message": str(exc)}


RUN_LABEL_CHARS = 60


@mcp.tool()
def castor_run_start(
    cwd: str,
    command: list[str],
    expect_minutes: int,
    triage: bool = True,
    label: str | None = None,
) -> str:
    """Run an allowlisted suite command in the background and report it.

    Needs two keys: the project's committed `.castor/run.json` (allow
    globs, flags, limits) and `cwd` inside CASTOR_RUN_ROOTS in the castor
    registration. The command runs in its own session with NVIDIA_* and
    CASTOR_* removed from its environment; Castor watches its output and
    never kills it. Not a shell: the command is a script path the allow
    list names, plus its arguments.

    Args:
        cwd: Absolute project root.
        command: The script (relative to `cwd` or absolute) and its
            arguments, one list item each.
        expect_minutes: How long the run should take. Past this plus the
            config's grace minutes, the config's overdue command runs once.
        triage: Triage the log with a NIM agent when a row fails or is not
            assessed (default true).
        label: A short name for castor_runs (default: the command).

    Returns:
        "Run <id> started: <label>; collect it with gemini_poll(...)", or a
        bracketed error.
    """
    error = _run_start_error(cwd, command, expect_minutes)
    if error:
        return f"[Error: {error}]"
    root = Path(cwd).expanduser()
    cfg, error = _run_config(root)
    if cfg is None:
        return f"[Error: {error}]"
    argv, error = _run_resolve_command(root, cfg, command)
    if argv is None:
        return f"[Error: {error}]"
    error = _run_capacity_error(root, cfg)
    if error:
        return f"[Error: {error}]"
    name = (label or " ".join([Path(command[0]).name, *command[1:]]))[
        :RUN_LABEL_CHARS
    ]
    rec, error = _run_begin(
        root, cfg, argv, command, name, expect_minutes, triage
    )
    if rec is None:
        return f"[Error: {error}]"
    run_id = rec["run"]
    return (
        f"Run {run_id} started: {name}; collect it with "
        f'gemini_poll("{run_id}").'
    )


def _run_start_error(
    cwd: str, command: list[str], expect_minutes: int
) -> str | None:
    """The input error for castor_run_start, before any file is read."""
    if not command or not all(isinstance(c, str) for c in command):
        return "the command is empty or not a list of strings."
    if isinstance(expect_minutes, bool) or not isinstance(
        expect_minutes, (int, float)
    ):
        return "expect_minutes must be a number of minutes."
    if expect_minutes <= 0:
        return "expect_minutes must be above 0."
    root = Path(cwd).expanduser()
    if not root.is_absolute():
        return f"cwd must be an absolute path, not {cwd!r}."
    if not root.is_dir():
        return f"{cwd!r} is not a directory."
    return _run_enabled_for(root)


def _run_capacity_error(root: Path, cfg: RunConfig) -> str | None:
    """An error naming the live runs when max_running are going."""
    live = [
        r["run"]
        for r in _run_records(root)
        if r.get("status") in ("running", "stopped", "gave_up")
        and _run_alive(r)
    ]
    if len(live) < cfg.max_running:
        return None
    return (
        f"{len(live)} run(s) already going under this root (max_running "
        f"{cfg.max_running}): {', '.join(live)}. Collect them with "
        "gemini_poll first."
    )


@mcp.tool()
def castor_runs(cwd: str | None = None) -> str:
    """List the runs on record (kept 30 days), newest first.

    Args:
        cwd: A project root to list runs for; all runs when omitted.

    Returns:
        One line per run: id, status, label, elapsed or wall seconds,
        verdict (or -); or "No runs on record."
    """
    root = Path(cwd).expanduser() if cwd else None
    recs = _run_records(root)
    if not recs:
        return "No runs on record."
    return "\n".join(_run_line(rec) for rec in recs)


def _run_line(rec: dict) -> str:
    """One castor_runs line for a record."""
    result = rec.get("result") or {}
    if rec.get("status") == "running":
        secs = int(time.time() - rec["started_at"])
    else:
        secs = result.get("wall_s", 0)
    verdict = result.get("verdict") or "-"
    return f"{rec['run']} {rec['status']} {rec['label']} {secs}s {verdict}"
