"""Brief packs: the text of the ranges a scout answer cites.

A scout answers with pointers (`path:start-end`, a short quote, a reason).
Each pointer still costs its reader a turn to open. This module copies the
text of those ranges from disk, after the agent's citation and quote checks
have tagged the answer, so the reader starts with the code in hand: inline
after the answer, or in a pack file another agent reads in one call.

The model never writes this text. Only ranges that passed the checks are
copied, through the agent's own path rules, so a pack holds nothing the
agent could not read. No network, no model.
"""

import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from castor.nim.agent import (
    NIM_AGENT_MAX_FILE_BYTES,
    _BARE_CITATION_RE,
    _CITATION_RE,
    _QUOTE_RE,
    _agent_text_lines,
    _citation_target,
    _unique_basenames,
)
from castor.nim.paths import _agent_resolve


NIM_PACK_TOKENS = 6000  # text budget when the caller names none

NIM_PACK_MIN_TOKENS = 1000

NIM_PACK_MAX_TOKENS = 20000  # past this one Read may not return the file

NIM_PACK_MAX_LINES = 1500  # one Read returns at most 2,000 lines

NIM_PACK_PAD = 2  # lines of context kept on each side of a range

NIM_PACK_GAP = 6  # ranges of one file this close are shown as one

NIM_PACK_RANGE_LINES = 120  # most lines kept of one scout range

NIM_PACK_LINE_CHARS = 400  # a longer line is cut, with a visible mark

NIM_PACK_WHY_CHARS = 160

NIM_PACK_TASK_CHARS = 300

NIM_PACK_DIR = ".castor/packs"  # inside cwd, so a subagent may Read it

NIM_PACK_KEEP = 100  # packs kept per project

NIM_PACK_DAYS = 7  # a pack is a snapshot; older ones are deleted

# Where the text of a scout's ranges goes.
NIM_SCOUT_TEXT_MODES = ("none", "inline", "file")

NIM_SCOUT_TEXT_ERROR = '[Error: text must be "none", "inline" or "file".]'

NIM_PACK_LEGEND = (
    'Each line below is "number<TAB>text", as Read prints it; the text '
    "after the\ntab is exact. This is what a scout judged relevant, not "
    "everything the task\ntouches. It was copied when this was built: if "
    "an Edit fails to match,\nre-read that range."
)

# The tags the agent's citation and quote checks add to an answer.
_TAG_RE = re.compile(
    r"\s*\[(?:unverified|path is [^\]]+|quote is at line \d+"
    r"|quote not in file)\]"
)

_QUOTE_AT_RE = re.compile(r"\[quote is at line (\d+)\]")

_NOT_READ_RE = re.compile(r"\(not read\)", re.IGNORECASE)

_SPAN_RE = re.compile(r"(\d+)(?:-(\d+))?")

_WHY_SPLIT_RE = re.compile(r"\s+-\s+")

# A remainder of a range that is held; it is named, not counted as a range.
_PAST_CAP = f"past {NIM_PACK_RANGE_LINES} lines"


@dataclass(frozen=True)
class _Lead:
    """One range a scout answer cites and the checks let through.

    `quote_at` is the line the quote check found the bullet's quote on,
    when that is not in the range; None when the quote matched.
    """

    path: str
    start: int
    end: int
    why: str
    quote_at: int | None


@dataclass(frozen=True)
class _Skip:
    """A cited range whose text is not held, and why."""

    cite: str
    reason: str


@dataclass
class _Excerpt:
    """Padded, merged ranges of one file, shown as one block."""

    start: int
    end: int
    whys: list[str]
    quote_at: list[int]


@dataclass(frozen=True)
class _Pack:
    """The text of a scout answer's ranges and what it holds.

    Attributes:
        body: The excerpts, ready to print.
        held: Ranges whose text is in `body`.
        total: Ranges the answer cites.
        lines: File lines in `body`.
        files: Files those lines come from.
        skips: The ranges left out, in the answer's order.
    """

    body: str
    held: int
    total: int
    lines: int
    files: int
    skips: list[_Skip]


class _Files:
    """The project's text files by path, each read once."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.unique = _unique_basenames(root)
        self._lines: dict[str, list[str] | None] = {}

    def lines(self, path: str) -> list[str] | None:
        """A file's lines, or None when the agent could not read it."""
        if path not in self._lines:
            self._lines[path] = self._read(path)
        return self._lines[path]

    def _read(self, path: str) -> list[str] | None:
        target, error = _agent_resolve(self.root, path)
        if error or target is None or not target.is_file():
            return None
        if target.stat().st_size > NIM_AGENT_MAX_FILE_BYTES:
            return None

        return _agent_text_lines(self.root, path)


def _first_cite(line: str, files: _Files) -> re.Match | None:
    """The first citation on a line; a bare name only when it is a file."""
    match = _CITATION_RE.search(line)
    if match is not None:
        return match

    match = _BARE_CITATION_RE.search(line)
    if match is None:
        return None
    path = _citation_target(files.root, match.group(1), files.unique)
    return match if files.lines(path) is not None else None


def _line_fault(line: str, cite_end: int) -> str | None:
    """Why a cited line's text must not be held, if a check failed it."""
    tail = line[cite_end:]
    if tail.lstrip().startswith("[unverified]"):
        return "unverified"
    if "[quote not in file]" in tail:
        return "quote not in file"
    if _NOT_READ_RE.search(line):
        return "not read"

    return None


def _lead_why(tail: str) -> str:
    """The reason a bullet gives after its quote, on one line."""
    text = _QUOTE_RE.sub("", _TAG_RE.sub("", tail), count=1)
    parts = [part.strip(" -*_`") for part in _WHY_SPLIT_RE.split(text)]
    why = next((part for part in reversed(parts) if part), "")
    return " ".join(why.split())[:NIM_PACK_WHY_CHARS].rstrip(".")


def _cite(path: str, start: int, end: int) -> str:
    """A range as the answer would write it."""
    return f"{path}:{start}" if start == end else f"{path}:{start}-{end}"


def _span_lead(
    path: str,
    span: tuple[int, int],
    total: int,
    why: str,
    quote_at: int | None,
) -> tuple[_Lead | None, _Skip | None]:
    """One cited span as a lead, cut to NIM_PACK_RANGE_LINES lines.

    Returns:
        (lead, skip): no lead for a span outside the file; a skip naming
        the lines past the cap when the span was cut.
    """
    start, end = span
    if not 1 <= start <= end <= total:
        return None, _Skip(_cite(path, start, end), "unverified")

    cut = start + NIM_PACK_RANGE_LINES - 1
    if end <= cut:
        return _Lead(path, start, end, why, quote_at), None

    rest = _Skip(_cite(path, cut + 1, end), _PAST_CAP)
    return _Lead(path, start, cut, why, quote_at), rest


def _line_leads(
    line: str, files: _Files
) -> tuple[list[_Lead], list[_Skip], int]:
    """The ranges one answer line cites: (leads, skips, ranges cited)."""
    match = _first_cite(line, files)
    if match is None:
        return [], [], 0

    path = _citation_target(files.root, match.group(1), files.unique)
    spans = [
        (int(a), int(b or a)) for a, b in _SPAN_RE.findall(match.group(2))
    ]
    text = files.lines(path)
    fault = _line_fault(line, match.end())
    if fault is None and text is None:
        fault = "not readable"
    if fault is not None or text is None:
        skips = [_Skip(_cite(path, a, b), fault or "") for a, b in spans]
        return [], skips, len(spans)

    elsewhere = _QUOTE_AT_RE.search(line)
    quote_at = int(elsewhere.group(1)) if elsewhere else None
    why = _lead_why(line[match.end() :])
    leads: list[_Lead] = []
    skips = []
    for span in spans:
        lead, skip = _span_lead(path, span, len(text), why, quote_at)
        leads += [lead] if lead else []
        skips += [skip] if skip else []
    return leads, skips, len(spans)


def _answer_leads(
    answer: str, files: _Files
) -> tuple[list[_Lead], list[_Skip], int]:
    """Every range a scout answer cites, in the answer's order."""
    leads: list[_Lead] = []
    skips: list[_Skip] = []
    total = 0
    for line in answer.splitlines():
        if line.lstrip().startswith("[nim_agent:"):
            continue
        found, left, cited = _line_leads(line, files)
        leads += found
        skips += left
        total += cited
    return leads, skips, total


def _excerpts(leads: list[_Lead], total: int) -> list[_Excerpt]:
    """One file's leads as padded excerpts in line order, close ones merged."""
    merged: list[_Excerpt] = []
    for lead in sorted(leads, key=lambda item: item.start):
        start = max(1, lead.start - NIM_PACK_PAD)
        end = min(total, lead.end + NIM_PACK_PAD)
        last = merged[-1] if merged else None
        at = [lead.quote_at] if lead.quote_at is not None else []
        if last is None or start - last.end - 1 > NIM_PACK_GAP:
            whys = [lead.why] if lead.why else []
            merged.append(_Excerpt(start, end, whys, at))
            continue
        last.end = max(last.end, end)
        last.quote_at += [n for n in at if n not in last.quote_at]
        if lead.why and lead.why not in last.whys:
            last.whys.append(lead.why)
    return merged


def _numbered(number: int, text: str) -> str:
    """A file line in Read's layout; a very long one is cut and marked."""
    extra = len(text) - NIM_PACK_LINE_CHARS
    if extra > 0:
        text = f"{text[:NIM_PACK_LINE_CHARS]} [+{extra} chars cut]"
    return f"{number:>6}\t{text}"


def _excerpt_text(path: str, excerpt: _Excerpt, lines: list[str]) -> str:
    """One excerpt: its heading, its reasons, then its numbered lines."""
    span = range(excerpt.start, excerpt.end + 1)
    long = any(len(lines[n - 1]) > NIM_PACK_LINE_CHARS for n in span)
    flags = ""
    if excerpt.quote_at:
        where = ", ".join(str(n) for n in excerpt.quote_at)
        flags += f", quote found at line {where} instead"
    if long:
        flags += ", long lines cut"
    head = (
        f"## {path}:{excerpt.start}-{excerpt.end} "
        f"(of {len(lines):,} lines){flags}"
    )
    why = [f"why: {'; '.join(excerpt.whys)}"] if excerpt.whys else []
    shown = [_numbered(n, lines[n - 1]) for n in span]
    return "\n".join([head, *why, *shown])


def _by_file(leads: list[_Lead]) -> dict[str, list[_Lead]]:
    """Leads grouped by file; files keep the order of their best lead."""
    groups: dict[str, list[_Lead]] = {}
    for lead in leads:
        groups.setdefault(lead.path, []).append(lead)
    return groups


def _render(leads: list[_Lead], files: _Files) -> tuple[str, int]:
    """The excerpts for `leads`: (text, file lines shown)."""
    blocks = []
    shown = 0
    for path, group in _by_file(leads).items():
        lines = files.lines(path) or []
        for excerpt in _excerpts(group, len(lines)):
            blocks.append(_excerpt_text(path, excerpt, lines))
            shown += excerpt.end - excerpt.start + 1
    return "\n\n".join(blocks), shown


def _fit(
    leads: list[_Lead], files: _Files, chars: int
) -> tuple[list[_Lead], list[_Skip]]:
    """The leads whose text fits in `chars`, first fit in scout's order.

    A lead that does not fit is skipped, and a later, smaller one may
    still go in.
    """
    kept: list[_Lead] = []
    skips = []
    for lead in leads:
        body, shown = _render([*kept, lead], files)
        if len(body) <= chars and shown <= NIM_PACK_MAX_LINES:
            kept.append(lead)
        else:
            skips.append(
                _Skip(_cite(lead.path, lead.start, lead.end), "over budget")
            )
    return kept, skips


def _pack_chars(tokens: int | None) -> int:
    """The text budget in characters (about four to a token)."""
    wanted = tokens or NIM_PACK_TOKENS
    return 4 * min(max(wanted, NIM_PACK_MIN_TOKENS), NIM_PACK_MAX_TOKENS)


def _build_pack(answer: str, root: Path, chars: int) -> _Pack:
    """Read a checked scout answer into the text of its ranges."""
    files = _Files(root)
    leads, skips, total = _answer_leads(answer, files)
    kept, over = _fit(leads, files, chars)
    body, shown = _render(kept, files)
    return _Pack(
        body=body,
        held=len(kept),
        total=total,
        lines=shown,
        files=len(_by_file(kept)),
        skips=[*skips, *over],
    )


def _left_out(skips: list[_Skip]) -> str:
    """The skipped ranges counted by reason: "1 over budget, 2 unverified"."""
    counts = Counter(skip.reason for skip in skips)
    return ", ".join(f"{counts[reason]} {reason}" for reason in sorted(counts))


def _summary(label: str, pack: _Pack, where: str = "") -> str:
    """The one line that tells the caller what was held and what was not."""
    left = f" | left out: {_left_out(pack.skips)}" if pack.skips else ""
    return (
        f"[{label}: {where}{pack.held} of {pack.total} ranges, "
        f"{pack.lines} lines, about {len(pack.body) // 4:,} tokens{left}]"
    )


def _nothing(label: str, pack: _Pack) -> str:
    """The line for an answer none of whose ranges could be held."""
    if not pack.total:
        return f"[{label}: the answer cites no range]"

    return f"[{label}: no range could be held ({_left_out(pack.skips)})]"


def _pack_header(question: str, pack: _Pack) -> str:
    """The top of a pack file: the task, when it was built, what it holds."""
    files = "1 file" if pack.files == 1 else f"{pack.files} files"
    lines = [
        "# Castor brief pack",
        f"task: {' '.join(question.split())[:NIM_PACK_TASK_CHARS]}",
        f"built: {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime())}",
        f"holds: {pack.held} of {pack.total} scout ranges, {files}, "
        f"{pack.lines} lines, about {len(pack.body) // 4:,} tokens",
    ]
    if pack.skips:
        named = "; ".join(f"{s.cite} ({s.reason})" for s in pack.skips)
        lines.append(f"not held (see the scout answer): {named}")

    return "\n".join(lines)


def _evict_packs(folder: Path) -> None:
    """Delete packs past NIM_PACK_DAYS, then the oldest past NIM_PACK_KEEP.

    Room is left for the pack about to be written.
    """
    cutoff = time.time() - NIM_PACK_DAYS * 86400
    fresh = []
    for path in sorted(folder.glob("*.md"), key=lambda p: p.stat().st_mtime):
        if path.stat().st_mtime < cutoff:
            path.unlink()
        else:
            fresh.append(path)
    for path in fresh[: max(0, len(fresh) - (NIM_PACK_KEEP - 1))]:
        path.unlink()


def _write_pack(root: Path, question: str, pack: _Pack) -> str:
    """Write the pack under `root` and return the line that names it.

    The folder ignores itself, so a pack never shows in `git status` and
    the repo's own .gitignore is left alone.
    """
    folder = root / NIM_PACK_DIR
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    name = f"{stamp}-{uuid.uuid4().hex[:6]}.md"
    text = "\n\n".join(
        [_pack_header(question, pack), NIM_PACK_LEGEND, pack.body]
    )
    try:
        folder.mkdir(parents=True, exist_ok=True)
        ignore = folder / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
        _evict_packs(folder)
        (folder / name).write_text(text + "\n", encoding="utf-8")
    except OSError as exc:
        return f"[pack not written: {exc.strerror or exc}]"

    return _summary("pack", pack, f"{NIM_PACK_DIR}/{name} | ")


def _scout_text(
    answer: str,
    cwd: str,
    question: str,
    mode: str,
    tokens: int | None = None,
) -> str:
    """A scout answer with the text of its ranges added.

    Args:
        answer: The agent's answer, check tags and footer included.
        cwd: The project root the scout ran in.
        question: The scout question, for the pack's header.
        mode: "none" (the answer as it is), "inline" (the text follows
            the answer) or "file" (the text goes to a pack file under
            `cwd` and the answer names it).
        tokens: Text budget (NIM_PACK_TOKENS when None), clamped.

    Returns:
        The answer, then the text or one line naming the pack. An error
        answer comes back as it is.
    """
    if mode == "none" or answer.startswith("["):
        return answer

    root = Path(cwd).expanduser().resolve()
    pack = _build_pack(answer, root, _pack_chars(tokens))
    if not pack.held:
        label = "text not added" if mode == "inline" else "pack not written"
        return f"{answer}\n{_nothing(label, pack)}"
    if mode == "inline":
        added = "\n".join([_summary("text", pack), NIM_PACK_LEGEND])
        return f"{answer}\n\n{added}\n\n{pack.body}"

    return f"{answer}\n{_write_pack(root, question, pack)}"
