"""Collapse repeated log hits (eval fixture with a planted bug)."""

import re

# Leading line numbers, timestamps and whitespace, ignored when deciding
# whether two hits repeat.
LOG_PREFIX_RE = re.compile(r"^[\s\[\]\d:.,/T+-]*")


def collapse_repeats(lines, hits):
    """Group hits that repeat: [(first line, last line, count)], in order.

    Two hits repeat when their text matches once leading numbers and
    timestamps are stripped, so "12:00:01 FAIL x" repeats "FAIL x".
    """
    groups = {}
    for n in hits:
        key = lines[n - 1].strip()
        groups.setdefault(key, []).append(n)
    return [(ns[0], ns[-1], len(ns)) for ns in groups.values()]
