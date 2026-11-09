import difflib
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any


from castor.common import (
    _is_binary_bytes,
    _read_bytes,
)
from castor.nim.paths import (
    _AGENT_DENIED_ERROR,
    _agent_resolve,
)


def _edit_one_line(text: str) -> str:
    """`text` with its line breaks folded into spaces."""
    return " ".join(text.split())


@dataclass(frozen=True)
class EditBlock:
    """One exact-text edit: replace `old` with `new` in the file `path`.

    `deletes` is True when the `>>>NEW` section had no lines at all (not
    one empty line): the old text's whole lines go, terminators included.
    """

    index: int
    path: str
    old: str
    new: str
    deletes: bool = False


_EDIT_PATH_ERRORS = {
    "path is outside the project": "outside cwd",
    _AGENT_DENIED_ERROR: "denied",
}


def _edit_in_scope(path: Path, root: Path, scope: list[str] | None) -> bool:
    """True when `path` is an entry of `scope` or inside an entry directory."""
    if scope is None:
        return True

    for entry in scope:
        try:
            base = (root / entry).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if path == base or path.is_relative_to(base):
            return True

    return False


def _edit_resolve(
    raw: str, root: Path, scope: list[str] | None
) -> tuple[Path | None, str | None]:
    """Resolve an edit target and its scope: (path, None) or (None, reason)."""
    path, error = _agent_resolve(root, raw)
    if error or path is None:
        return None, _EDIT_PATH_ERRORS.get(error or "", "invalid path")

    if not _edit_in_scope(path, root, scope):
        return None, "outside scope"

    return path, None


def _edit_read(path: Path) -> tuple[str | None, str | None]:
    """Read one file as strict UTF-8 text: (text, None) or (None, reason)."""
    data = _read_bytes(path) if path.is_file() else None
    if data is None:
        return None, "file creation is not supported"

    if _is_binary_bytes(data):
        return None, "binary file"

    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "not UTF-8"


def _edit_find_all(text: str, needle: str) -> list[int]:
    """Start offsets of every occurrence of `needle`, overlaps included."""
    found: list[int] = []
    at = text.find(needle)
    while at != -1:
        found.append(at)
        at = text.find(needle, at + 1)

    return found


# Repair hints for a block whose old text matched more than once or not at
# all: they tell the model where to look in its repair round.
EDIT_HINT_MAX_LINES = 8

EDIT_HINT_MIN_CHARS = 8

EDIT_HINT_CUTOFF = 0.8

EDIT_HINT_QUOTE = 100

EDIT_HINT_FULL = 400  # a cut-short line is quoted whole so it can be copied


def _edit_line_at(text: str, offset: int) -> int:
    """1-based line number holding `offset`."""
    return text.count("\n", 0, offset) + 1


def _edit_hits_hint(text: str, hits: list[int]) -> str:
    """The lines where a duplicated old text starts, for the repair round."""
    lines = [str(_edit_line_at(text, at)) for at in hits[:EDIT_HINT_MAX_LINES]]
    if len(hits) > EDIT_HINT_MAX_LINES:
        lines.append("...")
    return f"lines {', '.join(lines)}; add a line that tells them apart"


def _edit_missing_hint(text: str, old: str) -> str:
    """Where an old text that is not in the file probably went wrong.

    Points at its first line when that line is in the file (a later line
    differs), else at the file line closest to it; "" when neither helps.
    """
    old_lines = [line.rstrip("\r") for line in old.split("\n")]
    first = next((line for line in old_lines if line.strip()), "")
    if len(first.strip()) < EDIT_HINT_MIN_CHARS:
        return ""

    file_lines = [line.rstrip("\r") for line in text.split("\n")]
    starts = [i + 1 for i, line in enumerate(file_lines) if line == first]
    if starts:
        at = starts[0]
        listed = ", ".join(str(n) for n in starts[:EDIT_HINT_MAX_LINES])
        return (
            f" (its first line is at line {listed}; a later line differs, "
            f"re-read lines {at}-{at + len(old_lines) - 1})"
        )

    close = difflib.get_close_matches(
        first, file_lines, n=1, cutoff=EDIT_HINT_CUTOFF
    )
    if not close:
        return ""

    line = file_lines.index(close[0]) + 1
    return f" (closest is line {line}: `{close[0][:EDIT_HINT_QUOTE]}`)"


def _edit_partial_hint(text: str, start: int, end: int) -> str:
    """Quote the line(s) a mid-line match cut short, for the repair round."""
    lines = text.split("\n")
    numbers = sorted({_edit_line_at(text, start), _edit_line_at(text, end)})
    quoted = [
        f"line {n} in full: `{lines[n - 1].rstrip(chr(13))[:EDIT_HINT_FULL]}`"
        for n in numbers
    ]
    return "; " + "; ".join(quoted)


def _edit_span(text: str, start: int, old: str) -> tuple[int, int]:
    """1-based inclusive line range of `old` matched at offset `start`.

    A leading or trailing line terminator does not count as touching the
    line it ends; an `old` that is only terminators spans the line holding
    the first one.
    """
    lead = 2 if old.startswith("\r\n") else 1 if old.startswith("\n") else 0
    if lead == len(old):
        line = text.count("\n", 0, start) + 1
        return line, line

    body = old[lead:]
    first = text.count("\n", 0, start + lead) + 1
    inside = body.count("\n") - (1 if body.endswith("\n") else 0)
    return first, first + inside


def _edit_locate(
    text: str,
    block: EditBlock,
    rel: str,
    taken: list[tuple[int, int, int]],
) -> tuple[tuple[int, int, str] | None, str | None]:
    """Match one block in `text`: ((start, end, new), None) or (None, error).

    `taken` holds (start, end, index) of the file's earlier accepted blocks.
    The match must be whole lines: it starts a line and ends right before
    a line terminator or at the end of the file.
    """
    crlf = "\r\n" in text
    old = block.old.replace("\n", "\r\n") if crlf else block.old
    new = block.new.replace("\n", "\r\n") if crlf else block.new
    if not old:
        return None, "old text is empty"

    if old == new:
        return None, "old and new text are identical"

    hits = _edit_find_all(text, old)
    if not hits:
        hint = _edit_missing_hint(text, old)
        return None, f"old text not found in {rel}{hint}"

    if len(hits) > 1:
        return None, (
            f"old text matches {len(hits)} times in {rel} "
            f"({_edit_hits_hint(text, hits)})"
        )

    start = hits[0]
    end = start + len(old)
    if not _edit_whole_lines(text, start, end):
        return None, (
            "old text must be whole lines (copy each line in full)"
            f"{_edit_partial_hint(text, start, end)}"
        )

    if block.deletes:
        start, end = _edit_delete_range(text, start, end)
    for other_start, other_end, other_index in taken:
        if start < other_end and other_start < end:
            return None, f"overlaps block {other_index} in {rel}"

    return (start, end, new), None


def _edit_whole_lines(text: str, start: int, end: int) -> bool:
    """True when text[start:end] starts a line and ends one (or the file)."""
    starts_line = start == 0 or text[start - 1] == "\n"
    ends_line = end == len(text) or text.startswith(("\n", "\r\n"), end)
    return starts_line and ends_line


def _edit_delete_range(text: str, start: int, end: int) -> tuple[int, int]:
    """Widen a whole-line delete of text[start:end] to take a terminator.

    It takes the LF or CRLF after it, or at the end of a file with no
    final terminator the one before it, so no blank line is left.
    """
    after = (
        2 if text.startswith("\r\n", end) else int(text[end : end + 1] == "\n")
    )
    if after:
        return start, end + after

    before = 2 if text[:start].endswith("\r\n") else int(start > 0)
    return start - before, end


# --- nim_edit: edits recorded through propose_edit -------------------------

NIM_EDIT_CALLS_PER_STEP = 16

NIM_EDIT_CUT_NUDGE = (
    "Your reply was cut off. Record any change still missing with "
    "propose_edit, or reply done if every change is recorded."
)

NIM_AGENT_EDIT_NOTE = (
    " propose_edit and drop_edit calls count apart from the others: up to "
    f"{NIM_EDIT_CALLS_PER_STEP} of them per request."
)


NIM_EDIT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "propose_edit",
            "description": (
                "Record one edit: replace old with new in path. Castor "
                "checks it at once and answers with the edit number and "
                "its lines, or why it was refused. No file is written."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to the project",
                    },
                    "old": {
                        "type": "string",
                        "description": (
                            "Whole lines copied exactly from read_range or "
                            'grep output, without the "N: " or "path:N:" '
                            "prefixes"
                        ),
                    },
                    "new": {
                        "type": "string",
                        "description": (
                            'The replacement lines. new "" deletes the old '
                            "lines."
                        ),
                    },
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drop_edit",
            "description": "Remove a recorded edit by its number.",
            "parameters": {
                "type": "object",
                "properties": {
                    "edit": {
                        "type": "integer",
                        "description": "The number propose_edit gave it",
                    },
                },
                "required": ["edit"],
            },
        },
    },
]

NIM_EDIT_TOOL_NAMES = frozenset(
    tool["function"]["name"] for tool in NIM_EDIT_TOOLS
)


@dataclass(frozen=True)
class _StoredEdit:
    """One recorded edit: its block, file and matched range.

    `start` and `end` are offsets in the file's original text (a delete
    widened to its terminator); `lines` is the 1-based inclusive range.
    """

    block: EditBlock
    path: Path
    relative: str
    start: int
    end: int
    lines: tuple[int, int]


@dataclass
class _EditStore:
    """The edits one nim_edit run records through propose_edit.

    One store serves the first loop and the repair loop. `edits` maps
    each recorded edit's number to it, in the order recorded; `texts`
    holds the original text of every file read for them; `refusals` the
    refusal messages given. `drops` counts successful drop_edit calls (a
    propose_edit repeat key carries it), `calls` the edit tool calls run,
    and `used` is set once the recorded edits were taken as the answer.
    """

    scope: list[str] | None
    edits: dict[int, _StoredEdit] = field(default_factory=dict)
    texts: dict[Path, str] = field(default_factory=dict)
    refusals: list[str] = field(default_factory=list)
    next_index: int = 1
    drops: int = 0
    calls: int = 0
    used: bool = False


def _edit_recorded(store: _EditStore | None) -> bool:
    """True when a store is given and holds at least one edit."""
    return store is not None and bool(store.edits)


def _edit_run_tool(
    store: _EditStore, root: Path, name: str, args: dict
) -> str:
    """Run one propose_edit or drop_edit call against the store."""
    store.calls += 1
    try:
        if name == "propose_edit":
            return _edit_propose(store, root, args)
        return _edit_drop(store, args.get("edit"))
    except (OSError, TypeError, ValueError, OverflowError) as exc:
        return f"[tool error: {exc}]"


def _edit_proposed_block(args: dict, index: int) -> EditBlock | None:
    """The block a propose_edit call asks for, or None for bad arguments.

    new "" deletes the old lines; then one trailing newline comes off old
    and new, so new "\\n" leaves one empty line.
    """
    path, old, new = (args.get(key) for key in ("path", "old", "new"))
    if not all(isinstance(value, str) for value in (path, old, new)):
        return None

    deletes = new == ""
    return EditBlock(
        index, path, old.removesuffix("\n"), new.removesuffix("\n"), deletes
    )


def _edit_propose(store: _EditStore, root: Path, args: dict) -> str:
    """Check one proposed edit now: record it, or refuse it with the reason."""
    block = _edit_proposed_block(args, store.next_index)
    if block is None:
        edit, error = None, "path, old and new must be strings"
    else:
        edit, error = _edit_place(store, root, block)
    if error or edit is None:
        message = f"edit refused: {error or 'not placed'}"
        store.refusals.append(message)
        return message

    store.edits[edit.block.index] = edit
    store.next_index += 1
    first, last = edit.lines
    return (
        f"edit {edit.block.index} recorded: {edit.relative} "
        f"lines {first}-{last}"
    )


def _edit_store_text(
    store: _EditStore, path: Path
) -> tuple[str | None, str | None]:
    """A file's original text, read once per run: (text, None) or (None,
    reason)."""
    if path in store.texts:
        return store.texts[path], None

    text, error = _edit_read(path)
    if text is not None:
        store.texts[path] = text
    return text, error


def _edit_place(
    store: _EditStore, root: Path, block: EditBlock
) -> tuple[_StoredEdit | None, str | None]:
    """Match a block against its file's original text and the recorded
    edits in that file, as _edit_apply_in_memory does."""
    path, error = _edit_resolve(block.path, root, store.scope)
    if error or path is None:
        return None, error or "invalid path"

    text, error = _edit_store_text(store, path)
    if text is None:
        return None, error or "file creation is not supported"

    rel = path.relative_to(root).as_posix()
    taken = [
        (edit.start, edit.end, index)
        for index, edit in store.edits.items()
        if edit.path == path
    ]
    hit, error = _edit_locate(text, block, rel, taken)
    if error or hit is None:
        return None, error

    start, end, _ = hit
    lines = _edit_span(text, start, text[start:end])
    return _StoredEdit(block, path, rel, start, end, lines), None


def _edit_drop(store: _EditStore, raw: Any) -> str:
    """Remove recorded edit `raw` (its number) from the store."""
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return "[error: edit must be a recorded edit's number]"

    if store.edits.pop(number, None) is None:
        return f"[error: no recorded edit {number}]"

    store.drops += 1
    return f"edit {number} dropped"
