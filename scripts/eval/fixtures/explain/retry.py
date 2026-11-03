"""Retry helpers for an HTTP client (eval fixture with a planted bug)."""

import random
import time

BACKOFF_BASE = 2.0
BACKOFF_MAX = 30.0
BACKOFF_JITTER = 1.0


def parse_retry_after(value):
    """Parse a numeric Retry-After header (HTTP-date form is ignored)."""
    try:
        seconds = float((value or "").strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def backoff(attempt, retry_after):
    """Exponential backoff with jitter; honors a Retry-After floor."""
    base = min(BACKOFF_MAX, BACKOFF_BASE * 2 ** (attempt - 1))
    delay = base + random.uniform(0, BACKOFF_JITTER)
    return max(delay, retry_after)


def pause_before_retry(status, headers, attempt):
    """Sleep before the next attempt of a failed request."""
    retry_after = parse_retry_after(headers.get("retry-after"))
    delay = backoff(attempt, retry_after)
    time.sleep(min(delay, BACKOFF_MAX))


def send_with_retry(send, attempts=4):
    """Call send() until it returns 200 or attempts run out."""
    for attempt in range(1, attempts + 1):
        status, headers, body = send()
        if status == 200:
            return body
        if status not in (429, 500, 502, 503) or attempt == attempts:
            raise RuntimeError(f"request failed with {status}")
        pause_before_retry(status, headers, attempt)
    return None


if __name__ == "__main__":
    replies = iter([(503, {}, ""), (200, {}, "ok")])
    print(send_with_retry(lambda: next(replies)))
