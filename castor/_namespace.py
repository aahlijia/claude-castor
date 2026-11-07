"""One namespace over every castor module, for tests and eval scripts.

The code used to be a single `server.py`, so a test could read or patch any
name as `server.<name>`. The names now live in several modules, and a patch
only reaches the code that looks the name up in the module it patched.
`view()` returns an object that behaves like the old single module: reading
a name finds the module that defines it, and setting one sets it in that
module and in every module that imported it with `from ... import`.

Not used by the server itself.
"""

import ast
import importlib
from pathlib import Path
from types import ModuleType
from typing import Any

MODULES = (
    "castor.app",
    "castor.common",
    "castor.logs",
    "castor.jobs",
    "castor.agy",
    "castor.nim.backend",
    "castor.nim.edit_tools",
    "castor.nim.agent",
    "castor.nim.roles",
    "castor.nim.explain",
    "castor.nim.edit",
    "castor.run.tier",
    "castor.run.compare",
)


def _defined_names(module: ModuleType) -> set[str]:
    """Top-level names a module defines itself (not names it imports)."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            for target in targets:
                names.update(
                    n.id for n in ast.walk(target) if isinstance(n, ast.Name)
                )
    return names


class _View:
    """Reads and sets names across the castor modules as one namespace."""

    def __init__(self) -> None:
        mods = [importlib.import_module(name) for name in MODULES]
        object.__setattr__(self, "_mods", mods)
        owner: dict[str, ModuleType] = {}
        defined: dict[str, set[str]] = {}
        for mod in mods:
            defined[mod.__name__] = _defined_names(mod)
            for name in defined[mod.__name__]:
                owner.setdefault(name, mod)
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_defined", defined)
        object.__setattr__(self, "_saved", {})
        object.__setattr__(self, "_extra", {})

    def __getattr__(self, name: str) -> Any:
        extra = self._extra
        if name in extra:
            return extra[name]
        owner = self._owner.get(name)
        if owner is not None:
            return getattr(owner, name)
        for mod in self._mods:
            if hasattr(mod, name):
                return getattr(mod, name)
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        owner = self._owner.get(name)
        if owner is None and not any(name in vars(m) for m in self._mods):
            self._extra[name] = value  # a scratch flag, as on a plain module
            return
        old = getattr(self, name)
        saved = self._saved.setdefault(name, {})
        for mod in self._mods:
            own = name in self._defined[mod.__name__]
            if own or (name in vars(mod) and vars(mod)[name] is old):
                saved.setdefault(mod.__name__, (mod, vars(mod)[name]))
                setattr(mod, name, value)

    def __delattr__(self, name: str) -> None:
        """Undo every set of `name`: pytest's monkeypatch calls this."""
        if name in self._extra:
            del self._extra[name]
            return
        saved = self._saved.pop(name, None)
        if saved is None:
            return  # patched twice: the first delete already restored it
        for mod, original in saved.values():
            setattr(mod, name, original)


def view() -> _View:
    """The single-namespace view (build it after `castor` is importable)."""
    return _View()
