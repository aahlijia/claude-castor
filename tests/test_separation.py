"""The backends stay separate: each one loads and works without the other.

Each check runs in a fresh interpreter with a module blocked, because a
module this test process already imported cannot be un-imported.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "castor"

NEUTRAL = (
    "castor.app",
    "castor.common",
    "castor.jobs",
    "castor.logs",
    "castor.tools",
)
AGY = ("castor.agy.cli", "castor.agy.prompts", "castor.agy.tools")
NIM = (
    "castor.nim.config",
    "castor.nim.limits",
    "castor.nim.records",
    "castor.nim.transport",
    "castor.nim.backend",
    "castor.nim.paths",
    "castor.nim.edit_tools",
    "castor.nim.agent",
    "castor.nim.roles",
    "castor.nim.explain",
    "castor.nim.edit",
)
RUN = ("castor.run.tier", "castor.run.compare")

NEUTRAL_TOOLS = {
    "castor_poll",
    "castor_jobs",
    "castor_reset",
    "castor_sessions",
    "castor_session_show",
    "castor_session_delete",
    "castor_cache_clear",
}


def _python(code, env=None):
    """Run `code` in a fresh interpreter rooted at the repo."""
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT), **(env or {})},
        timeout=60,
    )


def _import_with_blocked(blocked, module):
    """Import `module` while every name in `blocked` cannot be imported."""
    blocks = "".join(f"sys.modules[{name!r}] = None\n" for name in blocked)
    return _python(
        f"import sys, importlib\n{blocks}importlib.import_module({module!r})\n"
    )


@pytest.mark.parametrize("module", NEUTRAL + NIM + RUN)
def test_loads_without_agy(module):
    done = _import_with_blocked(["castor.agy"], module)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("module", NEUTRAL + AGY + RUN)
def test_loads_without_nim(module):
    done = _import_with_blocked(["castor.nim", "httpx"], module)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("module", NEUTRAL + AGY + NIM)
def test_loads_without_the_run_tier(module):
    done = _import_with_blocked(["castor.run"], module)
    assert done.returncode == 0, done.stderr


def _castor_imports(path):
    """Every castor module `path` imports, at any depth, with its line."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("castor"):
                found.append((node.module, node.lineno))
        elif isinstance(node, ast.Import):
            found += [
                (alias.name, node.lineno)
                for alias in node.names
                if alias.name.startswith("castor")
            ]
    return found


def _sources(folder):
    """The non-test modules under `folder`."""
    return [p for p in folder.rglob("*.py") if p.name != "__init__.py"]


def test_agy_never_names_nim_or_the_run_tier():
    for path in _sources(PACKAGE / "agy"):
        for module, line in _castor_imports(path):
            assert not module.startswith(("castor.nim", "castor.run")), (
                f"{path.name}:{line} imports {module}"
            )


def test_nim_never_names_agy_or_the_run_tier():
    for path in _sources(PACKAGE / "nim"):
        for module, line in _castor_imports(path):
            assert not module.startswith(("castor.agy", "castor.run")), (
                f"{path.name}:{line} imports {module}"
            )


@pytest.mark.parametrize(
    ("module", "allowed"),
    [
        ("config", set()),
        ("limits", {"castor.nim.config"}),
        ("records", {"castor.nim.config"}),
        (
            "transport",
            {"castor.nim.config", "castor.nim.limits", "castor.nim.records"},
        ),
    ],
)
def test_nim_infrastructure_imports_one_way(module, allowed):
    path = PACKAGE / "nim" / f"{module}.py"
    named = {m for m, _ in _castor_imports(path) if m.startswith("castor.nim")}
    assert named <= allowed


@pytest.mark.parametrize(
    "name", ["app.py", "common.py", "jobs.py", "logs.py", "tools.py"]
)
def test_neutral_modules_name_no_backend(name):
    for module, line in _castor_imports(PACKAGE / name):
        assert not module.startswith(
            ("castor.agy", "castor.nim", "castor.run")
        ), f"{name}:{line} imports {module}"


def test_run_tier_reaches_nim_only_inside_triage():
    tree = ast.parse((PACKAGE / "run" / "tier.py").read_text(encoding="utf-8"))
    triage = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_run_triage"
    )
    allowed = {id(n) for n in ast.walk(triage)}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("castor.nim"):
                assert id(node) in allowed, f"line {node.lineno}"


def test_agy_pool_ignores_nim_concurrency():
    code = (
        "from castor.agy.tools import _agy_job_pool\n"
        "from castor.jobs import JOB_POOL_MAX\n"
        "assert _agy_job_pool._max_workers == JOB_POOL_MAX\n"
    )
    done = _python(code, env={"CASTOR_NIM_CONCURRENCY": "1"})
    assert done.returncode == 0, done.stderr


def test_status_loads_without_nim_and_omits_its_line():
    code = (
        "import sys\n"
        "sys.modules['castor.nim'] = None\n"
        "from castor.app import _status_lines\n"
        "import castor.agy.tools\n"
        "assert _status_lines == []\n"
    )
    done = _python(code)
    assert done.returncode == 0, done.stderr


def test_run_triage_without_nim_is_an_error_object():
    code = (
        "import sys\n"
        "sys.modules['castor.nim'] = None\n"
        "from pathlib import Path\n"
        "from castor.run.tier import _run_triage\n"
        "result = _run_triage(Path('.'), 'suite.log')\n"
        "assert result['error'] == 'failed', result\n"
    )
    done = _python(code)
    assert done.returncode == 0, done.stderr


def _tool_names(path):
    """The functions in `path` registered with @mcp.tool()."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any("mcp.tool" in ast.unparse(d) for d in node.decorator_list)
    }


def test_neutral_tools_live_in_one_module_under_castor_names():
    assert _tool_names(PACKAGE / "tools.py") == NEUTRAL_TOOLS


def test_no_tool_keeps_an_old_neutral_name():
    old = {name.replace("castor_", "gemini_", 1) for name in NEUTRAL_TOOLS}
    for path in _sources(PACKAGE):
        assert not (_tool_names(path) & old), path.name


@pytest.mark.parametrize(
    "path", _sources(PACKAGE), ids=lambda p: str(p.relative_to(PACKAGE))
)
def test_module_docstring_is_not_stranded_below_the_imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    stranded = [
        node.lineno
        for node in tree.body[1:]
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]
    assert not stranded, f"{path.name}: bare string at {stranded}"
