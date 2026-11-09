"""nim_summarize: split large input, summarize the parts, merge them."""

import fnmatch
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from castor.app import (
    mcp,
)
from castor.common import (
    SKIP_DIRS,
    SKIP_EXTENSIONS,
    _is_binary_bytes,
    _load_gitignore,
    _parse_summary,
    _read_bytes,
)
from castor.nim.backend import (
    _dispatch_nim,
)
from castor.nim.config import (
    NIM_DEFAULT_MAX_INPUT_CHARS,
    NIM_MERGE_SUMMARIES_PROMPT,
    NIM_SUMMARIZE_CHUNK_CHARS,
    NIM_SUMMARIZE_MAX_CHARS,
    NIM_SUMMARIZE_MAX_TOKENS,
    NIM_SUMMARIZE_MERGE_CHARS,
    NIM_SUMMARIZE_MERGE_POINTS,
    NIM_SUMMARIZE_PART_POINTS,
    NIM_SUMMARIZE_PROMPT,
    NIM_SUMMARIZE_THINKING,
    NIM_SUMMARIZE_WORKERS,
    _nim_env,
)


@mcp.tool()
def nim_summarize(cwd: str, target: str, model: str | None = None) -> str:
    """Summarize a file or directory with NIM into compact JSON.

    Castor reads `target` (a file or directory under cwd) and inlines it.
    A target over one request's budget (about 240k chars) is split on line
    boundaries, each part summarized in parallel, then merged into one
    summary. Targets over 2,000,000 chars are refused; pass a narrower
    target or use gemini_summarize.

    Args:
        cwd: Absolute path to the project root.
        target: A file or directory path under cwd (absolute, or relative
            to cwd).
        model: NIM model id (see nim_models; default Nemotron 3.5 Super).

    Returns:
        A JSON string with keys: summary, key_points. On parse failure,
        {"summary": <raw>, "key_points": []}. On error, a bracketed
        error string.
    """
    root = Path(cwd).resolve()
    path = Path(target)
    path = (path if path.is_absolute() else root / path).resolve()
    if path != root and root not in path.parents:
        return f"[Error: target must be inside cwd ({root}).]"
    if not path.exists():
        return f"[Error: target not found: {path}]"
    blocks, error = _summary_blocks(path, root, _summary_chunk_chars())
    if error:
        return error
    chunks = _pack_chunks(blocks, _summary_chunk_chars())
    if len(chunks) <= 1:
        raw = _summarize_part(target, chunks[0] if chunks else "", model)
        if raw.startswith("["):  # a summary is a JSON object, never "[..."
            return raw
        return _tidy_summary(_parse_summary(raw))
    return _summarize_in_parts(target, chunks, model)


def _tidy_summary(summary: str) -> str:
    """Split key points the model packed into one string, one per line.

    The merge has returned every bullet in a single newline-joined string;
    this restores one list entry per bullet and drops bullet markers.
    """
    data = json.loads(summary)
    points = data.get("key_points")
    if isinstance(points, str):
        points = [points]
    if not isinstance(points, list):
        return summary
    lines = (
        re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s+", "", line).strip()
        for point in points
        for line in str(point).splitlines()
    )
    data["key_points"] = [line for line in lines if line]
    return json.dumps(data)


def _summary_chunk_chars() -> int:
    """Content chars per summarize request.

    NIM_SUMMARIZE_CHUNK_CHARS, or less when CASTOR_NIM_MAX_INPUT_CHARS is
    lower (leaving room for the prompt).
    """
    limit = int(
        _nim_env("CASTOR_NIM_MAX_INPUT_CHARS", NIM_DEFAULT_MAX_INPUT_CHARS)
    )
    return max(1000, min(NIM_SUMMARIZE_CHUNK_CHARS, limit - 5000))


def _summary_files(path: Path) -> list[Path]:
    """Files a summary covers: `path` itself, or a filtered walk of it.

    The walk skips what _inline_directory skips (gitignore, SKIP_DIRS,
    SKIP_EXTENSIONS) but has no per-file or total size cap.
    """
    if path.is_file():
        return [path]
    patterns = _load_gitignore(path)
    files: list[Path] = []
    for base, dirs, names in os.walk(path):
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in SKIP_DIRS
            and not any(fnmatch.fnmatch(d, p.rstrip("/")) for p in patterns)
        )
        for name in sorted(names):
            file_path = Path(base) / name
            rel = str(file_path.relative_to(path))
            ignored = any(
                fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p)
                for p in patterns
            )
            if not ignored and file_path.suffix not in SKIP_EXTENSIONS:
                files.append(file_path)
    return files


def _summary_blocks(
    path: Path, root: Path, size: int
) -> tuple[list[tuple[str, str]], str | None]:
    """(label, text) blocks for a summary, each at most `size` chars.

    Returns (blocks, None), or ([], error) past NIM_SUMMARIZE_MAX_CHARS;
    the ceiling is checked on file sizes before anything is read.
    """
    files = _summary_files(path)
    total = sum(f.stat().st_size for f in files)
    if total > NIM_SUMMARIZE_MAX_CHARS:
        return [], (
            f"[Error: {path} holds {total:,} bytes, over the "
            f"{NIM_SUMMARIZE_MAX_CHARS:,}-char nim_summarize ceiling. Pass "
            "a narrower target, or use gemini_summarize.]"
        )
    blocks: list[tuple[str, str]] = []
    for file_path in files:
        data = _read_bytes(file_path)
        if data is None or _is_binary_bytes(data):
            continue
        label = str(file_path.relative_to(root))
        text = data.decode("utf-8", errors="replace")
        blocks.extend(_split_block(label, text, size - 200))
    return blocks, None


def _split_block(label: str, text: str, size: int) -> list[tuple[str, str]]:
    """One block, or "label (lines A-B)" blocks of at most `size` chars."""
    if len(text) <= size:
        return [(label, text)]
    pieces: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        pieces.extend(
            (number, line[i : i + size]) for i in range(0, len(line), size)
        )
    blocks: list[tuple[str, str]] = []
    start, buffer = 1, ""
    for number, piece in pieces:
        if buffer and len(buffer) + len(piece) > size:
            blocks.append((f"{label} (lines {start}-{number - 1})", buffer))
            start, buffer = number, ""
        buffer += piece
    last = pieces[-1][0]
    blocks.append((f"{label} (lines {start}-{last})", buffer))
    return blocks


def _pack_chunks(blocks: list[tuple[str, str]], size: int) -> list[str]:
    """Pack labeled blocks, in order, into chunks of at most `size` chars."""
    chunks: list[str] = []
    current = ""
    for label, text in blocks:
        block = f"[FILE: {label}]\n{text}\n"
        if current and len(current) + len(block) > size:
            chunks.append(current)
            current = ""
        current += block
    if current:
        chunks.append(current)
    return chunks


def _summarize_part(
    target: str, content: str, model: str | None, part: str = ""
) -> str:
    """One summarize request (cached); `part` names it, e.g. "2 of 3"."""
    note = (
        f"\nPart {part} of {target}; summarize this part only, in at most "
        f"{NIM_SUMMARIZE_PART_POINTS} key points.\n"
        if part
        else ""
    )
    prompt = (
        f"{NIM_SUMMARIZE_PROMPT.format(target=target)}\n{note}\n"
        f"[CONTENT]\n{content}"
    )
    return _summary_request(prompt, model)


def _summary_request(prompt: str, model: str | None) -> str:
    """Send one summarize or merge prompt with the summary token budget."""
    return _dispatch_nim(
        prompt,
        raw=True,
        model=model,
        max_tokens=NIM_SUMMARIZE_MAX_TOKENS,
        thinking=NIM_SUMMARIZE_THINKING,
    )


def _summarize_in_parts(
    target: str, chunks: list[str], model: str | None
) -> str:
    """Summarize each chunk in parallel, then merge them into one summary.

    Any chunk error is returned for the whole call, before any merge.
    """
    count = len(chunks)
    workers = min(count, NIM_SUMMARIZE_WORKERS)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        raws = list(
            pool.map(
                lambda item: _summarize_part(
                    target, item[1], model, f"{item[0]} of {count}"
                ),
                enumerate(chunks, 1),
            )
        )
    for raw in raws:
        if raw.startswith("["):
            return raw
    parts = [json.loads(_tidy_summary(_parse_summary(raw))) for raw in raws]
    return _merge_summaries(target, parts, model)


def _merge_summaries(target: str, parts: list[dict], model: str | None) -> str:
    """Merge per-part summaries into one {summary, key_points} JSON.

    Past NIM_SUMMARIZE_MERGE_CHARS the merge gets the key points only.
    """
    body = json.dumps(parts, indent=1)
    if len(body) > NIM_SUMMARIZE_MERGE_CHARS:
        points = [{"key_points": p.get("key_points", [])} for p in parts]
        body = json.dumps(points, indent=1)[:NIM_SUMMARIZE_MERGE_CHARS]
    header = NIM_MERGE_SUMMARIES_PROMPT.format(
        target=target, parts=len(parts), points=NIM_SUMMARIZE_MERGE_POINTS
    )
    prompt = f"{header}\n\n[PARTS]\n{body}\n[/PARTS]"
    raw = _summary_request(prompt, model)
    if raw.startswith("["):
        return raw
    return _tidy_summary(_parse_summary(raw))
