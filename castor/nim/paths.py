"""Path confinement for the NIM agent: what it may read, and where."""

from pathlib import Path


# .dekko holds dekko's map and notes: read through the dekko tools only.
_NIM_AGENT_DENIED_DIRS = {".git", ".ssh", ".aws", ".gnupg", ".dekko"}


_AGENT_DENIED_ERROR = "path is off limits (secrets, .git and .dekko)"


_NIM_AGENT_DENIED_NAMES = {
    "id_rsa",
    "id_ed25519",
    ".netrc",
    ".npmrc",
    ".pypirc",
}


_NIM_AGENT_DENIED_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def _agent_denied(relative: Path) -> bool:
    """True for paths the agent may never read (secrets, .git, .dekko)."""
    if _NIM_AGENT_DENIED_DIRS & set(relative.parts):
        return True
    name = relative.name.lower()
    return (
        name.startswith(".env")
        or name in _NIM_AGENT_DENIED_NAMES
        or name.endswith(_NIM_AGENT_DENIED_SUFFIXES)
    )


def _agent_resolve(root: Path, raw: str) -> tuple[Path | None, str | None]:
    """Resolve `raw` inside `root`: (path, None) or (None, error).

    Symlinks are followed before the containment check, so a link out of
    the project is refused like any other outside path.
    """
    try:
        target = (root / (raw or ".")).resolve()
    except (OSError, RuntimeError, ValueError):
        return None, "invalid path"
    if not target.is_relative_to(root):
        return None, "path is outside the project"
    if _agent_denied(target.relative_to(root)):
        return None, _AGENT_DENIED_ERROR
    return target, None
