"""Local NIM usage records and how long they are kept."""

import json
import os
import shutil
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from castor.common import (
    _cache_dir,
)
from castor.nim.config import (
    NIM_RECORDS_TTL_DAYS,
    NIM_TRACES_DIR,
    NIM_USAGE_DIR,
)


_nim_records_lock = threading.Lock()  # usage and trace files


def _nim_records_on() -> bool:
    """True unless CASTOR_NIM_TRACES=0 turns off usage and trace files."""
    return os.environ.get("CASTOR_NIM_TRACES", "1").strip() != "0"


def _nim_usage_record(
    payload: dict | None, data: Any, failure: Any, started: float
) -> None:
    """Append one chat request to today's usage file (best effort).

    One JSON line per attempt under <cache>/nim-usage/, no prompt text:
    model, outcome, seconds and token counts, for nim_usage.
    """
    if not _nim_records_on():
        return
    usage = data.get("usage") if isinstance(data, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    entry = {
        "t": round(time.time(), 1),
        "model": (payload or {}).get("model"),
        "ok": failure is None,
        "kind": None if failure is None else failure.kind,
        "seconds": round(time.monotonic() - started, 2),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
    }
    folder = _cache_dir() / NIM_USAGE_DIR
    target = folder / f"{time.strftime('%Y-%m-%d')}.jsonl"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            _nim_prune_records(folder)
        with _nim_records_lock, target.open("a") as out:
            out.write(json.dumps(entry) + "\n")
    except OSError:
        return


def _nim_prune_records(folder: Path) -> None:
    """Delete usage and trace entries older than NIM_RECORDS_TTL_DAYS."""
    cutoff = time.time() - NIM_RECORDS_TTL_DAYS * 86400
    try:
        for entry in folder.iterdir():
            if entry.stat().st_mtime < cutoff:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink()
    except OSError:
        return


def _nim_usage_entries(days: int) -> list[dict]:
    """Usage-file entries from the last `days` days, each tagged with day."""
    folder = _cache_dir() / NIM_USAGE_DIR
    cutoff = time.time() - days * 86400
    entries: list[dict] = []
    for path in sorted(folder.glob("*.jsonl")) if folder.is_dir() else []:
        for raw in path.read_text(errors="replace").splitlines():
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("t", 0) >= cutoff:
                entry["day"] = path.stem
                entries.append(entry)
    return entries


def _nim_trace_days(days: int) -> dict[str, list[dict]]:
    """Agent traces from the last `days` days, grouped by YYYY-MM-DD."""
    folder = _cache_dir() / NIM_TRACES_DIR
    cutoff = time.time() - days * 86400
    grouped: dict[str, list[dict]] = {}
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        if path.stat().st_mtime < cutoff:
            continue
        try:
            trace = json.loads(path.read_text(errors="replace"))
        except ValueError:
            continue
        day = f"{path.name[:4]}-{path.name[4:6]}-{path.name[6:8]}"
        grouped.setdefault(day, []).append(trace)
    return grouped


def _nim_usage_line(rows: list[dict], runs: list[dict] | None) -> str:
    """One summary line: requests, failures, tokens, time, agent runs."""
    failed = Counter(str(r.get("kind")) for r in rows if not r.get("ok"))
    tokens_in = sum(r.get("prompt_tokens") or 0 for r in rows)
    tokens_out = sum(r.get("completion_tokens") or 0 for r in rows)
    seconds = sum(r.get("seconds") or 0 for r in rows)
    parts = [f"{len(rows)} requests"]
    if failed:
        parts.append(
            "failed " + ", ".join(f"{k} {n}" for k, n in failed.items())
        )
    parts.append(f"tokens {tokens_in:,} in / {tokens_out:,} out")
    if rows:
        parts.append(f"avg {seconds / len(rows):.1f}s")
    if runs:
        good = sum(1 for r in runs if r.get("ok"))
        mean = sum(r.get("seconds") or 0 for r in runs) / len(runs)
        parts.append(f"agent runs {len(runs)} ({good} ok, avg {mean:.0f}s)")
    return "; ".join(parts)
