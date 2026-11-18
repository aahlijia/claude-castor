"""Replay past implementers' tasks through scout and measure the pack.

The brief pack's gate (design step 3): before anyone depends on a pack,
check that scout finds what implementers needed. For each recent subagent
that edited files in one project, this rebuilds its task (the prompt, plus
the text of any brief file the prompt names), runs nim_scout with
text="inline" on the replay repo, and compares the ranges the pack would
hold with what the implementer read before its first edit.

Reads come from Read calls and from shell readers in Bash calls (cat, nl,
head, tail, sed -n, awk NR ranges); searches (grep) are not reads. Only
files git tracks in the replay repo count, since scout can only find
those, and the brief itself is the task, not a read.

    uv run --with httpx --with fastmcp python scripts/pack_replay.py \\
        --repo ~/Projects/game-development/no-questions-cleaning \\
        [--agent-type nqc-implementer] [--limit 20] [--days 30]

The bar: the pack holds at least 60% of the files read; under 40%, stop.
Line counts are approximate: the tree has moved since the transcripts.
"""

import argparse
import importlib.util
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from dataclasses import astuple
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_usage_audit() -> ModuleType:
    """The sibling usage_audit script, for its transcript helpers."""
    path = Path(__file__).resolve().with_name("usage_audit.py")
    spec = importlib.util.spec_from_file_location("usage_audit", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


usage_audit = _load_usage_audit()


@dataclass(frozen=True)
class Read:
    """Lines one call printed from a file.

    `start` and `end` count from 1, and `end` None reads to the end of the
    file. A negative `start` counts from the end, as `tail -N` does.
    """

    path: str
    start: int = 1
    end: int | None = None


# Shell words that separate one simple command from the next.
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", ";;", "|&"})

_REDIRECTS = frozenset({">", ">>", "<", ">&", "&>", "&>>", "<&", ">|"})

# Words that can open a simple command without being the command.
_KEYWORDS = frozenset(
    {"do", "then", "else", "elif", "if", "while", "until", "time", "!", "{"}
)

_ASSIGN_RE = re.compile(r"^([A-Za-z_]\w*)=(.*)$", re.DOTALL)

_VAR_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")

# A here-document: the rest of its first line is kept, its body dropped.
_HEREDOC_RE = re.compile(
    r"(?<!<)<<-?[ \t]*(['\"]?)(\w+)\1([^\n]*)\n.*?\n[ \t]*\2[ \t]*(?=\n|$)",
    re.DOTALL,
)

_SED_RANGE_RE = re.compile(r"(\d+)(?:\s*,\s*(\d+|\$))?\s*p")

_AWK_FROM_RE = re.compile(r"NR\s*(>=|>|==)\s*(\d+)")

_AWK_TO_RE = re.compile(r"NR\s*(<=|<|==)\s*(\d+)")

HEAD_LINES = 10  # head and tail without a count


def _words(command: str) -> list[str] | None:
    """A shell line as words and operators; None when it does not parse."""
    text = _HEREDOC_RE.sub(r"\3", command.replace("\\\n", " "))
    lexer = shlex.shlex(
        text.replace("\n", " ; "), posix=True, punctuation_chars=True
    )
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def _drop_redirects(words: list[str]) -> list[str]:
    """Words without redirections, their targets or their fd numbers."""
    kept: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
        elif word in _REDIRECTS:
            skip = True
            if kept and kept[-1].isdigit():
                kept.pop()
        else:
            kept.append(word)
    return kept


def _simple_commands(words: list[str]) -> list[list[str]]:
    """Words split into simple commands, keywords and redirections gone."""
    commands: list[list[str]] = [[]]
    for word in words:
        if word in _SEPARATORS:
            commands.append([])
        else:
            commands[-1].append(word)
    found = []
    for command in commands:
        words = _drop_redirects(command)
        while words and words[0] in _KEYWORDS:
            words = words[1:]
        if words:
            found.append(words)
    return found


def _expand(word: str, env: dict[str, str]) -> str | None:
    """A word with `~` and known variables expanded; None if one is not."""
    if word == "~" or word.startswith("~/"):
        word = str(Path.home()) + word[1:]
    text = _VAR_RE.sub(lambda m: env.get(m.group(1) or m.group(2), "$"), word)
    return None if "$" in text else text


def _split_args(
    args: list[str], takes_value: frozenset[str]
) -> tuple[list[tuple[str, str]], list[str]]:
    """A command's arguments as (flag, value) options and plain words.

    `-30` reads as `-n 30`, the way head and tail take it.
    """
    options: list[tuple[str, str]] = []
    plain: list[str] = []
    pending = None
    for word in args:
        if pending is not None:
            options.append((pending, word))
            pending = None
        elif word[1:].isdigit() and word.startswith("-"):
            options.append(("-n", word[1:]))
        elif word.startswith("-") and len(word) > 1:
            flag = word[:2]
            if flag in takes_value and len(word) == 2:
                pending = flag
            else:
                options.append((flag, word[2:]))
        else:
            plain.append(word)
    return options, plain


Span = tuple[str, int, int | None]  # (word naming a file, start, end)


def _cat_spans(args: list[str]) -> list[Span]:
    """cat and nl print whole files."""
    _, files = _split_args(args, frozenset())
    return [(name, 1, None) for name in files]


def _head_spans(args: list[str]) -> list[Span]:
    """head prints the first N lines (whole files when counting bytes)."""
    options, files = _split_args(args, frozenset({"-n", "-c"}))
    flags = dict(options)
    if "-c" in flags:
        return [(name, 1, None) for name in files]
    count = flags.get("-n", "")
    lines = int(count) if count.isdigit() else HEAD_LINES
    return [(name, 1, lines) for name in files]


def _tail_spans(args: list[str]) -> list[Span]:
    """tail prints the last N lines, or from line N with `+N`."""
    options, files = _split_args(args, frozenset({"-n", "-c"}))
    count = dict(options).get("-n", "")
    if count.startswith("+") and count[1:].isdigit():
        return [(name, int(count[1:]), None) for name in files]
    lines = int(count) if count.isdigit() else HEAD_LINES
    return [(name, -lines, None) for name in files]


def _sed_spans(args: list[str]) -> list[Span]:
    """sed -n prints its `A,Bp` ranges; other sed scripts print all."""
    options, plain = _split_args(args, frozenset({"-e", "-f", "-l"}))
    flags = dict(options)
    if "-i" in flags:
        return []
    scripts = [value for flag, value in options if flag == "-e"]
    if not scripts and "-f" not in flags:
        scripts, plain = plain[:1], plain[1:]
    ranges = [
        (int(start), None if end == "$" else int(end or start))
        for script in scripts
        for start, end in _SED_RANGE_RE.findall(script)
    ]
    if "-n" not in flags or not ranges:
        ranges = [(1, None)]
    return [(name, start, end) for name in plain for start, end in ranges]


def _awk_spans(args: list[str]) -> list[Span]:
    """awk prints the lines its NR bounds allow, else the whole file."""
    options, plain = _split_args(args, frozenset({"-F", "-v", "-f"}))
    if "-f" not in dict(options):
        script, plain = (plain[0] if plain else ""), plain[1:]
    else:
        script = ""
    start, end = 1, None
    low = _AWK_FROM_RE.search(script)
    if low:
        start = int(low.group(2)) + (low.group(1) == ">")
    high = _AWK_TO_RE.search(script)
    if high:
        end = int(high.group(2)) - (high.group(1) == "<")
    return [(name, start, end) for name in plain]


_READERS = {
    "cat": _cat_spans,
    "nl": _cat_spans,
    "head": _head_spans,
    "tail": _tail_spans,
    "sed": _sed_spans,
    "awk": _awk_spans,
}


def _run_command(
    words: list[str], env: dict[str, str], where: list[str]
) -> list[Read] | None:
    """The reads of one simple command; tracks cd and assignments.

    Returns:
        The reads of a reader, [] for cd or an assignment, and None for
        any other command (its output is not file text).
    """
    if words[0] == "export":
        words = words[1:]
    while words and _ASSIGN_RE.match(words[0]):
        name, value = _ASSIGN_RE.match(words[0]).groups()
        env[name] = _expand(value, env) or "$"
        words = words[1:]
    if not words:
        return []
    if words[0] == "cd" and len(words) > 1:
        target = _expand(words[1], env)
        if target is not None:
            where[0] = os.path.normpath(os.path.join(where[0], target))
        return []
    reader = _READERS.get(words[0].rsplit("/", 1)[-1])
    if reader is None:
        return None

    reads = []
    for name, start, end in reader(words[1:]):
        path = _expand(name, env)
        if path is not None:
            full = os.path.normpath(os.path.join(where[0], path))
            reads.append(Read(full, start, end))
    return reads


def _bash_parse(command: str, cwd: str) -> tuple[list[Read], bool]:
    """A shell line's reads, and whether only readers print anything."""
    words = _words(command)
    if words is None:
        return [], False

    env: dict[str, str] = {}
    where = [cwd]
    reads: list[Read] = []
    pure = True
    for simple in _simple_commands(words):
        found = _run_command(simple, env, where)
        pure = pure and found is not None
        reads += found or []
    return reads, pure


def bash_reads(command: str, cwd: str) -> list[Read]:
    """The file lines a shell line prints, as far as its text shows.

    Args:
        command: The Bash call's command.
        cwd: The folder it ran in.

    Returns:
        One Read per file a reader (cat, nl, head, tail, sed, awk) names,
        in order. Words with an unknown variable are skipped, and a line
        that does not parse reads nothing.
    """
    return _bash_parse(command, cwd)[0]


@dataclass
class Implementer:
    """A subagent that edited files, as its transcript shows it.

    Attributes:
        transcript: Its transcript file.
        agent: Its agent id.
        prompt: The prompt it was given.
        cwd: The folder it started in.
        reads: What it read before its first edit, in order.
        texts: What a call printed of a file it read alone, by path; the
            first such call wins.
    """

    transcript: Path
    agent: str
    prompt: str
    cwd: str
    reads: list[Read] = field(default_factory=list)
    texts: dict[str, str] = field(default_factory=dict)


def _text(content: Any) -> str:
    """The text of a message or tool result (a string or text blocks)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    return "\n".join(
        str(part.get("text", ""))
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    )


def _results(recs: list[dict]) -> dict[str, tuple[str, dict | None]]:
    """Each tool result's text and Read's file record, by call id."""
    found = {}
    for rec in recs:
        extra = rec.get("toolUseResult")
        file = extra.get("file") if isinstance(extra, dict) else None
        for block in usage_audit._blocks(rec):
            if block.get("type") == "tool_result":
                text = _text(block.get("content"))
                found[str(block.get("tool_use_id"))] = (
                    text,
                    file if isinstance(file, dict) else None,
                )
    return found


def _calls_before_edit(recs: list[dict]) -> list[tuple[dict, str]] | None:
    """Tool calls before the first edit with their folder; None: no edit."""
    calls = []
    for rec in recs:
        for block in usage_audit._blocks(rec):
            if block.get("type") != "tool_use":
                continue
            args = block.get("input")
            command = args.get("command", "") if isinstance(args, dict) else ""
            if usage_audit.is_write(str(block.get("name")), str(command)):
                return calls
            calls.append((block, str(rec.get("cwd") or "")))
    return None


def _read_call(
    args: dict, result: tuple[str, dict | None]
) -> tuple[list[Read], str | None]:
    """What a Read call read, from its file record when there is one."""
    path = os.path.normpath(str(args.get("file_path", "")))
    _, file = result
    if file and isinstance(file.get("startLine"), int):
        start = file["startLine"]
        end = start + max(int(file.get("numLines") or 0), 1) - 1
        return [Read(path, start, end)], str(file.get("content", ""))

    start = int(args.get("offset") or 1)
    limit = args.get("limit")
    end = start + int(limit) - 1 if limit else None
    return [Read(path, start, end)], None


def _call_reads(
    block: dict, cwd: str, result: tuple[str, dict | None]
) -> tuple[list[Read], str | None]:
    """A call's reads, and its output when it read one file alone."""
    args = block.get("input")
    if not isinstance(args, dict):
        return [], None
    if block.get("name") == "Read":
        return _read_call(args, result)
    if block.get("name") != "Bash":
        return [], None

    reads, pure = _bash_parse(str(args.get("command", "")), cwd)
    alone = pure and len({read.path for read in reads}) == 1
    return reads, result[0] if alone else None


def read_implementer(path: Path) -> Implementer | None:
    """A subagent transcript as an implementer; None if it never edits."""
    recs = list(usage_audit.records(path))
    calls = _calls_before_edit(recs)
    if calls is None or not recs:
        return None

    first = recs[0]
    found = Implementer(
        transcript=path,
        agent=str(first.get("agentId") or path.stem),
        prompt=_text(first["message"].get("content")),
        cwd=str(first.get("cwd") or ""),
    )
    results = _results(recs)
    for block, cwd in calls:
        result = results.get(str(block.get("id")), ("", None))
        reads, text = _call_reads(block, cwd or found.cwd, result)
        found.reads += reads
        if text is not None:
            found.texts.setdefault(reads[0].path, text)
    return found


def _git(repo: Path, *args: str) -> str:
    """A git command's output in `repo`, "" when it fails."""
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return done.stdout if done.returncode == 0 else ""


class RepoMap:
    """Read paths as paths the replay repo tracks.

    A read in another checkout of the same project (a lane: a clone or
    worktree sharing the repo's root commit) maps to the same path here.
    """

    def __init__(self, repo: Path) -> None:
        self.repo = repo.resolve()
        self.tracked = set(_git(self.repo, "ls-files", "-z").split("\0"))
        self._roots = self._root_commits(self.repo)
        self._tops: dict[str, str | None] = {}
        self._same: dict[str, bool] = {}
        self._lines: dict[str, int] = {}

    @staticmethod
    def _root_commits(top: Path) -> set[str]:
        """The commits with no parent that `top`'s HEAD descends from."""
        found = _git(top, "rev-list", "--max-parents=0", "HEAD").split()
        return set(found)

    def _toplevel(self, folder: str) -> str | None:
        """The nearest folder at or above `folder` holding a `.git`."""
        if folder not in self._tops:
            parent = os.path.dirname(folder)
            if os.path.exists(os.path.join(folder, ".git")):
                self._tops[folder] = folder
            elif parent == folder:
                self._tops[folder] = None
            else:
                self._tops[folder] = self._toplevel(parent)
        return self._tops[folder]

    def _same_project(self, top: str) -> bool:
        """True for the replay repo and checkouts sharing its root."""
        if top not in self._same:
            here = Path(top).resolve()
            self._same[top] = here == self.repo or bool(
                self._roots & self._root_commits(here)
            )
        return self._same[top]

    def _deleted_root(self, folder: str) -> str | None:
        """The root of a deleted checkout of this repo holding `folder`.

        That is the highest missing folder at or above `folder`, when it
        sat beside the repo or inside it (where lanes live).
        """
        missing = None
        while not os.path.exists(folder):
            missing = folder
            parent = os.path.dirname(folder)
            if parent == folder:
                return None
            folder = parent
        if missing is None:
            return None

        here = os.path.realpath(folder)
        home = str(self.repo)
        beside = here == os.path.dirname(home)
        inside = here == home or here.startswith(home + os.sep)
        return missing if beside or inside else None

    def relative(self, path: str) -> str | None:
        """The tracked path a read names, or None for any other file."""
        folder = os.path.dirname(path)
        top = self._toplevel(folder)
        if top is not None and self._same_project(top):
            rel = os.path.relpath(path, top)
            if rel in self.tracked:
                return rel

        root = self._deleted_root(folder)
        rel = os.path.relpath(path, root) if root else None
        return rel if rel in self.tracked else None

    def line_count(self, rel: str) -> int:
        """Lines in a tracked file as it is now (0 when unreadable)."""
        if rel not in self._lines:
            try:
                with (self.repo / rel).open("rb") as handle:
                    self._lines[rel] = sum(1 for _ in handle)
            except OSError:
                self._lines[rel] = 0
        return self._lines[rel]


@dataclass
class Case:
    """One implementer's task, ready to scout, and what it read.

    Attributes:
        transcript: The implementer's transcript file.
        agent: Its agent id.
        question: The task text scout is given.
        read: Line spans read before the first edit, by tracked path.
        briefs: The files whose text joined the question.
    """

    transcript: str
    agent: str
    question: str
    read: dict[str, list[list[int]]]
    briefs: list[str]


QUESTION_CHARS = 12000  # most of a task's text scout is given

# A file name in a prompt that may be a brief (the task written out).
_BRIEF_RE = re.compile(r"[\w.~/-]+\.(?:md|txt)\b")


def _brief_paths(found: Implementer) -> list[str]:
    """Files the prompt names as .md or .txt that the implementer read."""
    read = {read.path for read in found.reads}
    named = []
    for word in _BRIEF_RE.findall(found.prompt):
        path = os.path.normpath(
            os.path.join(found.cwd, os.path.expanduser(word))
        )
        if path in read and path not in named:
            named.append(path)
    return named


def _brief_text(found: Implementer, path: str) -> str | None:
    """A brief's text: as the transcript shows it, else as it is now."""
    if path in found.texts:
        return found.texts[path]
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return None


def _span(read: Read, total: int) -> tuple[int, int] | None:
    """A read as lines of a file that now has `total` lines."""
    start = total + read.start + 1 if read.start < 0 else read.start
    end = total if read.end is None else min(read.end, total)
    start = max(start, 1)
    return (start, end) if start <= end else None


def _merge(spans: list[tuple[int, int]]) -> list[list[int]]:
    """Spans sorted, with touching and overlapping ones joined."""
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def build_case(found: Implementer, repo: RepoMap) -> Case:
    """An implementer as a replay case: its question and tracked reads."""
    briefs = _brief_paths(found)
    texts = [_brief_text(found, path) for path in briefs]
    question = "\n\n".join([found.prompt, *(t for t in texts if t)])
    spans: dict[str, list[tuple[int, int]]] = {}
    for read in found.reads:
        rel = None if read.path in briefs else repo.relative(read.path)
        span = _span(read, repo.line_count(rel)) if rel else None
        if rel and span:
            spans.setdefault(rel, []).append(span)
    return Case(
        transcript=str(found.transcript),
        agent=found.agent,
        question=question[:QUESTION_CHARS],
        read={rel: _merge(parts) for rel, parts in sorted(spans.items())},
        briefs=[repo.relative(path) or path for path in briefs],
    )


# An excerpt's heading in a pack: "## path:start-end (of N lines)...".
_EXCERPT_RE = re.compile(r"^## (.+):(\d+)-(\d+) \(of [\d,]+ lines\)")


def pack_spans(answer: str) -> dict[str, list[list[int]]] | None:
    """The line spans a scout's inline text holds, by path.

    Returns:
        {} when no text was added, None when the scout failed.
    """
    if answer.startswith("["):
        return None

    spans: dict[str, list[list[int]]] = {}
    body = False
    for line in answer.splitlines():
        body = body or line.startswith("[text:")
        match = _EXCERPT_RE.match(line) if body else None
        if match:
            path, start, end = match.groups()
            spans.setdefault(path, []).append([int(start), int(end)])
    return spans


@dataclass(frozen=True)
class Score:
    """How much of an implementer's reading one pack holds.

    Attributes:
        files_read: Tracked files read before the first edit.
        files_held: Those the pack holds any of.
        lines_read: Lines of those files read.
        lines_held: Those the pack holds.
        pack_lines: Lines the pack holds.
        pack_unread: Pack lines the implementer never read.
    """

    files_read: int
    files_held: int
    lines_read: int
    lines_held: int
    pack_lines: int
    pack_unread: int


def _lines(spans: dict[str, list[list[int]]]) -> set[tuple[str, int]]:
    """Every (path, line) the spans cover."""
    return {
        (path, number)
        for path, parts in spans.items()
        for start, end in parts
        for number in range(start, end + 1)
    }


def score(
    read: dict[str, list[list[int]]], pack: dict[str, list[list[int]]]
) -> Score:
    """Compare what was read with what the pack holds."""
    read_lines = _lines(read)
    pack_lines = _lines(pack)
    return Score(
        files_read=len(read),
        files_held=len(read.keys() & pack.keys()),
        lines_read=len(read_lines),
        lines_held=len(read_lines & pack_lines),
        pack_lines=len(pack_lines),
        pack_unread=len(pack_lines - read_lines),
    )


def implementers(
    paths: list[Path], kind: str | None, limit: int
) -> list[Implementer]:
    """The newest subagents in `paths` that edit, of one type if given."""
    newest = sorted(
        (path for path in paths if usage_audit.is_subagent(path)),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    found = []
    for path in newest:
        if len(found) >= limit:
            break
        if kind and usage_audit.agent_type(path) != kind:
            continue
        one = read_implementer(path)
        if one is not None:
            found.append(one)
    return found


Scout = Callable[[str], str]


def _load_rows(path: Path) -> dict[str, dict]:
    """Rows already in a results file, by transcript."""
    rows = {}
    if path.exists():
        for line in path.read_text().splitlines():
            row = json.loads(line)
            rows[row["transcript"]] = row
    return rows


def _scout_row(case: Case, scout: Scout) -> dict:
    """Scout one case and keep what scoring needs (no task text)."""
    began = time.monotonic()
    answer = scout(case.question)
    pack = pack_spans(answer)
    note = next(
        (line for line in answer.splitlines() if line.startswith("[text")),
        answer.splitlines()[0] if answer else "",
    )
    return {
        "transcript": case.transcript,
        "agent": case.agent,
        "briefs": case.briefs,
        "read": case.read,
        "pack": pack,
        "note": note,
        "seconds": round(time.monotonic() - began, 1),
    }


def run_cases(
    cases: list[Case], scout: Scout, results: Path, jobs: int
) -> None:
    """Scout each case and append its row to `results` as it finishes."""
    results.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max(1, jobs)) as pool:
        futures = [pool.submit(_scout_row, case, scout) for case in cases]
        for future in as_completed(futures):
            with results.open("a") as handle:
                handle.write(json.dumps(future.result()) + "\n")


GO_SHARE = 60.0  # files held, in percent, to go on to the pilot

STOP_SHARE = 40.0  # under this, scout is not finding what is read


def _percent(part: int, whole: int) -> float:
    return 100 * part / whole if whole else 0.0


def _verdict(held: int, read: int) -> str:
    """The gate's answer for the share of files held."""
    share = _percent(held, read)
    if share >= GO_SHARE:
        return f"go: at or over the {GO_SHARE:.0f}% bar; run the pilot."
    if share < STOP_SHARE:
        return (
            f"stop: under {STOP_SHARE:.0f}%; scout is not finding what "
            "implementers read."
        )

    return (
        f"between {STOP_SHARE:.0f}% and {GO_SHARE:.0f}%: look at what it "
        "misses (--files) before deciding."
    )


def _row_line(row: dict, mark: Score | None) -> str:
    """One case in the table."""
    if mark is None:
        cells = f"{'scout failed: ' + row['note'][:50]:>41}"
    else:
        cells = (
            f"{mark.files_read:>6} {mark.files_held:>5} "
            f"{mark.lines_read:>6} {mark.lines_held:>5} "
            f"{mark.pack_lines:>5} {mark.pack_unread:>6}"
        )
    return f"  {row['agent'][:17]:17} {cells} {row['seconds']:>6.0f}"


def _files_lines(row: dict) -> list[str]:
    """What a case read that the pack missed, and what it held unread."""
    pack = row["pack"] or {}
    missed = sorted(set(row["read"]) - set(pack))
    unread = sorted(set(pack) - set(row["read"]))
    return [
        f"  {row['agent'][:17]}",
        f"    missed: {', '.join(missed) or '-'}",
        f"    held, never read: {', '.join(unread) or '-'}",
    ]


def _total_lines(marks: list[Score], failed: int, empty: int) -> list[str]:
    """The pooled shares, what was not measured, and the verdict."""
    skipped = f"{failed} scout failed, {empty} read no tracked file"
    if not marks:
        return [f"nothing measured: {skipped}"]

    total = Score(*(sum(column) for column in zip(*map(astuple, marks))))
    shares = [_percent(m.files_held, m.files_read) for m in marks]

    def share(part: int, whole: int) -> str:
        return f"{part:,} of {whole:,} ({_percent(part, whole):.1f}%)"

    return [
        f"files held: {share(total.files_held, total.files_read)}; "
        f"median per task {statistics.median(shares):.1f}%",
        f"lines held: {share(total.lines_held, total.lines_read)}",
        "pack lines nobody read: "
        f"{share(total.pack_unread, total.pack_lines)}",
        f"not measured: {skipped}",
        "",
        f"verdict: {_verdict(total.files_held, total.files_read)}",
    ]


def render(rows: list[dict], repo: Path, show_files: bool) -> str:
    """The replay as a text report."""
    head = [
        f"Pack replay: {len(rows)} implementer(s), scouted in {repo}",
        "Lines are approximate: the tree has moved since these ran.",
        "",
        f"  {'agent':17} {'files':>6} {'held':>5} {'lines':>6} {'held':>5} "
        f"{'pack':>5} {'unread':>6} {'secs':>6}",
    ]
    marks = []
    table = []
    detail = []
    for row in rows:
        mark = (
            score(row["read"], row["pack"])
            if row["pack"] is not None
            else None
        )
        table.append(_row_line(row, mark))
        if mark is not None and mark.files_read:
            marks.append(mark)
            detail += _files_lines(row) if show_files else []
    failed = sum(row["pack"] is None for row in rows)
    empty = len(rows) - failed - len(marks)
    totals = _total_lines(marks, failed, empty)
    return "\n".join([*head, *table, "", *totals, *detail])


def _load_castor_env() -> None:
    """Take NIM settings from Castor's registration when unset here.

    The keys stay in this process; nothing is printed.
    """
    try:
        config = json.loads((Path.home() / ".claude.json").read_text())
        env = config["mcpServers"]["claude-castor"].get("env") or {}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return
    for name, value in env.items():
        os.environ.setdefault(str(name), str(value))


def _castor_scout(repo: Path, model: str | None, tokens: int | None) -> Scout:
    """nim_scout with text="inline" on `repo`, run in this process."""
    _load_castor_env()
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from castor.nim.roles import NIM_SCOUT_STEPS
    from castor.nim.roles import _dispatch_scout
    from castor.nim.roles import _scout_task

    def scout(question: str) -> str:
        return _dispatch_scout(
            _scout_task(question),
            str(repo),
            model,
            NIM_SCOUT_STEPS,
            question,
            "inline",
            tokens,
        )

    return scout


def _default_results() -> Path:
    """A fresh results file beside Castor's other caches."""
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return root / "claude-castor" / "pack-replay" / f"results-{stamp}.jsonl"


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--repo", type=Path, required=True, help="the checkout to scout"
    )
    parser.add_argument(
        "--project",
        help="transcript folders whose name contains this (default: the "
        "repo's folder name)",
    )
    parser.add_argument("--root", type=Path, default=usage_audit.DEFAULT_ROOT)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--agent-type", help="e.g. nqc-implementer")
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--model", help="NIM model id for the scout")
    parser.add_argument("--tokens", type=int, help="pack budget (6000)")
    parser.add_argument(
        "--results",
        type=Path,
        help="JSONL of scouted cases; an existing one is resumed",
    )
    parser.add_argument(
        "--files",
        action="store_true",
        help="list each task's missed files and unread pack files",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Replay recent implementers through scout and print the report."""
    args = _arguments(argv)
    repo = args.repo.expanduser().resolve()
    project = args.project or repo.name
    paths = usage_audit.transcripts(args.root, args.days, project)
    found = implementers(paths, args.agent_type, args.limit)
    repo_map = RepoMap(repo)
    cases = [build_case(one, repo_map) for one in found]
    results = (args.results or _default_results()).expanduser()
    done = _load_rows(results)
    pending = [case for case in cases if case.transcript not in done]
    if pending:
        scout = _castor_scout(repo, args.model, args.tokens)
        run_cases(pending, scout, results, args.jobs)
        done = _load_rows(results)
    rows = [done[case.transcript] for case in cases]
    print(render(rows, repo, args.files))
    print(f"\nresults: {results}")


if __name__ == "__main__":
    main()
