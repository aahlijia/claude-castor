"""Tests for the NVIDIA NIM backend, run against a local stub server.

No network and no real API key: CASTOR_NIM_BASE_URL points at a loopback
HTTP server that replays scripted responses and records every request.
"""

import importlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
server = importlib.import_module("server")

KEY = "nvapi-test-secret-key"


def _tool(tool):
    """Unwrap a @mcp.tool() object (plain function or FunctionTool)."""
    return getattr(tool, "fn", tool)


def _completion(text, finish="stop"):
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish,
            }
        ]
    }


def _write_sse(handler, spec):
    """Stream spec["__sse__"] chunks, pausing after chunk `stall_after`."""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.end_headers()
    try:
        for i, chunk in enumerate(spec["__sse__"], 1):
            handler.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            handler.wfile.flush()
            if spec.get("stall_after") == i:
                time.sleep(spec.get("stall", 1.0))
        handler.wfile.write(b"data: [DONE]\n\n")
    except OSError:  # the client gave up on a stall
        pass


class Stub:
    """Scripted OpenAI-compatible endpoint on a loopback port."""

    def __init__(self):
        self.queue = []  # (status, headers, body) consumed per POST
        self.requests = []  # (method, path, headers, parsed body)
        self.models = [
            "nvidia/nemotron-3.5-super-vl-120b-a12b",
            "nvidia/nemotron-x",
        ]
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, headers, body):
                if isinstance(body, dict) and "__sse__" in body:
                    _write_sse(self, body)
                    return
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                stub.requests.append(
                    ("GET", self.path, dict(self.headers), None)
                )
                data = [{"id": m} for m in stub.models]
                self._reply(200, {}, {"data": data})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                stub.requests.append(
                    ("POST", self.path, dict(self.headers), body)
                )
                if stub.queue:
                    status, headers, payload = stub.queue.pop(0)
                else:
                    status, headers, payload = (
                        200,
                        {},
                        _completion("stub answer"),
                    )
                self._reply(status, headers, payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def posts(self):
        return [r for r in self.requests if r[0] == "POST"]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def stub(tmp_path, monkeypatch):
    s = Stub()
    monkeypatch.setenv("CASTOR_NIM_BASE_URL", s.url)
    monkeypatch.setenv("NVIDIA_API_KEY", KEY)
    monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
    monkeypatch.delenv("CASTOR_NIM_MODEL", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("CASTOR_NIM_DEKKO", "0")
    # Fast, deterministic backoff.
    monkeypatch.setattr(server, "NIM_BACKOFF_BASE", 0.01)
    monkeypatch.setattr(server, "NIM_BACKOFF_JITTER", 0.0)

    real_run = server.subprocess.run

    def no_agy(*args, **kwargs):
        if args and args[0][:1] == ["git"]:  # the agent's file map
            return real_run(*args, **kwargs)
        raise AssertionError(f"agy/subprocess must not run: {args}")

    monkeypatch.setattr(server.subprocess, "run", no_agy)
    server._nim_reset_state()
    yield s
    server._nim_reset_state()
    s.close()


@pytest.fixture
def fake_dekko(tmp_path, monkeypatch):
    """Pretend dekko is installed; `_dekko_run` pops queued results."""

    class Fake:
        queue: list = []
        calls: list = []

    fake = Fake()
    fake.queue = []
    fake.calls = []

    def run(root, args, timeout=10.0):
        fake.calls.append(args)
        return fake.queue.pop(0) if fake.queue else (True, "")

    monkeypatch.setenv("CASTOR_NIM_DEKKO", "1")
    monkeypatch.setattr(server, "_dekko_bin", lambda: tmp_path / "dekko")
    monkeypatch.setattr(server, "_dekko_version", lambda: (1, 9, 35))
    monkeypatch.setattr(server, "_dekko_run", run)
    return fake


# --- classification -------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "kind", "retryable"),
    [
        (429, "rate_limit", True),
        (500, "upstream", True),
        (503, "upstream", True),
        (529, "upstream", True),
        (401, "auth", False),
        (402, "billing", False),
        (403, "permission", False),
        (404, "not_found", False),
        (400, "invalid", False),
        (422, "invalid", False),
        (413, "context", False),
        (418, "other", False),
    ],
)
def test_classify_status(status, kind, retryable):
    failure = server._classify_nim_status(status, "boom")
    assert (failure.kind, failure.retryable) == (kind, retryable)


def test_classify_context_window_on_400():
    failure = server._classify_nim_status(
        400, "This model's maximum context length is 8192 tokens"
    )
    assert failure.kind == "context"
    assert not failure.retryable


def test_parse_retry_after():
    assert server._parse_retry_after("7") == 7.0
    assert server._parse_retry_after("0") == 0.0
    assert server._parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert server._parse_retry_after(None) is None
    assert server._parse_retry_after("-3") is None


def test_error_detail_shapes():
    assert server._nim_error_detail('{"detail": "bad"}') == "bad"
    assert server._nim_error_detail('{"error": {"message": "nope"}}') == "nope"
    assert server._nim_error_detail("plain text") == "plain text"
    assert "loc" in server._nim_error_detail('{"detail": [{"loc": ["x"]}]}')


# --- limiter --------------------------------------------------------------


def test_sliding_window_limiter():
    limiter = server._SlidingWindowLimiter(2, 0.2)
    assert limiter.reserve() == 0.0
    assert limiter.reserve() == 0.0
    wait = limiter.reserve()
    assert 0 < wait <= 0.2
    time.sleep(0.25)
    assert limiter.reserve() == 0.0


def test_admit_fails_fast_when_wait_too_long(monkeypatch):
    monkeypatch.setattr(server, "NIM_MAX_WAIT", 0.05)
    server._nim_reset_state()
    server._nim_set_cooldown(30, KEY)
    key, err = server._nim_admit([KEY])
    assert key is None
    assert err and err.startswith("[NVIDIA NIM is rate-limited")
    server._nim_reset_state()


# --- chat -----------------------------------------------------------------


def test_prompt_success_sends_expected_request(stub):
    out = _tool(server.nim_prompt)("hello", use_cache=False)
    assert out == "stub answer"
    (_, path, headers, body) = stub.posts()[0]
    assert path == "/v1/chat/completions"
    assert headers["Authorization"] == f"Bearer {KEY}"
    assert body["model"] == server.NIM_DEFAULT_MODEL
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["messages"][0] == {
        "role": "system",
        "content": server.SYSTEM_INSTRUCTION,
    }
    assert body["messages"][1]["role"] == "user"
    assert "hello" in body["messages"][1]["content"]


def test_plain_call_caps_nemotron_reasoning_at_half_the_cap(stub):
    _tool(server.nim_prompt)("q", use_cache=False)
    assert stub.posts()[0][3]["reasoning_budget"] == (
        server.NIM_DEFAULT_MAX_TOKENS // 2
    )
    _tool(server.nim_prompt)("q", use_cache=False, max_tokens=50_000)
    assert stub.posts()[1][3]["reasoning_budget"] == (
        server.NIM_PROMPT_THINKING
    )
    _tool(server.nim_prompt)("q", use_cache=False, model="vendor/other")
    assert "reasoning_budget" not in stub.posts()[2][3]


def test_raw_skips_system_message(stub):
    _tool(server.nim_prompt)("hi", raw=True, use_cache=False)
    messages = stub.posts()[0][3]["messages"]
    assert [m["role"] for m in messages] == ["user"]


def test_model_resolution_order(stub, monkeypatch):
    _tool(server.nim_prompt)("a", use_cache=False)
    monkeypatch.setenv("CASTOR_NIM_MODEL", "env/model")
    _tool(server.nim_prompt)("b", use_cache=False)
    _tool(server.nim_prompt)("c", model="arg/model", use_cache=False)
    models = [r[3]["model"] for r in stub.posts()]
    assert models == [
        "nvidia/nemotron-3.5-super-vl-120b-a12b",
        "env/model",
        "arg/model",
    ]


def test_files_are_inlined(stub, tmp_path):
    src = tmp_path / "a.py"
    src.write_text("print('inlined-marker')\n")
    _tool(server.nim_prompt)("explain", files=[str(src)], use_cache=False)
    user = stub.posts()[0][3]["messages"][-1]["content"]
    assert "inlined-marker" in user and f"[FILE: {src}]" in user


def test_missing_key(stub, monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    out = _tool(server.nim_prompt)("hi")
    assert out.startswith("[NVIDIA NIM is not configured")
    assert not stub.requests


def test_alternate_key_env_var(stub, monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nvapi-alt")
    _tool(server.nim_prompt)("hi", use_cache=False)
    assert stub.posts()[0][2]["Authorization"] == "Bearer nvapi-alt"


def test_cache_hit_skips_second_request(stub):
    first = _tool(server.nim_prompt)("same question")
    second = _tool(server.nim_prompt)("same question")
    assert first == second == "stub answer"
    assert len(stub.posts()) == 1


def test_cache_key_varies_with_model(stub):
    _tool(server.nim_prompt)("q", model="m/one")
    _tool(server.nim_prompt)("q", model="m/two")
    assert len(stub.posts()) == 2


def test_bracket_prefixed_answer_is_a_success(stub):
    stub.queue.append((200, {}, _completion("[1] first item, [2] second")))
    out = _tool(server.nim_prompt)("list things")
    assert out.startswith("[1]")
    # Cached, because it was a real answer despite the leading bracket.
    _tool(server.nim_prompt)("list things")
    assert len(stub.posts()) == 1


def test_think_blocks_stripped(stub):
    stub.queue.append(
        (
            200,
            {},
            _completion("<think>secret chain\nof thought</think>\nfinal"),
        )
    )
    assert _tool(server.nim_prompt)("q", use_cache=False) == "final"


def test_truncation_note(stub):
    stub.queue.append((200, {}, _completion("partial", finish="length")))
    out = _tool(server.nim_prompt)("q", use_cache=False)
    assert out.startswith("partial") and "truncated at max_tokens" in out


def test_empty_content_is_error(stub):
    stub.queue.append((200, {}, _completion("", finish="length")))
    out = _tool(server.nim_prompt)("q", use_cache=False)
    assert out.startswith("[NVIDIA NIM returned no content")


def test_input_budget_enforced(stub, monkeypatch):
    monkeypatch.setenv("CASTOR_NIM_MAX_INPUT_CHARS", "100")
    out = _tool(server.nim_prompt)("x" * 500, use_cache=False)
    assert "input budget" in out
    assert not stub.requests


@pytest.mark.parametrize("temperature", [-0.1, 2.5])
def test_bad_temperature(stub, temperature):
    out = _tool(server.nim_prompt)("q", temperature=temperature)
    assert out.startswith("[Error: temperature")
    assert not stub.requests


# --- failures and retries -------------------------------------------------


def test_429_then_success_retries(stub):
    stub.queue.append((429, {"Retry-After": "0"}, {"detail": "slow down"}))
    out = _tool(server.nim_prompt)("q", use_cache=False)
    assert out == "stub answer"
    assert len(stub.posts()) == 2
    assert server._nim_stats["rate_limited"] == 1
    assert server._nim_stats["ok"] == 1


def test_persistent_429_gives_up_after_max_attempts(stub):
    for _ in range(10):
        stub.queue.append((429, {"Retry-After": "0"}, {"detail": "no"}))
    out = _tool(server.nim_prompt)("q", use_cache=False)
    assert out.startswith("[NVIDIA NIM error — rate limited")
    assert len(stub.posts()) == server.NIM_MAX_ATTEMPTS


def test_500_retried_then_success(stub):
    stub.queue.append((503, {}, {"detail": "overloaded"}))
    stub.queue.append((502, {}, {"detail": "bad gateway"}))
    assert _tool(server.nim_prompt)("q", use_cache=False) == "stub answer"
    assert len(stub.posts()) == 3


@pytest.mark.parametrize(
    ("status", "needle"),
    [
        (401, "API key rejected"),
        (402, "credits or quota exhausted"),
        (403, "access denied"),
        (404, "nim_models"),
        (400, "invalid request"),
    ],
)
def test_fatal_status_not_retried(stub, status, needle):
    stub.queue.append((status, {}, {"detail": "nope"}))
    out = _tool(server.nim_prompt)("q", use_cache=False)
    assert out.startswith("[NVIDIA NIM error") and needle in out
    assert len(stub.posts()) == 1


def test_error_responses_are_not_cached_or_sessioned(stub, tmp_path):
    stub.queue.append((401, {}, {"detail": "nope"}))
    _tool(server.nim_prompt)("q", cwd=str(tmp_path), session="s1")
    assert not server._session_path(str(tmp_path), "s1").exists()
    stub.queue.append((401, {}, {"detail": "nope"}))
    _tool(server.nim_prompt)("q")
    assert _tool(server.nim_prompt)("q") == "stub answer"  # not a cached error


def test_api_key_is_redacted_from_errors(stub):
    stub.queue.append((400, {}, {"detail": f"bad token {KEY} rejected"}))
    out = _tool(server.nim_prompt)("q", use_cache=False)
    assert KEY not in out and "***" in out


def test_429_sets_cooldown_that_fails_fast(stub, monkeypatch):
    monkeypatch.setattr(server, "NIM_MAX_WAIT", 0.05)
    for _ in range(10):
        stub.queue.append((429, {"Retry-After": "120"}, {"detail": "no"}))
    out = _tool(server.nim_prompt)("q", use_cache=False)
    assert "rate-limited" in out
    before = len(stub.posts())
    again = _tool(server.nim_prompt)("q2", use_cache=False)
    assert "rate-limited" in again
    assert len(stub.posts()) == before  # cooldown blocked the second call


def test_network_error_is_reported(stub, monkeypatch):
    monkeypatch.setenv("CASTOR_NIM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(server, "NIM_MAX_ATTEMPTS", 2)
    out = _tool(server.nim_prompt)("q", use_cache=False)
    assert out.startswith("[NVIDIA NIM error — could not reach")


def test_non_https_base_url_refused(stub, monkeypatch):
    monkeypatch.setenv("CASTOR_NIM_BASE_URL", "http://example.com/v1")
    out = _tool(server.nim_prompt)("q", use_cache=False)
    assert "must use https" in out
    assert not stub.requests


# --- sessions -------------------------------------------------------------


def test_session_requires_cwd(stub):
    out = _tool(server.nim_prompt)("q", session="s")
    assert out.startswith("[Error: cwd is required")


def test_session_replays_history(stub, tmp_path):
    cwd = str(tmp_path)
    _tool(server.nim_prompt)("first question", cwd=cwd, session="s")
    _tool(server.nim_prompt)("second question", cwd=cwd, session="s")
    second_user = stub.posts()[1][3]["messages"][-1]["content"]
    assert "first question" in second_user and "stub answer" in second_user
    assert "second question" in second_user


def test_continue_session_uses_default(stub, tmp_path):
    cwd = str(tmp_path)
    _tool(server.nim_prompt)("one", cwd=cwd, continue_session=True)
    assert server._session_path(cwd, server.DEFAULT_SESSION).exists()


def test_session_compaction_uses_nim_not_agy(stub, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SESSION_BUDGET_CHARS", 200)
    cwd = str(tmp_path)
    for i in range(6):
        stub.queue.append((200, {}, _completion(f"answer {i} " + "x" * 120)))
    for i in range(5):
        _tool(server.nim_prompt)(f"question {i}", cwd=cwd, session="long")
    saved = server._load_session(cwd, "long")
    assert saved["summary"]  # older turns were folded in
    assert len(saved["turns"]) <= server.SESSION_KEEP_VERBATIM


# --- workflow tools -------------------------------------------------------


def test_review_uses_supplied_diff(stub, tmp_path):
    out = _tool(server.nim_review)(str(tmp_path), diff="-a\n+b\n")
    assert out == "stub answer"
    user = stub.posts()[0][3]["messages"][-1]["content"]
    assert server.REVIEW_PROMPT in user and "+b" in user


def test_security_review_uses_security_prompt(stub, tmp_path):
    _tool(server.nim_security_review)(str(tmp_path), diff="+x\n")
    user = stub.posts()[0][3]["messages"][-1]["content"]
    assert server.SECURITY_REVIEW_PROMPT in user


def test_review_empty_diff(stub, tmp_path):
    out = _tool(server.nim_review)(str(tmp_path), diff="  \n")
    assert out.startswith("[No diff to review")
    assert not stub.requests


def test_explain_error_inlines_files(stub, tmp_path):
    src = tmp_path / "boom.py"
    src.write_text("raise ValueError('boom-marker')\n")
    _tool(server.nim_explain_error)("ValueError: boom", files=[str(src)])
    user = stub.posts()[0][3]["messages"][-1]["content"]
    assert "ValueError: boom" in user and "boom-marker" in user


def test_summarize_parses_json(stub, tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    stub.queue.append(
        (
            200,
            {},
            _completion(json.dumps({"summary": "S", "key_points": ["k"]})),
        )
    )
    out = json.loads(_tool(server.nim_summarize)(str(tmp_path), "m.py"))
    assert out == {"summary": "S", "key_points": ["k"]}
    user = stub.posts()[0][3]["messages"][-1]["content"]
    assert "x = 1" in user


def test_summarize_falls_back_on_prose(stub, tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    stub.queue.append((200, {}, _completion("just prose, no json")))
    out = json.loads(_tool(server.nim_summarize)(str(tmp_path), "m.py"))
    assert out["summary"] == "just prose, no json"


def test_summarize_rejects_escape_from_cwd(stub, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (tmp_path / "secret.txt").write_text("nope")
    out = _tool(server.nim_summarize)(str(proj), "../secret.txt")
    assert out.startswith("[Error: target must be inside cwd")
    assert not stub.requests


def test_summarize_missing_target(stub, tmp_path):
    out = _tool(server.nim_summarize)(str(tmp_path), "nope.py")
    assert out.startswith("[Error: target not found")


# --- models, status, background -------------------------------------------


def test_models_lists_and_filters(stub):
    out = _tool(server.nim_models)()
    assert server.NIM_DEFAULT_MODEL in out and "(2 of 2)" in out
    out = _tool(server.nim_models)("nemotron")
    assert "nvidia/nemotron-x" in out and "llama" not in out
    assert _tool(server.nim_models)("zzz").startswith("[No NIM models match")


def test_status_ready(stub):
    out = _tool(server.nim_status)()
    assert out.startswith("READY — NVIDIA NIM")
    assert KEY not in out
    assert "default_model: nvidia/nemotron-3.5-super-vl-120b-a12b" in out
    assert "models_listed: 2" in out
    assert "key 1: ok" in out and "cooldown none" in out


def test_status_not_configured(stub, monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    assert _tool(server.nim_status)().startswith("NOT CONFIGURED")


def test_status_bad_key(stub):
    stub.queue.append((401, {}, {"detail": "bad key"}))
    out = _tool(server.nim_status)()
    assert out.startswith("NOT READY") and "API key rejected" in out


def test_status_skip_verify_spends_no_completion(stub):
    out = _tool(server.nim_status)(verify=False)
    assert "(key not verified)" in out
    assert not stub.posts()


def test_nim_start_and_poll(stub):
    started = _tool(server.nim_start)("background q", use_cache=False)
    job_id = re.search(r"job (\w+)\.", started).group(1)
    for _ in range(100):
        result = _tool(server.gemini_poll)(job_id)
        if not result.startswith("[Job"):
            break
        time.sleep(0.05)
    assert result == "stub answer"


def test_all_nim_tools_registered():
    import asyncio

    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {
        "nim_prompt",
        "nim_start",
        "nim_review",
        "nim_security_review",
        "nim_explain_error",
        "nim_summarize",
        "nim_models",
        "nim_status",
        "nim_agent",
        "nim_agent_start",
        "nim_scout",
        "nim_scout_start",
        "nim_first_pass_review",
        "nim_first_pass_review_start",
        "nim_triage_log",
        "nim_triage_log_start",
        "nim_edit",
        "nim_edit_start",
    } <= names


# --- multiple keys --------------------------------------------------------

KEY2 = "nvapi-test-second-key"


def _auth(post):
    return post[2].get("Authorization")


@pytest.fixture
def two_keys(stub, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEYS", f"{KEY}, {KEY2}")
    server._nim_reset_state()
    return stub


def test_keys_parsed_deduplicated_in_order(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEYS", "a,b  c\nb")
    monkeypatch.setenv("NVIDIA_API_KEY", "c")
    monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
    assert server._nim_api_keys() == ["a", "b", "c"]
    assert server._nim_api_key() == "a"


def test_requests_rotate_across_keys(two_keys):
    for i in range(4):
        _tool(server.nim_prompt)(f"q{i}", use_cache=False)
    used = [_auth(p) for p in two_keys.posts()]
    assert used == [f"Bearer {k}" for k in (KEY, KEY2, KEY, KEY2)]


def test_429_fails_over_to_other_key_immediately(two_keys):
    two_keys.queue.append((429, {"Retry-After": "120"}, {"detail": "slow"}))
    out = _tool(server.nim_prompt)("q", use_cache=False)
    assert out == "stub answer"
    first, second = two_keys.posts()
    assert _auth(first) != _auth(second)
    # The limited key stays in cooldown, so later calls use the other one.
    _tool(server.nim_prompt)("q2", use_cache=False)
    _tool(server.nim_prompt)("q3", use_cache=False)
    assert {_auth(p) for p in two_keys.posts()[1:]} == {_auth(second)}


def test_all_keys_limited_fails_fast(two_keys, monkeypatch):
    monkeypatch.setattr(server, "NIM_MAX_WAIT", 0.05)
    for _ in range(10):
        two_keys.queue.append((429, {"Retry-After": "120"}, {"detail": "no"}))
    out = _tool(server.nim_prompt)("q", use_cache=False)
    assert "rate-limited" in out and "across 2 keys" in out


def test_rejected_key_is_retired_when_another_remains(two_keys):
    two_keys.queue.append((401, {}, {"detail": "bad key"}))
    out = _tool(server.nim_prompt)("q", use_cache=False)
    assert out == "stub answer"
    for i in range(3):
        _tool(server.nim_prompt)(f"more{i}", use_cache=False)
    later = {_auth(p) for p in two_keys.posts()[1:]}
    assert later == {f"Bearer {KEY2}"}
    assert server._nim_key_state(KEY).dead


def test_last_key_is_never_retired(stub):
    stub.queue.append((401, {}, {"detail": "bad key"}))
    out = _tool(server.nim_prompt)("q", use_cache=False)
    assert "API key rejected" in out
    assert not server._nim_key_state(KEY).dead


def test_status_reports_each_key_without_leaking(two_keys):
    out = _tool(server.nim_status)()
    assert out.startswith("READY")
    assert KEY not in out and KEY2 not in out
    assert "keys: 2" in out
    assert "key 1: OK" in out and "key 2: OK" in out


def test_status_ready_if_one_key_works(two_keys):
    two_keys.queue.append((401, {}, {"detail": "bad key"}))
    out = _tool(server.nim_status)()
    assert out.startswith("READY")
    assert "key 1: FAILED" in out and "key 2: OK" in out


# --- nim_agent (read-only tool loop) --------------------------------------


def _tool_call(name, args, call_id="call_1"):
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("one\nTARGET = 1\nthree\n")
    (root / "src" / "blob.bin").write_bytes(b"\x00\x01TARGET")
    (root / ".env").write_text("TARGET=secret-value\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("TARGET")
    (tmp_path / "outside.txt").write_text("TARGET outside\n")
    return root.resolve()


def test_agent_tools_are_confined_and_skip_secrets(repo):
    grep = server._agent_run_tool(repo, "grep", {"pattern": "TARGET"})
    assert "src/a.py:2:TARGET = 1" in grep
    assert "secret-value" not in grep and "blob.bin" not in grep
    assert ".git" not in grep
    read = server._agent_run_tool(
        repo, "read_range", {"path": "src/a.py", "start": 2, "end": 3}
    )
    assert "2: TARGET = 1" in read and "3: three" in read
    for bad in ("../outside.txt", "/etc/hosts", ".env", ".git/config"):
        out = server._agent_run_tool(
            repo, "read_range", {"path": bad, "start": 1, "end": 5}
        )
        assert out.startswith("[error"), bad
    assert "src/" in server._agent_run_tool(repo, "list_dir", {})
    assert ".env" not in server._agent_run_tool(repo, "list_dir", {})


def _read(repo, path):
    return server._agent_run_tool(
        repo, "read_range", {"path": path, "start": 1, "end": 5}
    )


def test_agent_dekko_folders_are_off_limits(repo):
    (repo / ".dekko").mkdir()
    (repo / ".dekko" / "MAP.md").write_text("map of TARGET\n")
    (repo / "sub" / ".dekko").mkdir(parents=True)
    (repo / "sub" / ".dekko" / "x").write_text("TARGET\n")
    assert _read(repo, ".dekko/MAP.md").startswith("[error: path is off")
    assert _read(repo, "sub/.dekko/x").startswith("[error: path is off")
    assert ".dekko" not in server._agent_run_tool(repo, "list_dir", {})
    listing = server._agent_run_tool(repo, "list_dir", {"path": "sub"})
    assert ".dekko" not in listing
    assert ".dekko" not in server._agent_grep(repo, "TARGET", ".", None)


def test_agent_symlink_out_of_project_is_refused(repo, tmp_path):
    (repo / "link.txt").symlink_to(tmp_path / "outside.txt")
    out = server._agent_run_tool(
        repo, "read_range", {"path": "link.txt", "start": 1, "end": 2}
    )
    assert out == "[error: path is outside the project]"


def test_agent_grep_searches_a_named_log_but_skips_logs_in_a_walk(repo):
    (repo / "run.log").write_text("ok\nFAILED test_x\n")
    hit = server._agent_grep(repo, "FAILED", "run.log", None)
    assert hit == "run.log:2:FAILED test_x"
    assert server._agent_grep(repo, "FAILED", ".", None) == "[no matches]"


def test_agent_grep_context_shows_lines_after_each_match(repo):
    out = server._agent_run_tool(
        repo, "grep", {"pattern": "TARGET", "path": "src", "context": 1}
    )
    assert out == "src/a.py:2:TARGET = 1\nsrc/a.py-3-three"


def test_agent_grep_reports_the_total_when_capped(repo):
    (repo / "many.txt").write_text("hit\n" * 70)
    out = server._agent_grep(repo, "hit", "many.txt", None)
    assert out.count("many.txt:") == server.NIM_AGENT_MAX_GREP_HITS
    assert out.endswith(
        f"[showing {server.NIM_AGENT_MAX_GREP_HITS} of 70 matching lines; "
        "narrow the search]"
    )


def test_agent_file_map_lists_tracked_files_with_line_counts(tmp_path):
    root = tmp_path / "g"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("one\ntwo\nthree\n")
    (root / ".env").write_text("SECRET=1\n")
    (root / "run.log").write_text("noise\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    out = server._agent_file_map(root.resolve())
    assert "src/a.py (3 lines)" in out
    assert ".env" not in out and "run.log" not in out
    assert server._agent_file_map(tmp_path) == ""  # not a git repo


def test_agent_skips_engine_assets_unless_named(tmp_path):
    root = tmp_path / "u"
    (root / "Assets").mkdir(parents=True)
    (root / "Assets" / "Mop.cs").write_text("class Mop { int TARGET; }\n")
    (root / "Assets" / "Mop.cs.meta").write_text("guid: TARGET\n")
    (root / "Assets" / "Mop.prefab").write_text("m_Name: TARGET\n")
    (root / "Assets" / "Mop.FBX").write_text("TARGET\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    root = root.resolve()
    walk = server._agent_grep(root, "TARGET", ".", None)
    assert walk == "Assets/Mop.cs:1:class Mop { int TARGET; }"
    named = server._agent_grep(root, "TARGET", "Assets/Mop.prefab", None)
    assert named == "Assets/Mop.prefab:1:m_Name: TARGET"
    file_map = server._agent_file_map(root)
    assert "Mop.cs (1 lines)" in file_map
    assert ".meta" not in file_map and ".prefab" not in file_map
    listing = server._agent_run_tool(root, "list_dir", {"path": "Assets"})
    assert "Mop.cs.meta" not in listing and "Mop.prefab" in listing


def test_agent_system_prompt_offers_batching_and_the_file_map(stub, repo):
    stub.queue.append((200, {}, _completion("done")))
    _tool(server.nim_agent)("t", str(repo))
    system = stub.posts()[0][3]["messages"][0]["content"]
    assert f"up to {server.NIM_AGENT_CALLS_PER_STEP} tool calls" in system


def test_agent_tool_edge_cases(repo):
    assert server._agent_run_tool(repo, "grep", {"pattern": "("}).startswith(
        "[error: bad regex"
    )
    assert server._agent_run_tool(repo, "nope", {}).startswith(
        "[error: unknown"
    )
    assert server._agent_run_tool(
        repo, "read_range", {"path": "src/a.py", "start": "x"}
    ).startswith("[tool error")
    big = repo / "big.txt"
    big.write_text("\n".join(f"line {i}" for i in range(500)))
    out = server._agent_run_tool(
        repo, "read_range", {"path": "big.txt", "start": 1, "end": 500}
    )
    assert "120: line 119" in out and "121:" not in out


def test_agent_loop_runs_tool_then_answers(stub, repo):
    stub.queue.append((200, {}, _tool_call("grep", {"pattern": "TARGET"})))
    stub.queue.append(
        (200, {}, _completion("Defined at src/a.py:2 and src/a.py:99"))
    )
    out = _tool(server.nim_agent)("where is TARGET?", str(repo))
    assert "src/a.py:2 and src/a.py:99 [unverified]" in out
    assert "citations verified 1/2" in out
    assert "2 steps, 1 tool calls" in out
    first, second = (p[3] for p in stub.posts())
    assert [t["function"]["name"] for t in first["tools"]] == [
        "grep",
        "read_range",
        "list_dir",
    ]
    tool_msg = second["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "call_1"
    assert "src/a.py:2:TARGET = 1" in tool_msg["content"]


def test_citations_check_lists_and_extensionless_files(repo):
    (repo / "Makefile").write_text("all:\n\techo hi\n")
    answer = (
        "- src/a.py:1, 3 fine\n"
        "- src/a.py:2, 99 bad\n"
        "- Makefile:2 fine\n"
        "- Makefile:9 bad\n"
        "- step:3 is prose, not a file"
    )
    out, verified, total = server._agent_verify_citations(answer, repo)
    assert out.splitlines() == [
        "- src/a.py:1, 3 fine",
        "- src/a.py:2, 99 [unverified] bad",
        "- Makefile:2 fine",
        "- Makefile:9 [unverified] bad",
        "- step:3 is prose, not a file",
    ]
    assert (verified, total) == (2, 4)


def test_agent_step_cap_forces_an_answer_without_tools(stub, repo):
    stub.queue.append((200, {}, _tool_call("list_dir", {"path": "src"})))
    stub.queue.append((200, {}, _completion("best effort")))
    out = _tool(server.nim_agent)("find it", str(repo), max_steps=2)
    assert out.startswith("best effort")
    last = stub.posts()[1][3]
    assert "tools" not in last
    # Models keep calling tools while the history holds tool calls, so the
    # final request flattens the transcript into one plain user message.
    assert [m["role"] for m in last["messages"]] == ["system", "user"]
    final = last["messages"][-1]["content"]
    assert final.startswith("find it")
    assert 'list_dir({"path": "src"})' in final and "a.py" in final
    assert final.endswith(server.NIM_AGENT_FINAL_NUDGE)


def test_agent_answer_step_gets_a_larger_token_budget(stub, repo):
    stub.queue.append((200, {}, _tool_call("list_dir", {"path": "src"})))
    stub.queue.append((200, {}, _completion("answer")))
    _tool(server.nim_agent)("t", str(repo), max_steps=2, max_tokens=1000)
    tool_step, answer_step = (p[3] for p in stub.posts())
    assert tool_step["max_tokens"] == 1000
    assert answer_step["max_tokens"] == server.NIM_AGENT_ANSWER_MAX_TOKENS


@pytest.mark.parametrize("partial", ["", "Now I have enough. Critical"])
def test_agent_out_of_tokens_mid_loop_moves_to_the_answer(stub, repo, partial):
    stub.queue.append((200, {}, _tool_call("list_dir", {"path": "src"})))
    stub.queue.append((200, {}, _completion(partial, finish="length")))
    stub.queue.append((200, {}, _completion("answer")))
    out = _tool(server.nim_agent)("t", str(repo), max_steps=8)
    assert out.startswith("answer")
    posts = [p[3] for p in stub.posts()]
    assert len(posts) == 3 and "tools" not in posts[2]
    assert "[nim_agent: 3 steps, 1 tool calls" in out


def test_agent_caps_nemotron_reasoning_per_step(stub, repo):
    stub.queue.append((200, {}, _tool_call("list_dir", {"path": "src"})))
    stub.queue.append((200, {}, _completion("answer")))
    _tool(server.nim_agent)("t", str(repo), max_steps=2)
    tool_step, answer_step = (p[3] for p in stub.posts())
    assert tool_step["reasoning_budget"] == server.NIM_AGENT_STEP_THINKING
    assert answer_step["reasoning_budget"] == (
        server.NIM_AGENT_ANSWER_THINKING
    )


def test_agent_sends_no_reasoning_budget_to_other_models(stub, repo):
    stub.queue.append((200, {}, _completion("answer")))
    _tool(server.nim_agent)("t", str(repo), model="vendor/other-model")
    assert "reasoning_budget" not in stub.posts()[0][3]


def test_agent_ignoring_the_cap_gives_a_clear_error(stub, repo):
    for i in range(2):
        stub.queue.append(
            (200, {}, _tool_call("list_dir", {"path": "src"}, f"c{i}"))
        )
    out = _tool(server.nim_agent)("t", str(repo), max_steps=2)
    assert "hit its step cap" in out


def test_agent_repeated_call_is_not_rerun(repo):
    seen = set()
    call = {
        "id": "c",
        "function": {"name": "list_dir", "arguments": "{}"},
    }
    first = server._agent_step_results(repo, [call], seen)
    again = server._agent_step_results(repo, [call], seen)
    assert "src/" in first[0]["content"]
    assert "already ran" in again[0]["content"]


def test_agent_answers_every_call_and_bad_arguments(repo):
    calls = [
        {"id": f"c{i}", "function": {"name": "list_dir", "arguments": "{"}}
        for i in range(6)
    ]
    out = server._agent_step_results(repo, calls, set())
    assert [m["tool_call_id"] for m in out] == [f"c{i}" for i in range(6)]
    assert "not a JSON object" in out[0]["content"]
    assert "too many tool calls" in out[5]["content"]


def test_agent_validates_inputs(stub, tmp_path, monkeypatch):
    assert _tool(server.nim_agent)("  ", str(tmp_path)).startswith(
        "[Error: task"
    )
    assert "not a directory" in _tool(server.nim_agent)(
        "t", str(tmp_path / "x")
    )
    monkeypatch.delenv("NVIDIA_API_KEY")
    assert "not configured" in _tool(server.nim_agent)("t", str(tmp_path))
    assert not stub.requests


def test_agent_http_error_is_returned(stub, repo):
    stub.queue.append((401, {}, {"detail": "bad key"}))
    out = _tool(server.nim_agent)("t", str(repo))
    assert out.startswith("[NVIDIA NIM error") and "rejected" in out


def test_agent_model_env_override(stub, repo, monkeypatch):
    monkeypatch.setenv("CASTOR_NIM_AGENT_MODEL", "nvidia/nemotron-x")
    stub.queue.append((200, {}, _completion("done")))
    _tool(server.nim_agent)("t", str(repo))
    assert stub.posts()[0][3]["model"] == "nvidia/nemotron-x"


def test_agent_start_and_poll(stub, repo):
    stub.queue.append((200, {}, _completion("bg answer")))
    started = _tool(server.nim_agent_start)("t", str(repo))
    job_id = re.search(r"job (\w+)\.", started).group(1)
    for _ in range(100):
        out = _tool(server.gemini_poll)(job_id)
        if "bg answer" in out:
            break
        time.sleep(0.05)
    assert "bg answer" in out


# --- agent roles ----------------------------------------------------------


@pytest.fixture
def captured(monkeypatch):
    """Replace the agent dispatcher and record how a role calls it."""
    calls = []

    def fake(task, cwd, model=None, max_steps=None, max_tokens=None):
        calls.append(
            {"task": task, "cwd": cwd, "model": model, "steps": max_steps}
        )
        return "ok"

    monkeypatch.setattr(server, "_dispatch_nim_agent", fake)
    return calls


def test_scout_builds_task_and_default_budget(captured, repo):
    assert _tool(server.nim_scout)("where is TARGET set?", str(repo)) == "ok"
    call = captured[0]
    assert "where is TARGET set?" in call["task"]
    assert "at most 15 bullets" in call["task"]
    assert call["steps"] == server.NIM_SCOUT_STEPS
    _tool(server.nim_scout)("q", str(repo), max_steps=3, model="m")
    assert captured[1]["steps"] == 3 and captured[1]["model"] == "m"


def test_review_reads_diff_file_server_side(captured, repo, tmp_path):
    diff = tmp_path / "t.diff"
    diff.write_text("@@ -1 +1 @@\n+TARGET = 2\n")
    out = _tool(server.nim_first_pass_review)(
        str(repo), diff_file=str(diff), focus="host owns the timer"
    )
    assert out == "ok"
    task = captured[0]["task"]
    assert "[DIFF]\n@@ -1 +1 @@\n+TARGET = 2\n\n[/DIFF]" in task
    assert "host owns the timer" in task
    assert captured[0]["steps"] == server.NIM_REVIEW_STEPS


def test_review_uses_range_then_working_tree(captured, repo, monkeypatch):
    seen = []

    def fake_git(cwd, rev_range=None):
        seen.append(rev_range)
        return "+x\n"

    monkeypatch.setattr(server, "_git_diff", fake_git)
    _tool(server.nim_first_pass_review)(str(repo), rev_range="abc..HEAD")
    _tool(server.nim_first_pass_review)(str(repo))
    assert seen == ["abc..HEAD", None]


@pytest.mark.parametrize("bad", ["--output=/tmp/x", "a b", "$(x)"])
def test_git_diff_rejects_non_revisions(bad, repo):
    assert server._git_diff(str(repo), bad).startswith("[Error")


def test_git_diff_passes_range_to_git(monkeypatch, repo):
    class Done:
        returncode, stdout, stderr = 0, "d", ""

    ran = []
    monkeypatch.setattr(
        server.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or Done()
    )
    assert server._git_diff(str(repo), "abc123..HEAD") == "d"
    assert server._git_diff(str(repo)) == "d"
    assert server._git_diff(str(repo), "") == "d"  # empty means HEAD
    assert ran == [
        ["git", "diff", "abc123..HEAD"],
        ["git", "diff", "HEAD"],
        ["git", "diff", "HEAD"],
    ]


def test_review_refuses_empty_oversize_and_missing(captured, repo, tmp_path):
    review = _tool(server.nim_first_pass_review)
    empty = tmp_path / "e.diff"
    empty.write_text("  \n")
    assert "empty" in review(str(repo), diff_file=str(empty))
    big = tmp_path / "b.diff"
    big.write_text("x" * (server.NIM_REVIEW_MAX_DIFF_CHARS + 1))
    assert "first-pass limit" in review(str(repo), diff_file=str(big))
    assert "not a file" in review(str(repo), diff_file=str(tmp_path / "no"))
    assert not captured


def test_review_refuses_a_diff_file_that_is_not_a_diff(
    captured, repo, tmp_path
):
    review = _tool(server.nim_first_pass_review)
    notes = tmp_path / "notes.txt"
    notes.write_text("api_key = abc\nhost = example\n")
    assert "does not look like a diff" in review(
        str(repo), diff_file=str(notes)
    )
    secret = tmp_path / ".env"
    secret.write_text("@@ -1 +1 @@\n+KEY=1\n")
    assert "off limits" in review(str(repo), diff_file=str(secret))
    assert not captured


def test_triage_requires_log_inside_cwd(captured, repo, tmp_path):
    (repo / "run.log").write_text("FAIL x\n")
    triage = _tool(server.nim_triage_log)
    assert triage(str(repo), str(repo / "run.log"), question="q?") == "ok"
    task = captured[0]["task"]
    assert "`run.log`" in task and "Also determine: q?" in task
    assert captured[0]["steps"] == server.NIM_TRIAGE_STEPS
    assert "inside cwd" in triage(str(repo), str(tmp_path / "outside.txt"))
    assert "inside cwd" in triage(str(repo), ".env")
    assert "inside cwd" in triage(str(repo), "missing.log")
    assert "not a directory" in triage(str(tmp_path / "zz"), "run.log")
    assert len(captured) == 1


def test_role_end_to_end_through_the_loop(stub, repo):
    stub.queue.append((200, {}, _tool_call("grep", {"pattern": "TARGET"})))
    stub.queue.append((200, {}, _completion("- src/a.py:1-3 - the setting")))
    out = _tool(server.nim_scout)("where is TARGET?", str(repo))
    assert "src/a.py:1-3 - the setting" in out
    assert "citations verified 1/1" in out


def _poll_until_done(job_id):
    for _ in range(100):
        out = _tool(server.gemini_poll)(job_id)
        if not out.startswith("[Job"):
            return out
        time.sleep(0.05)
    return out


def _job_id(started):
    return re.search(r"job (\w+)\.", started).group(1)


def test_role_start_tools_queue_the_same_task(captured, repo, tmp_path):
    diff = tmp_path / "t.diff"
    diff.write_text("@@ -1 +1 @@\n+TARGET = 2\n")
    (repo / "run.log").write_text("FAIL x\n")
    starts = [
        (server.nim_scout_start, ("q?", str(repo)), server.NIM_SCOUT_STEPS),
        (
            server.nim_first_pass_review_start,
            (str(repo), str(diff)),
            server.NIM_REVIEW_STEPS,
        ),
        (
            server.nim_triage_log_start,
            (str(repo), "run.log"),
            server.NIM_TRIAGE_STEPS,
        ),
    ]
    for tool, args, steps in starts:
        assert _poll_until_done(_job_id(_tool(tool)(*args))) == "ok"
        assert captured[-1]["steps"] == steps
    assert "q?" in captured[0]["task"]
    assert "+TARGET = 2" in captured[1]["task"]
    assert "`run.log`" in captured[2]["task"]


def test_role_start_tools_report_bad_input_at_once(captured, repo, tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("not a diff\n")
    review = _tool(server.nim_first_pass_review_start)
    assert "does not look like a diff" in review(str(repo), str(notes))
    triage = _tool(server.nim_triage_log_start)
    assert "inside cwd" in triage(str(repo), "missing.log")
    assert not captured


def test_agent_tells_the_model_its_budget_and_warns_before_the_last_step(
    stub, repo
):
    stub.queue.append((200, {}, _tool_call("list_dir", {"path": "src"}, "a")))
    stub.queue.append((200, {}, _tool_call("list_dir", {"path": "."}, "b")))
    stub.queue.append((200, {}, _completion("done")))
    _tool(server.nim_agent)("t", str(repo), max_steps=3)
    posts = [p[3] for p in stub.posts()]
    assert "3 requests in total" in posts[0]["messages"][0]["content"]
    assert server.NIM_AGENT_LAST_TOOL_NUDGE not in json.dumps(posts[0])
    assert posts[1]["messages"][-1]["content"] == (
        server.NIM_AGENT_LAST_TOOL_NUDGE
    )
    assert posts[2]["messages"][-1]["content"].endswith(
        server.NIM_AGENT_FINAL_NUDGE
    )


# --- step model, records, review context, quotes ---------------------------


def test_step_model_runs_tools_and_main_model_answers(stub, repo):
    stub.queue.append((200, {}, _tool_call("list_dir", {"path": "src"})))
    stub.queue.append((200, {}, _completion("early draft from the fast one")))
    stub.queue.append((200, {}, _completion("final answer")))
    text, stats = server._run_nim_agent(
        "t", str(repo), "vendor/strong", 5, None, "vendor/fast"
    )
    models = [p[3]["model"] for p in stub.posts()]
    assert models == ["vendor/fast", "vendor/fast", "vendor/strong"]
    assert "tools" not in stub.posts()[2][3]
    assert text.startswith("final answer")
    assert "models vendor/fast (steps), vendor/strong" in text
    assert [r["model"] for r in stats["requests"]] == models


def test_step_model_env_default(stub, repo, monkeypatch):
    monkeypatch.setenv("CASTOR_NIM_AGENT_STEP_MODEL", "vendor/fast")
    stub.queue.append((200, {}, _completion("draft")))
    stub.queue.append((200, {}, _completion("answer")))
    _tool(server.nim_agent)("t", str(repo), model="vendor/strong")
    assert [p[3]["model"] for p in stub.posts()] == [
        "vendor/fast",
        "vendor/strong",
    ]


def _usage_entries(tmp_path):
    folder = tmp_path / "cache" / "claude-castor" / server.NIM_USAGE_DIR
    return [
        json.loads(line)
        for path in sorted(folder.glob("*.jsonl"))
        for line in path.read_text().splitlines()
    ]


def test_usage_ledger_records_requests_without_prompt_text(stub, tmp_path):
    body = _completion("hi")
    body["usage"] = {"prompt_tokens": 11, "completion_tokens": 3}
    stub.queue.append((200, {}, body))
    _tool(server.nim_prompt)("secret prompt words", use_cache=False)
    (entry,) = _usage_entries(tmp_path)
    assert entry["ok"] is True and entry["model"] == server.NIM_DEFAULT_MODEL
    assert entry["prompt_tokens"] == 11 and entry["completion_tokens"] == 3
    folder = tmp_path / "cache" / "claude-castor" / server.NIM_USAGE_DIR
    assert "secret prompt" not in "".join(
        p.read_text() for p in folder.iterdir()
    )


def test_records_can_be_turned_off(stub, tmp_path, monkeypatch, repo):
    monkeypatch.setenv("CASTOR_NIM_TRACES", "0")
    stub.queue.append((200, {}, _completion("a")))
    _tool(server.nim_agent)("t", str(repo))
    assert not (tmp_path / "cache" / "claude-castor").exists()


def test_agent_run_leaves_a_trace_and_nim_usage_sums_it(stub, tmp_path, repo):
    stub.queue.append((200, {}, _tool_call("list_dir", {"path": "src"})))
    stub.queue.append((200, {}, _completion("see src/a.py:2")))
    _tool(server.nim_agent)("trace me", str(repo))
    folder = tmp_path / "cache" / "claude-castor" / server.NIM_TRACES_DIR
    (path,) = folder.glob("*.json")
    trace = json.loads(path.read_text())
    assert trace["ok"] and trace["task_head"] == "trace me"
    assert trace["requests"][0]["calls"][0]["name"] == "list_dir"
    assert trace["citations"] == [1, 1]
    report = _tool(server.nim_usage)()
    assert "2 requests" in report and "agent runs 1 (1 ok" in report


def test_nim_usage_with_no_records(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert "No NIM usage" in _tool(server.nim_usage)(days=1)


def test_review_context_numbers_lines_and_lists_uses(captured, repo, tmp_path):
    (repo / "src" / "lib.py").write_text(
        "".join(f"x{n} = {n}\n" for n in range(1, 41))
        + "def helper(v):\n    return v + 1\n"
    )
    (repo / "src" / "use.py").write_text("from lib import helper\nhelper(2)\n")
    diff = tmp_path / "c.diff"
    diff.write_text(
        "diff --git a/src/lib.py b/src/lib.py\n--- a/src/lib.py\n"
        "+++ b/src/lib.py\n@@ -41,2 +41,2 @@\n def helper(v):\n"
        "-    return v\n+    return v + 1\n"
    )
    _tool(server.nim_first_pass_review)(str(repo), diff_file=str(diff))
    task = captured[0]["task"]
    assert "[CONTEXT]" in task and "== src/lib.py lines 21-42 of 42" in task
    assert "42:     return v + 1" in task and "20: x20" not in task
    assert "uses of helper:" in task and "src/use.py:2:helper(2)" in task


def test_diff_symbols_reads_definitions_and_hunk_headers():
    diff = (
        "@@ -1,4 +1,4 @@ def outer(a):\n"
        "-    a = 1\n"
        "+    public static int Clamp(int v) {\n"
        " run_job() {\n"
        "+  x = 1\n"
        " def unrelated():\n"
    )
    assert server._diff_symbols(diff) == ["outer", "Clamp", "run_job"]


def test_quotes_are_checked_against_the_cited_lines(repo):
    answer = (
        "- src/a.py:2 - `TARGET = 1` - right line\n"
        "- src/a.py:3 - `TARGET = 1` - near enough\n"
        "- src/a.py:1 - `three` - off by two, within slack\n"
        "- src/a.py:2 - `nowhere in it` - invented\n"
        "- no citation `TARGET = 1` here"
    )
    out, matched, checked = server._agent_check_quotes(answer, repo)
    assert (matched, checked) == (3, 4)
    assert "`nowhere in it` [quote not in file]" in out
    far = "- src/a.py:2 - `three` - fine\n- big.txt:1 - `line 90` - far"
    (repo / "big.txt").write_text("\n".join(f"line {i}" for i in range(100)))
    out, matched, checked = server._agent_check_quotes(far, repo)
    assert "`line 90` [quote is at line 91]" in out and checked == 2


def test_quote_results_appear_in_the_footer(stub, repo):
    stub.queue.append((200, {}, _completion("- src/a.py:2 - `TARGET = 1`")))
    out = _tool(server.nim_agent)("t", str(repo))
    assert "citations verified 1/1; quotes matched 1/1]" in out


def test_quote_check_pairs_backticks_and_accepts_elisions(repo):
    answer = (
        "- src/a.py:2 - sets `1` only when `TARGET = 1` runs\n"
        "- src/a.py:1 - `one ... three` - shortened\n"
        "- src/a.py:1 - `one ... missing` - half invented"
    )
    out, matched, checked = server._agent_check_quotes(answer, repo)
    assert (matched, checked) == (2, 3)
    assert "[quote not in file]" in out.splitlines()[2]
    assert "only when `TARGET = 1` runs" in out.splitlines()[0]


def test_bare_file_names_resolve_when_unique(tmp_path):
    root = tmp_path / "b"
    (root / "Tools" / "playtest").mkdir(parents=True)
    (root / "Tools" / "playtest" / "run.sh").write_text("a\nb\nfail here\n")
    (root / "x").mkdir()
    (root / "x" / "dup.py").write_text("1\n")
    (root / "y").mkdir()
    (root / "y" / "dup.py").write_text("1\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    root = root.resolve()
    out, verified, total = server._agent_verify_citations(
        "run.sh:3 and dup.py:1", root
    )
    assert (verified, total) == (1, 2) and "dup.py:1 [unverified]" in out
    out, matched, checked = server._agent_check_quotes(
        "- run.sh:3 - `fail here`", root
    )
    assert (matched, checked) == (1, 1)


def test_agent_default_model_follows_a_changed_main_model(
    stub, repo, monkeypatch
):
    stub.queue.append((200, {}, _completion("a")))
    _tool(server.nim_agent)("t", str(repo))
    assert stub.posts()[0][3]["model"] == server.NIM_AGENT_DEFAULT_MODEL
    monkeypatch.setenv("CASTOR_NIM_MODEL", "local/self-hosted")
    stub.queue.append((200, {}, _completion("b")))
    _tool(server.nim_agent)("t", str(repo))
    assert stub.posts()[1][3]["model"] == "local/self-hosted"


# --- grep candidates from the git index -------------------------------------


def _git_repo(root):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)


def test_grep_walks_the_git_index_not_ignored_trees(tmp_path):
    root = tmp_path / "gi"
    (root / "src").mkdir(parents=True)
    (root / "Library" / "deep").mkdir(parents=True)
    (root / "src" / "a.cs").write_text("TARGET tracked\n")
    (root / ".gitignore").write_text("Library/\nbuild-out/\n")
    _git_repo(root)
    (root / "src" / "new.cs").write_text("TARGET untracked\n")
    (root / "Library" / "deep" / "x.cs").write_text("TARGET ignored\n")
    (root / "Library" / "run.txt").write_text("TARGET named\n")
    root = root.resolve()
    server._nim_reset_state()
    out = server._agent_grep(root, "TARGET", ".", None)
    assert "src/a.cs:1" in out and "src/new.cs:1" in out
    assert "ignored" not in out
    named = server._agent_grep(root, "TARGET", "Library/run.txt", None)
    assert named == "Library/run.txt:1:TARGET named"
    sub = server._agent_grep(root, "TARGET", "src", None)
    assert sub.count("\n") == 1 and "Library" not in sub


def test_grep_index_is_cached_until_reset(tmp_path):
    root = tmp_path / "gc"
    root.mkdir()
    (root / "a.txt").write_text("hit\n")
    _git_repo(root)
    root = root.resolve()
    server._nim_reset_state()
    first = server._agent_candidates(root)
    (root / "b.txt").write_text("hit\n")
    assert server._agent_candidates(root) == first
    server._nim_reset_state()
    assert server._agent_candidates(root) == [root / "a.txt", root / "b.txt"]


def test_grep_outside_git_falls_back_to_a_pruned_walk(tmp_path):
    root = tmp_path / "plain"
    (root / "Library").mkdir(parents=True)
    (root / "Library" / "x.cs").write_text("TARGET\n")
    (root / "a.cs").write_text("TARGET\n")
    root = root.resolve()
    server._nim_reset_state()
    assert server._agent_index(root) is None
    assert server._agent_grep(root, "TARGET", ".", None) == "a.cs:1:TARGET"


# --- triage context ----------------------------------------------------------


def _runner_log():
    rows = ["start run", "NOT ASSESSED  room A: no bag", "  cause: no owner"]
    for i in range(30):
        rows += [
            f"step {i}",
            "Traceback (most recent call last):",
            '  File "<string>", line 1, in <module>',
            f"    print({i})",
            "EOFError: EOF when reading a line",
        ]
    rows += [f"filler {i}" for i in range(60)]
    rows += ["NOT ASSESSED  room B: no bag", "--- verdicts ---"]
    rows += ["PASS  standard", "total 2, fail 0, not assessed 1"]
    return "\n".join(rows) + "\n"


def test_triage_context_counts_collapses_and_keeps_the_tail(tmp_path):
    log = tmp_path / "run.log"
    log.write_text(_runner_log())
    lines = log.read_text().splitlines()
    ctx = server._triage_context(log, "run.log")
    assert "Traceback: 30" in ctx and "NOT ASSESSED: 2" in ctx
    assert "FAIL: 0" in ctx and "ERROR: 0" in ctx
    first = lines.index("Traceback (most recent call last):") + 1
    last = len(lines) - lines[::-1].index("Traceback (most recent call last):")
    assert (
        f"run.log:{first} - Traceback (most recent call last): "
        f"(repeats 30 times, last at line {last})"
    ) in ctx
    assert ctx.count("EOFError") >= 1 and "    3: cause: no owner" in ctx
    assert "run.log:2 - NOT ASSESSED  room A" in ctx
    room_b = lines.index("NOT ASSESSED  room B: no bag") + 1
    assert f"run.log:{room_b} - NOT ASSESSED  room B" in ctx
    assert f"{len(lines)}: total 2, fail 0, not assessed 1" in ctx


def test_triage_collapses_a_row_repeated_in_the_verdict_table(tmp_path):
    log = tmp_path / "v.log"
    log.write_text(
        "NOT ASSESSED  room A\nplace one\n"
        + "".join(f"pad {i}\n" for i in range(60))
        + "--- verdicts ---\nNOT ASSESSED  room A\n"
    )
    ctx = server._triage_context(log, "v.log")
    assert "v.log:1 - NOT ASSESSED  room A (repeats 2 times, last at " in ctx


def test_triage_context_with_no_markers_has_zero_counts_and_the_ends(
    tmp_path,
):
    log = tmp_path / "ok.log"
    log.write_text("".join(f"line {i} error in path\n" for i in range(100)))
    ctx = server._triage_context(log, "ok.log")
    assert all(f"{name}: 0" in ctx for name, _ in server.NIM_TRIAGE_MARKERS)
    assert "== matching lines\n(none)" in ctx
    assert "1: line 0" in ctx and "100: line 99" in ctx
    assert "11: line 10" not in ctx


def test_triage_keeps_distinct_failures_apart():
    lines = ["FAIL test_3", "x", "FAIL test_4", "x", "12:00:01 FAIL test_3"]
    groups = server._collapse_repeats(lines, [1, 3, 5], lambda n: 0)
    assert groups == [(1, 5, 2), (3, 3, 1)]


def test_triage_run_carries_context_and_uses_at_most_three_requests(
    stub, repo
):
    (repo / "run.log").write_text(_runner_log())
    for pattern in ("x", "y"):
        stub.queue.append((200, {}, _tool_call("grep", {"pattern": pattern})))
    stub.queue.append((200, {}, _completion("inconclusive")))
    out = _tool(server.nim_triage_log)(str(repo), "run.log")
    posts = stub.posts()
    assert len(posts) <= 3 and "inconclusive" in out
    task = posts[0][3]["messages"][1]["content"]
    assert "[CONTEXT]" in task and "Traceback: 30" in task


def test_triage_refuses_a_log_over_the_size_cap(captured, repo, monkeypatch):
    (repo / "big.log").write_text("x" * 100)
    monkeypatch.setattr(server, "NIM_TRIAGE_MAX_LOG_BYTES", 10)
    out = _tool(server.nim_triage_log)(str(repo), "big.log")
    assert "split it first" in out and not captured


# --- chunked summarize -------------------------------------------------------


def _summary_json(text):
    return _completion(json.dumps({"summary": text, "key_points": [text]}))


def test_summarize_splits_a_big_file_and_merges(stub, tmp_path):
    row = "decision C{} keeps the host authoritative\n"
    (tmp_path / "DECISIONS.md").write_text(
        "".join(row.format(i) for i in range(3500))
    )
    for text in ("p", "p", "merged"):
        stub.queue.append((200, {}, _summary_json(text)))
    summarize = _tool(server.nim_summarize)
    out = json.loads(summarize(str(tmp_path), "DECISIONS.md"))
    assert out["summary"] == "merged"
    users = [p[3]["messages"][-1]["content"] for p in stub.posts()]
    assert len(users) == 3
    assert sorted("Part 1 of 2" in u for u in users[:2]) == [False, True]
    assert all("[PARTS]" not in u for u in users[:2])
    assert "[PARTS]" in users[2] and "2 parts" in users[2]
    assert "DECISIONS.md (lines 1-" in "".join(users[:2])
    for post in stub.posts():
        assert post[3]["max_tokens"] == server.NIM_SUMMARIZE_MAX_TOKENS
        assert post[3]["reasoning_budget"] == server.NIM_SUMMARIZE_THINKING


def test_summarize_a_file_under_the_chunk_size_makes_one_request(
    stub, tmp_path
):
    (tmp_path / "big.md").write_text("x" * 110_000)  # over the old 100 KB
    stub.queue.append((200, {}, _summary_json("S")))
    out = json.loads(_tool(server.nim_summarize)(str(tmp_path), "big.md"))
    assert out["summary"] == "S" and len(stub.posts()) == 1
    assert "skipped" not in stub.posts()[0][3]["messages"][-1]["content"]


def test_summarize_chunk_error_aborts_without_a_merge(stub, tmp_path):
    (tmp_path / "big.md").write_text("line\n" * 100_000)
    stub.queue.append((400, {}, {"detail": "bad chunk"}))
    stub.queue.append((200, {}, _summary_json("p")))
    out = _tool(server.nim_summarize)(str(tmp_path), "big.md")
    assert out.startswith("[NVIDIA NIM error") and "bad chunk" in out
    users = [p[3]["messages"][-1]["content"] for p in stub.posts()]
    assert not any("[PARTS]" in u for u in users)


def test_summarize_packs_a_directory_into_chunks(tmp_path):
    root = tmp_path / "d"
    root.mkdir()
    (root / "a_small.md").write_text("a" * 100)
    (root / "b_big.md").write_text("b\n" * 1500)
    (root / "c_small.md").write_text("c" * 100)
    (root / "skip.log").write_text("noise")
    blocks, error = server._summary_blocks(root, tmp_path, 1400)
    assert error is None
    labels = [label for label, _ in blocks]
    assert labels[0] == "d/a_small.md" and labels[-1] == "d/c_small.md"
    assert labels[1] == "d/b_big.md (lines 1-600)"
    assert not any("skip.log" in label for label in labels)
    chunks = server._pack_chunks(blocks, 1400)
    assert all(len(c) <= 1400 for c in chunks) and len(chunks) == 3
    assert "[FILE: d/a_small.md]" in chunks[0]
    assert "[FILE: d/b_big.md (lines 1-600)]" in chunks[0]
    assert "[FILE: d/c_small.md]" in chunks[-1]


def test_summarize_refuses_a_target_past_the_ceiling(
    stub, tmp_path, monkeypatch
):
    (tmp_path / "huge.md").write_text("x" * 5000)
    monkeypatch.setattr(server, "NIM_SUMMARIZE_MAX_CHARS", 1000)
    out = _tool(server.nim_summarize)(str(tmp_path), "huge.md")
    assert "ceiling" in out and not stub.posts()


def test_summary_key_points_packed_in_one_string_are_split():
    packed = json.dumps(
        {"summary": "S", "key_points": ["- one\n2. two\n\n\u2022 three"]}
    )
    out = json.loads(server._tidy_summary(packed))
    assert out["key_points"] == ["one", "two", "three"]
    plain = json.dumps({"summary": "S", "key_points": "solo"})
    assert json.loads(server._tidy_summary(plain))["key_points"] == ["solo"]


def test_wrong_directory_with_a_unique_basename_is_verified_and_tagged(
    tmp_path,
):
    root = tmp_path / "w"
    (root / "Tools" / "playtest").mkdir(parents=True)
    (root / "Tools" / "playtest" / "run.sh").write_text("a\nb\nfail here\n")
    (root / "x").mkdir()
    (root / "x" / "dup.py").write_text("1\n")
    (root / "y").mkdir()
    (root / "y" / "dup.py").write_text("1\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    root = root.resolve()
    server._nim_reset_state()
    out, verified, total = server._agent_verify_citations(
        "Tools/run.sh:3 and z/dup.py:1 and run.sh:9", root
    )
    assert "Tools/run.sh:3 [path is Tools/playtest/run.sh]" in out
    assert "z/dup.py:1 [unverified]" in out and "run.sh:9 [unverified]" in out
    assert (verified, total) == (1, 3)
    out, matched, checked = server._agent_check_quotes(
        "- Tools/run.sh:3 - `fail here`", root
    )
    assert (matched, checked) == (1, 1)


# --- transport -----------------------------------------------------------


def test_requests_reuse_one_http_client(stub):
    _tool(server.nim_prompt)("a", use_cache=False)
    first = server._nim_client()
    _tool(server.nim_prompt)("b", use_cache=False)
    assert server._nim_client() is first and len(stub.posts()) == 2
    assert first.follow_redirects is False
    server._nim_reset_state()
    assert first.is_closed and server._nim_client() is not first


@pytest.mark.parametrize(("count", "size"), [(1, 4), (3, 12), (5, 12)])
def test_gate_scales_with_keys_unless_overridden(monkeypatch, count, size):
    keys = ",".join(f"nvapi-k{i}" for i in range(count))
    monkeypatch.setenv("NVIDIA_API_KEYS", keys)
    monkeypatch.delenv("CASTOR_NIM_CONCURRENCY", raising=False)
    assert server._nim_gate_size() == size
    assert server._job_pool_size() == min(size, server.JOB_POOL_MAX)
    monkeypatch.setenv("CASTOR_NIM_CONCURRENCY", "2")
    assert server._nim_gate_size() == 2


def test_background_job_waits_for_a_slot_a_foreground_call_does_not(
    stub, monkeypatch
):
    monkeypatch.setenv("CASTOR_NIM_CONCURRENCY", "1")
    monkeypatch.setattr(server, "NIM_MAX_WAIT", 0.05)
    monkeypatch.setattr(server, "NIM_JOB_MAX_WAIT", 5.0)
    server._nim_reset_state()
    gate = server._nim_gate()
    gate.acquire()
    out = _tool(server.nim_prompt)("fg", use_cache=False)
    assert out.startswith("[NVIDIA NIM is busy")
    threading.Timer(0.3, gate.release).start()
    job = _job_id(_tool(server.nim_start)("bg", use_cache=False))
    assert _poll_until_done(job) == "stub answer"


# --- streaming -----------------------------------------------------------


def _delta(content=None, finish=None, calls=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if calls is not None:
        delta["tool_calls"] = calls
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


USAGE = {"prompt_tokens": 11, "completion_tokens": 7}


def test_streamed_answer_is_rebuilt(stub):
    chunks = [
        _delta("Hel"),
        {"choices": [{"delta": {"reasoning_content": "hmm"}}]},
        _delta("lo", finish="stop"),
        {"choices": [], "usage": USAGE},
    ]
    stub.queue.append((200, {}, {"__sse__": chunks}))
    assert _tool(server.nim_prompt)("q", use_cache=False) == "Hello"


def test_streamed_tool_call_split_across_deltas_is_merged(stub, repo):
    first = {"index": 0, "id": "c1", "type": "function"}
    first["function"] = {"name": "grep", "arguments": '{"pat'}
    rest = {"index": 0, "function": {"arguments": 'tern": "TARGET"}'}}
    stub.queue.append(
        (
            200,
            {},
            {
                "__sse__": [
                    _delta(calls=[first]),
                    _delta(calls=[rest], finish="tool_calls"),
                ]
            },
        )
    )
    stub.queue.append((200, {}, {"__sse__": [_delta("done", "stop")]}))
    out = _tool(server.nim_agent)("find TARGET", str(repo))
    assert out.startswith("done")
    tool_msg = stub.posts()[1][3]["messages"][-1]
    assert tool_msg["tool_call_id"] == "c1"
    assert "src/a.py:2:TARGET = 1" in tool_msg["content"]


def test_streamed_length_finish_keeps_the_truncation_note(stub):
    stub.queue.append(
        (200, {}, {"__sse__": [_delta("partial"), _delta("", "length")]})
    )
    out = _tool(server.nim_prompt)("q", use_cache=False)
    assert out.startswith("partial") and "truncated at max_tokens" in out


def test_stalled_answer_returns_partial_text_with_a_note(stub, monkeypatch):
    monkeypatch.setenv("CASTOR_NIM_TIMEOUT", "0.2")
    spec = {"__sse__": [_delta("half an "), _delta("answer")]}
    stub.queue.append((200, {}, {**spec, "stall_after": 2, "stall": 0.5}))
    out = _tool(server.nim_prompt)("q", use_cache=False)
    assert out.startswith("half an answer")
    assert "[Note: response cut off after a stall of 0s.]" in out
    assert len(stub.posts()) == 1


def test_stalled_tool_step_fails_as_a_timeout_and_retries_once(
    stub, repo, monkeypatch
):
    monkeypatch.setenv("CASTOR_NIM_TIMEOUT", "0.2")
    call = {"index": 0, "id": "c1", "function": {"name": "grep"}}
    stalled = {
        "__sse__": [_delta(calls=[call])],
        "stall_after": 1,
        "stall": 0.5,
    }
    stub.queue.extend([(200, {}, stalled), (200, {}, stalled)])
    out = _tool(server.nim_agent)("t", str(repo))
    assert out.startswith("[NVIDIA NIM error") and "stream stopped" in out
    assert len(stub.posts()) == 2


def test_stream_can_be_turned_off(stub, monkeypatch):
    monkeypatch.setenv("CASTOR_NIM_STREAM", "0")
    _tool(server.nim_prompt)("q", use_cache=False)
    body = stub.posts()[0][3]
    assert body["stream"] is False and "stream_options" not in body


def test_streamed_usage_reaches_the_usage_log(stub, tmp_path):
    stub.queue.append(
        (
            200,
            {},
            {
                "__sse__": [
                    _delta("ok", "stop"),
                    {"choices": [], "usage": USAGE},
                ]
            },
        )
    )
    _tool(server.nim_prompt)("q", use_cache=False)
    entry = _usage_entries(tmp_path)[-1]
    assert entry["prompt_tokens"] == 11 and entry["completion_tokens"] == 7


# --- dekko runner ---------------------------------------------------------


def test_dekko_disabled_by_env(monkeypatch):
    monkeypatch.setenv("CASTOR_NIM_DEKKO", "0")
    assert server._dekko_enabled() is False


def test_dekko_override_beats_env(monkeypatch, fake_dekko):
    monkeypatch.setenv("CASTOR_NIM_DEKKO", "0")
    token = server._dekko_override.set(True)
    try:
        assert server._dekko_enabled() is True
    finally:
        server._dekko_override.reset(token)


def test_dekko_off_without_binary(monkeypatch):
    monkeypatch.delenv("CASTOR_NIM_DEKKO", raising=False)
    monkeypatch.setattr(server, "_dekko_bin", lambda: None)
    assert server._dekko_enabled() is False


def test_dekko_off_below_min_version(monkeypatch, fake_dekko):
    monkeypatch.setattr(server, "_dekko_version", lambda: (1, 9, 0))
    assert server._dekko_enabled() is False


@pytest.mark.parametrize(
    ("os_name", "python", "dekko", "found"),
    [
        ("posix", "bin/python", "bin/dekko", True),
        ("posix", "bin/python", "bin/dekko.exe", False),
        ("nt", "Scripts/python.exe", "Scripts/dekko.exe", True),
        ("nt", "python.exe", "Scripts/dekko.exe", True),
        ("nt", "python.exe", "other/dekko.exe", False),
    ],
)
def test_dekko_bin_layouts(
    monkeypatch, tmp_path, os_name, python, dekko, found
):
    (tmp_path / dekko).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / dekko).write_text("")
    monkeypatch.setattr(server.sys, "executable", str(tmp_path / python))
    # Only _dekko_bin reads server.os here; pathlib keeps the real os.name.
    monkeypatch.setattr(server, "os", SimpleNamespace(name=os_name))
    expect = tmp_path / dekko if found else None
    assert server._dekko_bin() == expect


def test_dekko_run_reports_timeout(monkeypatch, tmp_path):
    def slow(*a, **k):
        raise server.subprocess.TimeoutExpired(a[0], 10)

    monkeypatch.setattr(server.subprocess, "run", slow)
    monkeypatch.setattr(server, "_dekko_bin", lambda: tmp_path / "dekko")
    ok, text = server._dekko_run(tmp_path, ["query", "symbol", "x"])
    assert ok is False and "timed out" in text


def test_ensure_map_runs_once_per_ttl(fake_dekko, tmp_path):
    fake_dekko.queue += [(True, "")]
    assert server._dekko_ensure_map(tmp_path) is True
    assert server._dekko_ensure_map(tmp_path) is True
    assert fake_dekko.calls == [["map", "--if-stale", "--quiet"]]


def test_ensure_map_failure_is_cached(fake_dekko, tmp_path):
    fake_dekko.queue += [(False, "[dekko error: timed out]")]
    assert server._dekko_ensure_map(tmp_path) is False
    assert server._dekko_ensure_map(tmp_path) is False
    assert len(fake_dekko.calls) == 1


def test_ensure_map_disabled_runs_nothing(monkeypatch, fake_dekko, tmp_path):
    monkeypatch.setenv("CASTOR_NIM_DEKKO", "0")
    assert server._dekko_ensure_map(tmp_path) is False
    assert fake_dekko.calls == []


def test_ensure_map_reset_clears_cache(fake_dekko, tmp_path):
    assert server._dekko_ensure_map(tmp_path) is True
    server._nim_reset_state()
    assert server._dekko_ensure_map(tmp_path) is True
    assert len(fake_dekko.calls) == 2


def test_ensure_map_ttl_counts_from_build_end(monkeypatch, tmp_path):
    clock = [1000.0]
    calls = []

    def slow_build(root, args, timeout=10.0):
        calls.append(args)
        clock[0] += server.NIM_DEKKO_MAP_TTL * 3
        return False, "[dekko error: timed out]"

    monkeypatch.setenv("CASTOR_NIM_DEKKO", "1")
    monkeypatch.setattr(server, "_dekko_bin", lambda: tmp_path / "dekko")
    monkeypatch.setattr(server, "_dekko_version", lambda: (1, 9, 35))
    monkeypatch.setattr(server, "_dekko_run", slow_build)
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    assert server._dekko_ensure_map(tmp_path) is False
    clock[0] += 1
    assert server._dekko_ensure_map(tmp_path) is False
    assert len(calls) == 1


def _clocked_dekko(monkeypatch, tmp_path, results):
    """dekko on, a fake clock, and `_dekko_run` popping `results`."""
    clock = [1000.0]
    calls = []

    def run(root, args, timeout=10.0):
        calls.append(args)
        return results.pop(0)

    monkeypatch.setenv("CASTOR_NIM_DEKKO", "1")
    monkeypatch.setattr(server, "_dekko_bin", lambda: tmp_path / "dekko")
    monkeypatch.setattr(server, "_dekko_version", lambda: (1, 9, 35))
    monkeypatch.setattr(server, "_dekko_run", run)
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    return clock, calls


def test_ensure_map_failure_waits_out_the_fail_ttl(monkeypatch, tmp_path):
    clock, calls = _clocked_dekko(
        monkeypatch, tmp_path, [(False, "[dekko error: crash]"), (True, "")]
    )
    assert server._dekko_ensure_map(tmp_path) is False
    clock[0] += server.NIM_DEKKO_MAP_TTL * 2
    assert server._dekko_ensure_map(tmp_path) is False
    assert server._dekko_ensure_map(tmp_path, fresh=True) is False
    assert len(calls) == 1
    clock[0] += server.NIM_DEKKO_MAP_FAIL_TTL
    assert server._dekko_ensure_map(tmp_path) is True
    assert len(calls) == 2


def test_ensure_map_fresh_skips_only_a_cached_success(monkeypatch, tmp_path):
    clock, calls = _clocked_dekko(
        monkeypatch, tmp_path, [(True, ""), (True, ""), (True, "")]
    )
    assert server._dekko_ensure_map(tmp_path) is True
    assert server._dekko_ensure_map(tmp_path) is True
    assert len(calls) == 1
    assert server._dekko_ensure_map(tmp_path, fresh=True) is True
    assert len(calls) == 2
    clock[0] += server.NIM_DEKKO_MAP_TTL
    assert server._dekko_ensure_map(tmp_path) is True
    assert len(calls) == 3


def test_ensure_map_reset_clears_a_cached_failure(fake_dekko, tmp_path):
    fake_dekko.queue += [(False, "[dekko error: crash]")]
    assert server._dekko_ensure_map(tmp_path) is False
    server._nim_reset_state()
    assert server._dekko_ensure_map(tmp_path) is True
    assert len(fake_dekko.calls) == 2


# --- dekko agent tools ----------------------------------------------------


def test_agent_offers_dekko_tools_when_map_builds(stub, repo, fake_dekko):
    fake_dekko.queue += [(True, "")]
    stub.queue.append((200, {}, _completion("src/a.py:2")))
    _tool(server.nim_agent)("where?", str(repo))
    names = [t["function"]["name"] for t in stub.posts()[0][3]["tools"]]
    assert names == [
        "grep",
        "read_range",
        "list_dir",
        "callers",
        "symbol",
        "outline",
        "context",
        "search",
        "trace",
    ]
    assert "Use grep for docs" in stub.posts()[0][3]["messages"][0]["content"]


def test_callers_merges_grep_only_sites(repo, fake_dekko):
    (repo / "src" / "b.py").write_text("from a import f\nf()\n# f() here\n")
    fake_dekko.queue += [
        (
            True,
            json.dumps(
                {
                    "results": [
                        {
                            "path": "src/b.py",
                            "line": 1,
                            "id": "src/b.py::<module>",
                            "sites": [2],
                        }
                    ]
                }
            ),
        )
    ]
    text = server._dekko_tool(repo, "callers", {"symbol": "f"})
    assert "src/b.py:2 in <module>" in text
    assert "grep-only" in text and "src/b.py:3:# f() here" in text
    assert "src/b.py:2:f()" not in text


def test_agent_run_without_map_uses_grep_tools_only(stub, repo, fake_dekko):
    fake_dekko.queue += [(False, "[dekko error: crash]")]
    stub.queue.append((200, {}, _completion("nothing at src/a.py:2")))
    _tool(server.nim_agent)("where is TARGET?", str(repo))
    names = [t["function"]["name"] for t in stub.posts()[0][3]["tools"]]
    assert names == ["grep", "read_range", "list_dir"]


def _callers_reply(stub, repo):
    """Run nim_agent where the model calls `callers`; return the reply."""
    stub.queue.append((200, {}, _tool_call("callers", {"symbol": "f"})))
    stub.queue.append((200, {}, _completion("done")))
    _tool(server.nim_agent)("who calls f?", str(repo))
    return stub.posts()[1][3]["messages"][-1]["content"]


_UNKNOWN_CALLERS = "[error: unknown tool 'callers']"


def test_dekko_tool_switched_off_is_an_unknown_tool(
    stub, repo, fake_dekko, monkeypatch
):
    monkeypatch.setenv("CASTOR_NIM_DEKKO", "0")
    assert _callers_reply(stub, repo) == _UNKNOWN_CALLERS
    assert fake_dekko.calls == []


def test_dekko_tool_overridden_off_is_an_unknown_tool(stub, repo, fake_dekko):
    token = server._dekko_override.set(False)
    try:
        assert _callers_reply(stub, repo) == _UNKNOWN_CALLERS
    finally:
        server._dekko_override.reset(token)
    assert fake_dekko.calls == []


def test_dekko_tool_without_a_map_is_an_unknown_tool(stub, repo, fake_dekko):
    fake_dekko.queue += [(False, "[dekko error: crash]")]
    assert _callers_reply(stub, repo) == _UNKNOWN_CALLERS
    assert fake_dekko.calls == [["map", "--if-stale", "--quiet"]]


def test_offered_dekko_tool_runs(stub, repo, fake_dekko):
    sites = {"results": [], "module_level": []}
    fake_dekko.queue += [(True, ""), (True, json.dumps(sites))]
    assert _callers_reply(stub, repo).startswith("callers of f (dekko")
    assert fake_dekko.calls[1][:3] == ["query", "callers", "f"]


def test_run_tool_offers_no_dekko_tool_by_default(repo, fake_dekko):
    out = server._agent_run_tool(repo, "callers", {"symbol": "f"})
    assert out == _UNKNOWN_CALLERS and fake_dekko.calls == []


def test_dekko_tool_refuses_when_dekko_is_off(repo, fake_dekko, monkeypatch):
    monkeypatch.setenv("CASTOR_NIM_DEKKO", "0")
    out = server._dekko_tool(repo, "symbol", {"name": "f"})
    assert out == "[error: unknown tool 'symbol']" and fake_dekko.calls == []


def test_dekko_tool_failure_is_a_tool_error(repo, fake_dekko):
    fake_dekko.queue += [(False, "[dekko error: exit 5]")]
    assert (
        server._dekko_tool(repo, "outline", {"path": "src/a.py"})
        == "[tool error: dekko error: exit 5]"
    )


# Real `dekko ... --json` shapes (1.9.35), trimmed, from a repo with
# src/a.py (target), src/mod.py (module-level call), and two denied files:
# src/.env_helpers.py (leaky) and .ssh/tool.py (sshy), both calling target.
_LEAKY = {
    "id": "src/.env_helpers.py::leaky",
    "path": "src/.env_helpers.py",
    "line": 3,
    "kind": "function",
    "signature": "leaky()",
    "doc": None,
}
_SSHY = {
    "id": ".ssh/tool.py::sshy",
    "path": ".ssh/tool.py",
    "line": 3,
    "kind": "function",
    "signature": "sshy()",
    "doc": None,
}
_TARGET = {
    "id": "src/a.py::target",
    "path": "src/a.py",
    "line": 1,
    "kind": "function",
    "signature": "target()",
    "doc": None,
}
_SECRET_SRC = 'def leaky():\n    SECRET_TOKEN = "abc123"\n    return target()'
_REAL = {
    "context_leaky": {
        "label": "src/.env_helpers.py:leaky",
        "target": _LEAKY,
        "file": "src/.env_helpers.py",
        "imports": [{"name": "target", "source": "src.a.target"}],
        "neighbors": [{"hop": 1, "direction": "callee", **_TARGET}],
        "module_callers": [],
        "source": _SECRET_SRC,
    },
    "context_target": {
        "label": "src/a.py:target",
        "target": _TARGET,
        "file": "src/a.py",
        "imports": [],
        "neighbors": [
            {"hop": 1, "direction": "caller", **_SSHY},
            {"hop": 1, "direction": "caller", **_LEAKY},
            {
                "hop": 1,
                "direction": "caller",
                "id": "src/mod.py::<module>",
                "path": "src/mod.py",
                "line": 3,
                "kind": "module",
                "signature": "<anonymous> (src/mod.py)",
                "doc": None,
            },
        ],
        "module_callers": ["src/mod.py"],
        "source": "def target():\n    return 1",
    },
    "symbol_leaky": {**_LEAKY, "language": "python", "end_line": 5},
    "symbol_target": {**_TARGET, "language": "python", "end_line": 2},
    "search_leaky": {"query": "leaky", "hits": [{**_LEAKY, "score": 0.85}]},
    "outline_src": {
        "target": "src",
        "files": [
            {
                "path": "src/.env_helpers.py",
                "language": "python",
                "symbols": [
                    {"line": 3, "kind": "function", "signature": "leaky()"}
                ],
            },
            {
                "path": "src/a.py",
                "language": "python",
                "symbols": [
                    {"line": 1, "kind": "function", "signature": "target()"}
                ],
            },
        ],
    },
    "trace_denied": {
        "from": ".ssh/tool.py::sshy",
        "to": "src/a.py::target",
        "paths": [[_SSHY, _TARGET]],
    },
    "callers_target": {
        "action": "callers",
        "target": "src/a.py::target",
        "results": [
            {**_SSHY, "sites": [5]},
            {**_LEAKY, "sites": [5]},
        ],
        "module_level": [
            {"path": "src/mod.py", "lines": [3]},
            {"path": ".ssh/tool.py", "lines": [9]},
        ],
    },
}
_DENIED_WORDS = ("env_helpers", ".ssh", "SECRET_TOKEN", "leaky", "sshy")


@pytest.fixture
def deny_repo(tmp_path):
    root = tmp_path / "deny"
    (root / "src").mkdir(parents=True)
    (root / ".ssh").mkdir()
    (root / "src" / "a.py").write_text("def target():\n    return 1\n")
    (root / "src" / "mod.py").write_text(
        "from src.a import target\n\ntarget()\n"
    )
    (root / "src" / ".env_helpers.py").write_text("target()  # SECRET_TOKEN\n")
    (root / ".ssh" / "tool.py").write_text("target()  # sshy\n")
    return root.resolve()


def _json(key):
    return (True, json.dumps(_REAL[key]))


@pytest.mark.parametrize(
    ("name", "args", "key", "expect"),
    [
        ("context", {"symbol": "target"}, "context_target", "src/mod.py:3"),
        ("symbol", {"name": "target"}, "symbol_target", "src/a.py:1"),
        ("outline", {"path": "src"}, "outline_src", "target()"),
    ],
)
def test_dekko_tool_renders_without_denied_files(
    deny_repo, fake_dekko, name, args, key, expect
):
    fake_dekko.queue += [_json(key)]
    text = server._dekko_tool(deny_repo, name, args)
    assert expect in text
    assert not [w for w in _DENIED_WORDS if w in text]


@pytest.mark.parametrize(
    ("name", "args", "key"),
    [
        ("context", {"symbol": "leaky"}, "context_leaky"),
        ("symbol", {"name": "leaky"}, "symbol_leaky"),
        ("search", {"text": "leaky"}, "search_leaky"),
    ],
)
def test_dekko_tool_denied_target_returns_only_an_error(
    deny_repo, fake_dekko, name, args, key
):
    fake_dekko.queue += [_json(key)]
    assert server._dekko_tool(deny_repo, name, args) == "[error: denied]"


def test_dekko_trace_through_denied_files_shows_no_path(deny_repo, fake_dekko):
    fake_dekko.queue += [_json("trace_denied")]
    text = server._dekko_tool(
        deny_repo, "trace", {"source": "sshy", "target": "target"}
    )
    assert text == "[no call path found]"


def test_dekko_trace_exit_one_is_no_path(deny_repo, fake_dekko):
    fake_dekko.queue += [(False, '[dekko error: exit 1: {"paths": []}]')]
    text = server._dekko_tool(
        deny_repo, "trace", {"source": "a", "target": "b"}
    )
    assert text == "[no call path found]"


def test_callers_drops_denied_and_keeps_module_level(deny_repo, fake_dekko):
    fake_dekko.queue += [_json("callers_target"), _json("symbol_target")]
    text = server._dekko_tool(deny_repo, "callers", {"symbol": "target"})
    assert "src/mod.py:3 in <module>" in text
    assert "(dekko, 1 sites)" in text
    assert not [w for w in _DENIED_WORDS if w in text]
    assert "src/mod.py:3:target()" not in text  # known site, not grep-only
    assert "src/a.py:1:" not in text  # the definition line is excluded
    assert "src/mod.py:1:from src.a import target" in text


def test_callers_sites_include_module_level(deny_repo, fake_dekko):
    fake_dekko.queue += [_json("callers_target")]
    sites, _ = server._dekko_callers(deny_repo, "target")
    assert sites == [("src/mod.py", 3)]


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("callers", {"symbol": "--root"}),
        ("symbol", {"name": "-x"}),
        ("outline", {"path": "-h"}),
        ("context", {"symbol": "--json"}),
        ("search", {"text": "-q"}),
        ("trace", {"source": "a", "target": "--no-regen"}),
    ],
)
def test_dekko_tool_rejects_option_like_arguments(
    deny_repo, fake_dekko, name, args
):
    text = server._dekko_tool(deny_repo, name, args)
    assert text == '[error: argument must not start with "-"]'
    assert fake_dekko.calls == []


def test_dekko_tool_argv(deny_repo, fake_dekko):
    fake_dekko.queue += [_json("symbol_target")] * 6
    for name, args in [
        ("symbol", {"name": "target"}),
        ("outline", {"path": "src/a.py"}),
        ("context", {"symbol": "target"}),
        ("search", {"text": "target"}),
        ("trace", {"source": "a", "target": "b"}),
    ]:
        server._dekko_tool(deny_repo, name, args)
    budget = ["--budget", "1200"]
    assert fake_dekko.calls == [
        ["query", "symbol", "target", "--json", "--no-regen", *budget],
        ["outline", "src/a.py", "--json", "--no-regen", *budget],
        [
            "context",
            "target",
            "--with-source",
            "--json",
            "--no-regen",
            *budget,
        ],
        ["search", "target", "--json", "--no-regen", *budget],
        ["trace", "a", "b", "--json", "--no-regen"],
    ]


def test_callers_argv_and_definition_query(deny_repo, fake_dekko):
    fake_dekko.queue += [_json("callers_target"), _json("symbol_target")]
    server._dekko_tool(deny_repo, "callers", {"symbol": "target"})
    budget = ["--budget", "1200"]
    assert fake_dekko.calls == [
        [
            "query",
            "callers",
            "target",
            "--sites",
            "--json",
            "--no-regen",
            *budget,
        ],
        ["query", "symbol", "target", "--json", "--no-regen", *budget],
    ]


# Real dekko 1.9.35 stderr for a name defined in a denied and an allowed file
# (exit 4, printed even with --json).
_AMBIGUOUS = (
    False,
    "[dekko error: exit 4: dekko: 'dup' is ambiguous; candidates:\n"
    "  src/.env_helpers.py:6  dup()\n"
    "  src/a.py:3  dup()]",
)
_AMBIGUOUS_ALL_DENIED = (
    False,
    "[dekko error: exit 4: dekko: 'dup' is ambiguous; candidates:\n"
    "  src/.env_helpers.py:6  dup()\n"
    "  .ssh/tool.py:2  dup()]",
)


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("symbol", {"name": "dup"}),
        ("context", {"symbol": "dup"}),
        ("callers", {"symbol": "dup"}),
        ("trace", {"source": "dup", "target": "dup"}),
        ("search", {"text": "dup"}),
    ],
)
def test_dekko_ambiguity_error_lists_allowed_candidates_only(
    deny_repo, fake_dekko, name, args
):
    fake_dekko.queue += [_AMBIGUOUS]
    text = server._dekko_tool(deny_repo, name, args)
    assert "env_helpers" not in text
    assert "src/a.py:3  dup()" in text and "ambiguous" in text


def test_dekko_ambiguity_with_only_denied_candidates(deny_repo, fake_dekko):
    fake_dekko.queue += [_AMBIGUOUS_ALL_DENIED]
    text = server._dekko_tool(deny_repo, "symbol", {"name": "dup"})
    assert text == "[tool error: no symbol matches]"


def test_dekko_other_error_lines_naming_denied_paths_are_dropped(
    deny_repo, fake_dekko
):
    fake_dekko.queue += [
        (False, "[dekko error: exit 5: bad\nsrc/.env_helpers.py broke\nok]")
    ]
    text = server._dekko_tool(deny_repo, "outline", {"path": "src/a.py"})
    assert "env_helpers" not in text and "bad" in text


def test_dekko_callers_failure_text_is_scrubbed(deny_repo, fake_dekko):
    fake_dekko.queue += [_AMBIGUOUS]
    sites, text = server._dekko_callers(deny_repo, "dup")
    assert sites is None and "env_helpers" not in text


def test_dekko_trace_exit_ten_is_an_error_not_no_path(deny_repo, fake_dekko):
    fake_dekko.queue += [(False, "[dekko error: exit 10: boom]")]
    text = server._dekko_tool(
        deny_repo, "trace", {"source": "a", "target": "b"}
    )
    assert text == "[tool error: dekko error: exit 10: boom]"


def test_parse_edit_blocks_reads_two_blocks():
    answer = (
        "Here you go\n<<<EDIT src/a.py\n<<<OLD\nTARGET = 1\n>>>NEW\n"
        "TARGET = 2\n>>>END\n<<<EDIT b.md\n<<<OLD\nx\n>>>NEW\ny\n>>>END\n"
    )
    blocks, errors = server._parse_edit_blocks(answer)
    assert errors == []
    assert blocks[0] == server.EditBlock(
        1, "src/a.py", "TARGET = 1", "TARGET = 2"
    )
    assert blocks[1].path == "b.md"


def test_parse_edit_blocks_reports_missing_marker():
    _, errors = server._parse_edit_blocks("<<<EDIT a.py\n<<<OLD\nx\n>>>END\n")
    assert errors == ["block 1: missing >>>NEW"]


def test_marker_text_inside_a_line_is_code():
    answer = "<<<EDIT a.py\n<<<OLD\ns = '>>>END'\n>>>NEW\ns = ''\n>>>END\n"
    blocks, errors = server._parse_edit_blocks(answer)
    assert errors == [] and blocks[0].old == "s = '>>>END'"


def test_empty_new_text_deletes():
    blocks, _ = server._parse_edit_blocks(
        "<<<EDIT a.py\n<<<OLD\nx\n>>>NEW\n>>>END\n"
    )
    assert blocks[0].new == ""


def test_parse_edit_blocks_reports_missing_end_at_eof():
    blocks, errors = server._parse_edit_blocks(
        "<<<EDIT a.py\n<<<OLD\nx\n>>>NEW\ny\n"
    )
    assert blocks == []
    assert errors == ["block 1: missing >>>END"]


def test_parse_edit_blocks_reports_missing_path():
    blocks, errors = server._parse_edit_blocks(
        "<<<EDIT \n<<<OLD\nx\n>>>NEW\ny\n>>>END\n"
        "<<<EDIT a.py\n<<<OLD\nx\n>>>NEW\ny\n>>>END\n"
    )
    assert errors == ["block 1: missing path"]
    assert [b.index for b in blocks] == [2]


def _blk(index, path, old, new):
    return server.EditBlock(index, path, old, new)


def test_apply_replaces_exactly_once(repo):
    changes, errors = server._edit_apply_in_memory(
        [_blk(1, "src/a.py", "TARGET = 1", "TARGET = 2")], repo, None
    )
    assert errors == []
    edit = changes[repo / "src" / "a.py"]
    assert edit.new == "one\nTARGET = 2\nthree\n"
    assert edit.original == "one\nTARGET = 1\nthree\n"
    assert edit.relative == "src/a.py"
    assert edit.spans == [(2, 2)]


def test_apply_rejects_missing_and_duplicate_old_text(repo):
    (repo / "src" / "d.py").write_text("x\nx\n")
    changes, errors = server._edit_apply_in_memory(
        [
            _blk(3, "src/a.py", "nope", "y"),
            _blk(4, "src/d.py", "x", "y"),
            _blk(5, "src/a.py", "", "y"),
        ],
        repo,
        None,
    )
    assert errors == [
        "block 3: old text not found in src/a.py",
        "block 4: old text matches 2 times in src/d.py",
        "block 5: old text is empty",
    ]
    assert changes == {}


def test_apply_counts_overlapping_occurrences(repo):
    (repo / "src" / "d.py").write_text("aaa")
    _, errors = server._edit_apply_in_memory(
        [_blk(1, "src/d.py", "aa", "b")], repo, None
    )
    assert errors == ["block 1: old text matches 2 times in src/d.py"]


def test_apply_rejects_out_of_scope_denied_and_escaping_paths(repo):
    (repo / "README.md").write_text("TARGET\n")
    (repo / "src" / "link.txt").symlink_to(repo.parent / "outside.txt")
    blocks = [
        _blk(1, "README.md", "TARGET", "x"),
        _blk(2, ".env", "TARGET", "x"),
        _blk(3, "../outside.txt", "TARGET", "x"),
        _blk(4, "src/link.txt", "TARGET", "x"),
    ]
    _, errors = server._edit_apply_in_memory(blocks, repo, ["src"])
    assert errors == [
        "block 1: outside scope",
        "block 2: denied",
        "block 3: outside cwd",
        "block 4: outside cwd",
    ]
    _, errors = server._edit_apply_in_memory(blocks[:1], repo, ["./src/a.py"])
    assert errors == ["block 1: outside scope"]
    changes, errors = server._edit_apply_in_memory(
        [_blk(1, "src/a.py", "TARGET = 1", "x")], repo, ["./src/"]
    )
    assert errors == [] and len(changes) == 1


def test_apply_refuses_an_edit_inside_dekko_folder(repo):
    (repo / ".dekko").mkdir()
    (repo / ".dekko" / "notes.json").write_text('{"TARGET": 1}\n')
    changes, errors = server._edit_apply_in_memory(
        [_blk(1, ".dekko/notes.json", "TARGET", "x")], repo, None
    )
    assert changes == {} and errors == ["block 1: denied"]


def test_apply_rejects_overlapping_blocks_even_when_paths_differ_in_form(repo):
    changes, errors = server._edit_apply_in_memory(
        [
            _blk(2, "./src/a.py", "TARGET = 1", "TARGET = 2"),
            _blk(4, "src/a.py", "TARGET", "OTHER"),
        ],
        repo,
        None,
    )
    assert errors == ["block 4: overlaps block 2 in src/a.py"]
    assert changes == {}


def test_apply_applies_several_blocks_in_one_pass(repo):
    changes, errors = server._edit_apply_in_memory(
        [
            _blk(1, "src/a.py", "three", "3\n3"),
            _blk(2, "src/a.py", "one", "1"),
        ],
        repo,
        None,
    )
    assert errors == []
    edit = changes[repo / "src" / "a.py"]
    assert edit.new == "1\nTARGET = 1\n3\n3\n"
    assert sorted(edit.spans) == [(1, 1), (3, 3)]


def test_apply_keeps_crlf(repo):
    (repo / "src" / "b.py").write_bytes(b"a\r\nTARGET = 1\r\n")
    changes, errors = server._edit_apply_in_memory(
        [_blk(1, "src/b.py", "a\nTARGET = 1", "a\nTARGET = 2\nz")], repo, None
    )
    assert errors == []
    edit = changes[repo / "src" / "b.py"]
    assert edit.new.encode() == b"a\r\nTARGET = 2\r\nz\r\n"
    assert edit.spans == [(1, 2)]


def test_apply_handles_non_ascii(repo):
    text = "café\nnaïve “quote”\nend\n"
    (repo / "src" / "u.py").write_bytes(text.encode())
    changes, errors = server._edit_apply_in_memory(
        [_blk(1, "src/u.py", "naïve “quote”", "plain")],
        repo,
        None,
    )
    assert errors == []
    edit = changes[repo / "src" / "u.py"]
    assert edit.new.encode() == "café\nplain\nend\n".encode()
    assert edit.original.encode() == text.encode()


def test_apply_refuses_new_files_binary_and_bad_utf8(repo):
    (repo / "src" / "bad.txt").write_bytes(b"caf\xe9 TARGET\n")
    _, errors = server._edit_apply_in_memory(
        [
            _blk(1, "src/new.py", "x", "y"),
            _blk(2, "src/blob.bin", "TARGET", "y"),
            _blk(3, "src", "x", "y"),
            _blk(4, "src/bad.txt", "TARGET", "y"),
        ],
        repo,
        None,
    )
    assert errors == [
        "block 1: file creation is not supported",
        "block 2: binary file",
        "block 3: file creation is not supported",
        "block 4: not UTF-8",
    ]


def test_apply_span_excludes_leading_and_trailing_terminators(repo):
    def span(name, data, old):
        (repo / "src" / name).write_bytes(data)
        changes, errors = server._edit_apply_in_memory(
            [_blk(1, f"src/{name}", old, "X")], repo, None
        )
        assert errors == []
        return changes[repo / "src" / name].spans

    assert span("s1.py", b"one\nTARGET = 1\nthree\n", "\nTARGET") == [(2, 2)]
    assert span("s2.py", b"one\r\nTARGET = 1\r\nthree\r\n", "\nTARGET") == [
        (2, 2)
    ]
    assert span("s3.py", b"one\nTARGET = 1\nthree\n", "TARGET = 1\n") == [
        (2, 2)
    ]
    assert span("s4.py", b"one\ntwo\nthree\n", "two\nthree\n") == [(2, 3)]
    assert span("s6.py", b"one\ntwo", "\n") == [(1, 1)]


def test_apply_rejects_identical_old_and_new(repo):
    _, errors = server._edit_apply_in_memory(
        [_blk(1, "src/a.py", "three", "three")], repo, None
    )
    assert errors == ["block 1: old and new text are identical"]


def test_apply_mixed_endings_convert_whole_block_to_crlf(repo):
    (repo / "src" / "m.py").write_bytes(b"a\nb\r\nc\r\n")
    changes, errors = server._edit_apply_in_memory(
        [_blk(1, "src/m.py", "b\nc", "b\nx\nc")], repo, None
    )
    assert errors == []
    assert changes[repo / "src" / "m.py"].new.encode() == b"a\nb\r\nx\r\nc\r\n"


def test_apply_reports_a_file_that_cannot_be_read_per_block(repo):
    (repo / "src" / "bad.txt").write_bytes(b"caf\xe9\n")
    _, errors = server._edit_apply_in_memory(
        [
            _blk(1, "src/bad.txt", "x", "y"),
            _blk(2, "src/bad.txt", "x", "z"),
        ],
        repo,
        None,
    )
    assert errors == ["block 1: not UTF-8", "block 2: not UTF-8"]


def test_apply_file_scope_is_exact(repo):
    (repo / "src" / "a.py.bak").write_text("TARGET = 1\n")
    blocks = [
        _blk(1, "src/a.py", "TARGET = 1", "x"),
        _blk(2, "src/a.py.bak", "TARGET = 1", "x"),
    ]
    changes, errors = server._edit_apply_in_memory(
        blocks, repo, ["./src/a.py"]
    )
    assert errors == ["block 2: outside scope"]
    assert list(changes) == [repo / "src" / "a.py"]
    changes, errors = server._edit_apply_in_memory(
        blocks[:1], repo, ["./src/a.py"]
    )
    assert errors == [] and list(changes) == [repo / "src" / "a.py"]


def _patch_repo(tmp_path, files):
    root = tmp_path / "gitrepo"
    root.mkdir()
    for rel, data in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    for cmd in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "t"],
        ["add", "-A"],
        ["commit", "-qm", "init"],
    ):
        subprocess.run(["git", "-C", str(root), *cmd], check=True)
    return root


def _apply_edits(tmp_path, monkeypatch, files, blocks):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    root = _patch_repo(tmp_path, files)
    changes, errors = server._edit_apply_in_memory(blocks, root, None)
    assert errors == []
    diff, checked, detail = server._edit_make_patch(changes, root)
    assert (checked, detail) == (True, "")
    patch = server._edit_write_patch(diff)
    subprocess.run(["git", "-C", str(root), "apply", str(patch)], check=True)
    return root, changes, diff


def test_diff_round_trips_through_git_apply(tmp_path, monkeypatch):
    root, changes, _ = _apply_edits(
        tmp_path,
        monkeypatch,
        {"src/a.py": b"one\nTARGET = 1\nthree\n"},
        [_blk(1, "src/a.py", "TARGET = 1", "TARGET = 2")],
    )
    edit = changes[root / "src" / "a.py"]
    assert (root / "src" / "a.py").read_text() == edit.new


def test_diff_keeps_a_missing_final_newline(tmp_path, monkeypatch):
    root, _, diff = _apply_edits(
        tmp_path,
        monkeypatch,
        {"src/a.py": b"a\nTARGET = 1"},
        [_blk(1, "src/a.py", "TARGET = 1", "TARGET = 2")],
    )
    assert (root / "src" / "a.py").read_bytes() == b"a\nTARGET = 2"
    assert "\\ No newline at end of file" in diff


def test_diff_handles_a_path_with_a_space(tmp_path, monkeypatch):
    root, _, diff = _apply_edits(
        tmp_path,
        monkeypatch,
        {"docs/my notes.md": b"x\nold\n"},
        [_blk(1, "docs/my notes.md", "old", "new")],
    )
    assert (root / "docs" / "my notes.md").read_text() == "x\nnew\n"
    assert '--- "a/docs/my notes.md"' in diff


def test_diff_quotes_non_ascii_paths(tmp_path, monkeypatch):
    root, _, _ = _apply_edits(
        tmp_path,
        monkeypatch,
        {"docs/café.md": b"old\n"},
        [_blk(1, "docs/café.md", "old", "new")],
    )
    assert (root / "docs" / "café.md").read_text() == "new\n"


def test_diff_keeps_crlf_bytes(tmp_path, monkeypatch):
    root, _, _ = _apply_edits(
        tmp_path,
        monkeypatch,
        {"w.txt": b"a\r\nold\r\nc\r\n"},
        [_blk(1, "w.txt", "old", "new")],
    )
    assert (root / "w.txt").read_bytes() == b"a\r\nnew\r\nc\r\n"


def test_diff_with_two_files_is_sorted(tmp_path, monkeypatch):
    root, _, diff = _apply_edits(
        tmp_path,
        monkeypatch,
        {"b.py": b"x = 1\n", "a.py": b"y = 1\n"},
        [_blk(1, "b.py", "x = 1", "x = 2"), _blk(2, "a.py", "y = 1", "y = 2")],
    )
    assert diff.index("a/a.py") < diff.index("a/b.py")
    assert (root / "a.py").read_text() == "y = 2\n"
    assert (root / "b.py").read_text() == "x = 2\n"


def test_git_check_reports_a_failing_patch(tmp_path):
    root = _patch_repo(tmp_path, {"a.py": b"one\n"})
    edit = server.FileEdit(root / "a.py", "a.py", "zzz\n", "yyy\n", [(1, 1)])
    _, ok, err = server._edit_make_patch({root / "a.py": edit}, root)
    assert ok is False and err


def test_git_check_outside_a_repo_returns_none(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert server._edit_git_check("", plain) == (None, "")


def test_patch_file_written_and_pruned(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    old = server._edit_write_patch("old\n")
    age = time.time() - 31 * 86400
    os.utime(old, (age, age))
    new = server._edit_write_patch("new\n")
    assert not old.exists() and new.exists()
    assert new.parent.name == server.NIM_EDIT_PATCH_DIR
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}\.diff", new.name)
    assert new.read_bytes() == b"new\n"


def test_diff_from_a_subdirectory_root_uses_the_repo_top_level(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    top = _patch_repo(
        tmp_path, {"f.txt": b"top\nold\n", "pkg/f.txt": b"pkg\nold\n"}
    )
    root = top / "pkg"
    changes, errors = server._edit_apply_in_memory(
        [_blk(1, "f.txt", "old", "new")], root, None
    )
    assert errors == []
    tree, reason = server._edit_work_tree(root)
    assert reason is None and tree.resolve() == top.resolve()
    prefix = server._edit_repo_prefix(root, tree)
    assert prefix == "pkg/"
    diff = server._edit_build_diff(changes, prefix)
    assert "--- a/pkg/f.txt" in diff
    assert server._edit_git_check(diff, root) == (True, "")
    patch = server._edit_write_patch(diff)
    subprocess.run(["git", "-C", str(top), "apply", str(patch)], check=True)
    assert (top / "pkg" / "f.txt").read_text() == "pkg\nnew\n"
    assert (top / "f.txt").read_text() == "top\nold\n"


def test_git_check_fails_on_a_skipped_patch(tmp_path, monkeypatch):
    top = _patch_repo(tmp_path, {"f.txt": b"old\n"})
    real = server.subprocess.run

    def run(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "apply"]:
            return subprocess.CompletedProcess(
                cmd, 0, "", "Skipped patch 'f.txt'."
            )
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(server.subprocess, "run", run)
    ok, message = server._edit_git_check("x", top)
    assert ok is False and "Skipped patch" in message


def test_git_check_says_when_git_could_not_run(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(server.subprocess, "run", boom)
    ok, message = server._edit_git_check("x", tmp_path)
    assert ok is False
    assert message.startswith("git check could not run:")


def test_diff_keeps_form_feed_and_lone_cr(tmp_path, monkeypatch):
    root, _, _ = _apply_edits(
        tmp_path,
        monkeypatch,
        {"w.txt": b"a\x0cb\rc\nold\nz\n"},
        [_blk(1, "w.txt", "old", "new")],
    )
    assert (root / "w.txt").read_bytes() == b"a\x0cb\rc\nnew\nz\n"


def test_prune_continues_past_a_failing_unlink(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    folder = server._cache_dir() / server.NIM_EDIT_PATCH_DIR
    folder.mkdir(parents=True)
    age = time.time() - 31 * 86400
    files = [folder / "a.diff", folder / "b.diff"]
    for f in files:
        f.write_text("x")
        os.utime(f, (age, age))
    real = Path.unlink
    calls = []

    def flaky(self, *args, **kwargs):
        calls.append(self.name)
        if len(calls) == 1:
            raise PermissionError("no")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky)
    server._edit_prune_patches()
    assert len(calls) == 2
    assert sum(f.exists() for f in files) == 1


def test_make_patch_from_a_subdirectory_with_a_decoy(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    top = _patch_repo(
        tmp_path, {"f.txt": b"top\nold\n", "pkg/f.txt": b"pkg\nold\n"}
    )
    changes, errors = server._edit_apply_in_memory(
        [_blk(1, "f.txt", "old", "new")], top / "pkg", None
    )
    assert errors == []
    diff, checked, detail = server._edit_make_patch(changes, top / "pkg")
    assert (checked, detail) == (True, "")
    assert "--- a/pkg/f.txt" in diff
    patch = server._edit_write_patch(diff)
    subprocess.run(["git", "-C", str(top), "apply", str(patch)], check=True)
    assert (top / "pkg" / "f.txt").read_text() == "pkg\nnew\n"
    assert (top / "f.txt").read_text() == "top\nold\n"


def test_build_diff_requires_a_prefix():
    with pytest.raises(TypeError):
        server._edit_build_diff({})


def test_make_patch_outside_a_repo_is_unchecked(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("old\n")
    changes, _ = server._edit_apply_in_memory(
        [_blk(1, "a.txt", "old", "new")], plain, None
    )
    diff, checked, detail = server._edit_make_patch(changes, plain)
    assert checked is None and "patch -p1" in detail
    assert "--- a/a.txt" in diff


def test_work_tree_ignores_the_locale(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    assert server._edit_work_tree(plain) == (None, None)


def test_make_patch_refuses_a_root_outside_the_top(tmp_path, monkeypatch):
    top = _patch_repo(tmp_path, {"a.py": b"one\n"})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(server, "_edit_work_tree", lambda root: (top, None))
    diff, checked, detail = server._edit_make_patch({}, elsewhere)
    assert (diff, checked) == ("", False)
    assert detail.startswith("git check could not run: cwd is not inside")


def test_main_guard_is_the_last_top_level_statement():
    import ast

    tree = ast.parse(Path(server.__file__).read_text())
    last = tree.body[-1]
    assert isinstance(last, ast.If)
    assert ast.unparse(last.test) == "__name__ == '__main__'"


# --- nim_edit (check, repair, patch) ----------------------------------------


def _edit_answer(*blocks):
    """An answer with one edit block per (path, old, new)."""
    parts = [
        f"<<<EDIT {path}\n<<<OLD\n{old}\n>>>NEW\n{new}\n>>>END"
        for path, old, new in blocks
    ]
    return "\n".join(parts)


_GOOD_EDIT = _edit_answer(("src/a.py", "TARGET = 1", "TARGET = 2"))
_BAD_EDIT = _edit_answer(("src/a.py", "TARGET = 9", "TARGET = 2"))


def _git_init(root):
    """Turn the `repo` fixture into a real git repo with one commit."""
    import shutil

    shutil.rmtree(root / ".git")
    for cmd in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "t"],
        ["add", "-A"],
        ["commit", "-qm", "init"],
    ):
        subprocess.run(["git", "-C", str(root), *cmd], check=True)


def _patches():
    folder = server._cache_dir() / server.NIM_EDIT_PATCH_DIR
    return sorted(folder.glob("*.diff")) if folder.is_dir() else []


def _answers(stub, *texts):
    for text in texts:
        stub.queue.append((200, {}, _completion(text)))


def test_nim_edit_returns_a_checked_patch(stub, repo):
    _git_init(repo)
    _answers(stub, _GOOD_EDIT)
    out = _tool(server.nim_edit)("set TARGET to 2", str(repo))
    lines = out.split("\n")
    assert lines[0] == "[nim_edit: OK, 1 files, 1 blocks, repair round: no]"
    (patch,) = _patches()
    assert lines[1] == f"patch: {patch}"
    assert patch.is_absolute()
    assert lines[2] == f"base: {server._repo_fingerprint(str(repo))}"
    assert lines[3] == "checked: git apply --check passed"
    assert "-TARGET = 1\n+TARGET = 2\n" in out
    assert patch.read_text() == "\n".join(lines[4:])
    assert (repo / "src" / "a.py").read_text() == "one\nTARGET = 1\nthree\n"
    assert len(stub.posts()) == 1
    request = stub.posts()[0][3]
    assert "<<<EDIT" in request["messages"][0]["content"]
    assert "set TARGET to 2" in request["messages"][1]["content"]


def test_nim_edit_outside_git_is_not_checked(stub, tmp_path):
    plain = tmp_path / "plain"
    (plain / "src").mkdir(parents=True)
    (plain / "src" / "a.py").write_text("one\nTARGET = 1\nthree\n")
    _answers(stub, _GOOD_EDIT)
    out = _tool(server.nim_edit)("set TARGET to 2", str(plain))
    lines = out.split("\n")
    assert lines[0] == "[nim_edit: OK, 1 files, 1 blocks, repair round: no]"
    assert lines[2] == "base: none"
    assert lines[3] == "checked: not checked by git (apply with patch -p1)"


def test_nim_edit_repairs_once_then_succeeds(stub, repo):
    _git_init(repo)
    _answers(stub, _BAD_EDIT, _GOOD_EDIT)
    out = _tool(server.nim_edit)("set TARGET to 2", str(repo))
    assert out.split("\n")[0] == (
        "[nim_edit: OK, 1 files, 1 blocks, repair round: yes]"
    )
    posts = stub.posts()
    assert len(posts) == 2
    repair = posts[1][3]["messages"][1]["content"]
    assert "set TARGET to 2" in repair
    assert "TARGET = 9" in repair
    assert "block 1: old text not found in src/a.py" in repair
    assert len(_patches()) == 1


def test_nim_edit_refuses_after_failed_repair(stub, repo):
    _answers(stub, _BAD_EDIT, _BAD_EDIT)
    out = _tool(server.nim_edit)("set TARGET to 2", str(repo))
    assert out.split("\n") == [
        "[nim_edit: REFUSED after repair round]",
        "- block 1: old text not found in src/a.py",
    ]
    assert len(stub.posts()) == 2
    assert _patches() == []


def test_nim_edit_refuses_an_answer_without_blocks(stub, repo):
    _answers(stub, "I could not find it.", "Still nothing.")
    out = _tool(server.nim_edit)("set TARGET to 2", str(repo))
    assert out.split("\n") == [
        "[nim_edit: REFUSED after repair round]",
        "- no edit blocks in the answer",
    ]
    assert _patches() == []


def test_nim_edit_enforces_the_files_scope(stub, repo):
    (repo / "docs").mkdir()
    (repo / "docs" / "x.md").write_text("TARGET\n")
    _answers(stub, _GOOD_EDIT, _GOOD_EDIT)
    out = _tool(server.nim_edit)("set TARGET", str(repo), files=["docs"])
    assert out.split("\n") == [
        "[nim_edit: REFUSED after repair round]",
        "- block 1: outside scope",
    ]
    assert "docs" in stub.posts()[0][3]["messages"][1]["content"]
    assert _patches() == []


def _cover_repo(repo):
    (repo / "src" / "lib.py").write_text("def old_name():\n    return 1\n")
    (repo / "src" / "use.py").write_text(
        "def go():\n    return old_name() + 1\n\n\n"
        "def stay():\n    return old_name() + 2\n"
    )


def _cover_dekko():
    callers = {
        "results": [
            {"path": "src/use.py", "id": "src/use.py::go", "sites": [2]},
            {"path": "src/use.py", "id": "src/use.py::stay", "sites": [6]},
        ]
    }
    symbol = {"path": "src/lib.py", "line": 1}
    return [
        (True, ""),  # the coverage check's fresh map build
        (True, json.dumps(callers)),
        (True, json.dumps(symbol)),
    ]


def test_nim_edit_must_cover_flags_an_unedited_site(stub, repo, fake_dekko):
    _cover_repo(repo)
    fake_dekko.queue += [(True, ""), *_cover_dekko(), *_cover_dekko()]
    answer = _edit_answer(
        ("src/lib.py", "def old_name():", "def new_name():"),
        ("src/use.py", "old_name() + 1", "new_name() + 1"),
    )
    _answers(stub, answer, answer)
    out = _tool(server.nim_edit)(
        "rename old_name to new_name", str(repo), must_cover=["old_name"]
    )
    assert out.split("\n") == [
        "[nim_edit: REFUSED after repair round]",
        "- call site src/use.py:6 of old_name not edited",
    ]
    assert "old_name" in stub.posts()[0][3]["messages"][1]["content"]
    assert _patches() == []


def test_nim_edit_must_cover_flags_an_unedited_definition(
    stub, repo, fake_dekko
):
    _cover_repo(repo)
    fake_dekko.queue += [(True, ""), *_cover_dekko(), *_cover_dekko()]
    answer = _edit_answer(
        ("src/use.py", "old_name() + 1", "new_name() + 1"),
        ("src/use.py", "old_name() + 2", "new_name() + 2"),
    )
    _answers(stub, answer, answer)
    out = _tool(server.nim_edit)(
        "rename old_name to new_name", str(repo), must_cover=["old_name"]
    )
    assert out.split("\n") == [
        "[nim_edit: REFUSED after repair round]",
        "- definition src/lib.py:1 of old_name not edited",
    ]


def test_nim_edit_must_cover_passes_when_every_site_is_edited(
    stub, repo, fake_dekko
):
    _cover_repo(repo)
    fake_dekko.queue += [(True, ""), *_cover_dekko()]
    answer = _edit_answer(
        ("src/lib.py", "def old_name():", "def new_name():"),
        ("src/use.py", "old_name() + 1", "new_name() + 1"),
        ("src/use.py", "old_name() + 2", "new_name() + 2"),
    )
    _answers(stub, answer)
    out = _tool(server.nim_edit)(
        "rename old_name to new_name", str(repo), must_cover=["old_name"]
    )
    assert out.split("\n")[0] == (
        "[nim_edit: OK, 2 files, 3 blocks, repair round: no]"
    )


def _coverage_errors(repo, fake_dekko, blocks, sites, definition):
    """Apply `blocks` in memory and check old_name's coverage."""
    callers = {
        "results": [
            {"path": path, "id": f"{path}::go", "sites": [line]}
            for path, line in sites
        ]
    }
    symbol = {"path": definition[0], "line": definition[1]}
    fake_dekko.queue += [
        (True, ""),
        (True, json.dumps(callers)),
        (True, json.dumps(symbol)),
    ]
    changes, errors = server._edit_apply_in_memory(blocks, repo, None)
    assert errors == []
    return server._edit_check_coverage(changes, repo, ["old_name"])[0]


_LIB = ("src/lib.py", "def old_name():", "def new_name():")


def test_coverage_refuses_a_wide_block_that_keeps_the_call(repo, fake_dekko):
    (repo / "src" / "lib.py").write_text("def old_name():\n    return 1\n")
    (repo / "src" / "w.py").write_text(
        "def go():\n    x = 1\n    return old_name()\n"
    )
    wide = _blk(
        2,
        "src/w.py",
        "    x = 1\n    return old_name()",
        "    x = 2\n    return old_name()",
    )
    errors = _coverage_errors(
        repo,
        fake_dekko,
        [_blk(1, *_LIB), wide],
        [("src/w.py", 3)],
        ("src/lib.py", 1),
    )
    assert errors == ["call site src/w.py:3 of old_name not edited"]


def test_coverage_counts_an_argument_added_inside_a_call(repo, fake_dekko):
    (repo / "src" / "lib.py").write_text("def old_name():\n    return 1\n")
    (repo / "src" / "w.py").write_text(
        "def go():\n    return old_name(\n        1,\n    )\n"
    )
    added = _blk(
        2, "src/w.py", "        1,\n    )", "        1,\n        2,\n    )"
    )
    errors = _coverage_errors(
        repo,
        fake_dekko,
        [_blk(1, *_LIB), added],
        [("src/w.py", 2)],
        ("src/lib.py", 1),
    )
    assert errors == []


def test_coverage_counts_a_single_line_rename(repo, fake_dekko):
    (repo / "src" / "lib.py").write_text("def old_name():\n    return 1\n")
    (repo / "src" / "w.py").write_text("def go():\n    return old_name()\n")
    rename = _blk(2, "src/w.py", "old_name()", "new_name()")
    errors = _coverage_errors(
        repo,
        fake_dekko,
        [_blk(1, *_LIB), rename],
        [("src/w.py", 2)],
        ("src/lib.py", 1),
    )
    assert errors == []


def test_coverage_counts_a_later_line_of_a_signature(repo, fake_dekko):
    (repo / "src" / "lib.py").write_text(
        "def old_name(\n    a,\n    b,\n):\n    return a\n"
    )
    (repo / "src" / "w.py").write_text(
        "def go():\n    return old_name(1, 2)\n"
    )
    signature = _blk(1, "src/lib.py", "    b,\n):", "    b: int,\n):")
    call = _blk(2, "src/w.py", "old_name(1, 2)", "old_name(1, b=2)")
    errors = _coverage_errors(
        repo,
        fake_dekko,
        [signature, call],
        [("src/w.py", 2)],
        ("src/lib.py", 1),
    )
    assert errors == []


def test_statement_end_balances_brackets_outside_strings():
    lines = [
        "x = f(",
        '    ")",',
        "    [1, 2],",
        ")",
        "y = 1",
    ]
    assert server._edit_statement_end(lines, 1) == 4
    assert server._edit_statement_end(lines, 5) == 5
    assert server._edit_statement_end(["f("] * 40, 1) == 20


def test_nim_edit_must_cover_dekko_failure_is_an_error(
    stub, repo, fake_dekko
):
    _cover_repo(repo)
    boom = (False, "[dekko error: boom]")
    fake_dekko.queue += [(True, ""), (True, ""), boom, (True, ""), boom]
    answer = _edit_answer(("src/lib.py", "def old_name():", "def new():"))
    _answers(stub, answer, answer)
    out = _tool(server.nim_edit)("rename", str(repo), must_cover=["old_name"])
    assert out.split("\n") == [
        "[nim_edit: REFUSED after repair round]",
        "- must_cover: dekko could not list callers of old_name",
    ]


def test_nim_edit_must_cover_needs_dekko(stub, repo):
    out = _tool(server.nim_edit)("rename", str(repo), must_cover=["x"])
    assert out == (
        "[Error: must_cover needs the dekko map: dekko is switched off]"
    )
    assert not stub.posts()


def test_nim_edit_must_cover_needs_a_map_that_builds(stub, repo, fake_dekko):
    fake_dekko.queue += [(False, "[dekko error: exit 2: no parser]")]
    out = _tool(server.nim_edit)("rename", str(repo), must_cover=["x"])
    assert out == (
        "[Error: must_cover needs the dekko map: the dekko map build "
        "failed: exit 2: no parser]"
    )
    assert not stub.posts()


def test_nim_edit_rejects_a_flag_like_must_cover_symbol(
    stub, repo, fake_dekko
):
    out = _tool(server.nim_edit)("rename", str(repo), must_cover=["-x"])
    assert out == "[Error: invalid must_cover symbol: -x]"
    assert not stub.posts() and not fake_dekko.calls


def test_nim_edit_rejects_bad_input(stub, repo, tmp_path):
    edit = _tool(server.nim_edit)
    assert edit("  ", str(repo)) == "[Error: task is empty.]"
    assert edit("t", str(tmp_path / "nope")).startswith("[Error: cwd ")
    assert edit("t", str(repo), files=["-x"]) == (
        "[Error: invalid files entry: -x]"
    )
    assert edit("t", str(repo), must_cover=[""]) == (
        "[Error: invalid must_cover symbol: ]"
    )
    assert not stub.posts()


def test_nim_edit_refuses_an_oversized_patch(stub, repo, monkeypatch):
    _git_init(repo)
    monkeypatch.setattr(server, "NIM_REVIEW_MAX_DIFF_CHARS", 10)
    _answers(stub, _GOOD_EDIT)
    out = _tool(server.nim_edit)("set TARGET to 2", str(repo))
    assert out.split("\n") == [
        "[nim_edit: REFUSED]",
        "- patch too large for a mechanical edit (1 files, +1 -1 lines)",
    ]
    assert _patches() == []


def test_nim_edit_refuses_a_failed_git_check(stub, repo, monkeypatch):
    _git_init(repo)
    monkeypatch.setattr(
        server,
        "_edit_make_patch",
        lambda changes, root: ("diff", False, "error: patch failed"),
    )
    _answers(stub, _GOOD_EDIT)
    out = _tool(server.nim_edit)("set TARGET to 2", str(repo))
    assert out.split("\n") == [
        "[nim_edit: REFUSED]",
        "- git apply --check failed: error: patch failed",
    ]
    assert _patches() == []


def test_nim_edit_reports_a_failed_nim_run(stub, repo):
    stub.queue.append((401, {}, {"error": "bad key"}))
    out = _tool(server.nim_edit)("set TARGET to 2", str(repo))
    assert out.startswith("[") and not out.startswith("[nim_edit: OK")
    assert _patches() == []


def test_nim_edit_writes_a_trace(stub, tmp_path, repo):
    _git_init(repo)
    _answers(stub, _GOOD_EDIT)
    _tool(server.nim_edit)("trace this edit", str(repo))
    folder = tmp_path / "cache" / "claude-castor" / server.NIM_TRACES_DIR
    (path,) = folder.glob("*.json")
    trace = json.loads(path.read_text())
    assert trace["task_head"] == "trace this edit"
    assert trace["outcome"] == "patch"
    assert trace["patch"] == str(_patches()[0])
    assert (trace["blocks"], trace["files"], trace["repair"]) == (1, 1, False)


def test_nim_edit_start_and_poll(stub, repo):
    _git_init(repo)
    _answers(stub, _GOOD_EDIT)
    started = _tool(server.nim_edit_start)("set TARGET to 2", str(repo))
    job_id = _job_id(started)
    for _ in range(200):
        out = _tool(server.gemini_poll)(job_id)
        if "still running" not in out:
            break
        time.sleep(0.05)
    assert out.split("\n")[0] == (
        "[nim_edit: OK, 1 files, 1 blocks, repair round: no]"
    )


def test_nim_edit_start_reports_bad_input_at_once(stub, repo):
    start = _tool(server.nim_edit_start)
    assert start("rename", str(repo), must_cover=["a b"]) == (
        "[Error: invalid must_cover symbol: a b]"
    )
    assert not stub.posts()


_FULL_COVER = _edit_answer(
    ("src/lib.py", "def old_name():", "def new_name():"),
    ("src/use.py", "old_name() + 1", "new_name() + 1"),
    ("src/use.py", "old_name() + 2", "new_name() + 2"),
)


def test_nim_edit_coverage_refreshes_a_warm_map(stub, repo, fake_dekko):
    _cover_repo(repo)
    fake_dekko.queue += [(True, ""), *_cover_dekko()]
    assert server._dekko_ensure_map(repo) is True  # an earlier agent run
    _answers(stub, _FULL_COVER)
    out = _tool(server.nim_edit)("rename", str(repo), must_cover=["old_name"])
    assert out.split("\n")[0] == (
        "[nim_edit: OK, 2 files, 3 blocks, repair round: no]"
    )
    assert [c[:2] for c in fake_dekko.calls] == [
        ["map", "--if-stale"],
        ["map", "--if-stale"],
        ["query", "callers"],
        ["query", "symbol"],
    ]


def test_nim_edit_failed_map_refresh_refuses(stub, repo, fake_dekko):
    _cover_repo(repo)
    crash = (False, "[dekko error: crash]")
    fake_dekko.queue += [(True, ""), crash, crash]
    assert server._dekko_ensure_map(repo) is True
    _answers(stub, _FULL_COVER, _FULL_COVER)
    out = _tool(server.nim_edit)("rename", str(repo), must_cover=["old_name"])
    assert out.split("\n") == [
        "[nim_edit: REFUSED after repair round]",
        "- must_cover: dekko could not refresh the map (the dekko map "
        "build failed: crash)",
    ]
    assert _patches() == []


def test_nim_edit_must_cover_flags_a_grep_only_reference(
    stub, repo, fake_dekko
):
    _cover_repo(repo)
    _git_init(repo)
    assert "src/reg.py" not in server._agent_index(repo)  # warm, pre-file
    (repo / "src" / "reg.py").write_text("HANDLERS = [\n    old_name,\n]\n")
    (repo / ".dekko").mkdir()
    (repo / ".dekko" / "MAP.md").write_text("old_name\n")
    fake_dekko.queue += [(True, ""), *_cover_dekko(), *_cover_dekko()]
    _answers(stub, _FULL_COVER, _FULL_COVER)
    out = _tool(server.nim_edit)("rename", str(repo), must_cover=["old_name"])
    assert out.split("\n") == [
        "[nim_edit: REFUSED after repair round]",
        "- reference src/reg.py:2 of old_name not edited",
    ]


def test_callers_tool_lists_grep_only_references(repo, fake_dekko):
    _cover_repo(repo)
    (repo / "src" / "reg.py").write_text("HANDLERS = [old_name]\n")
    fake_dekko.queue += _cover_dekko()[1:]
    text = server._dekko_tool(repo, "callers", {"symbol": "old_name"})
    assert "src/use.py:2 in go" in text
    assert "grep-only" in text and "src/reg.py:1:HANDLERS = [old_name]" in text
    assert "src/lib.py:1:" not in text and "src/use.py:6:" not in text


def test_nim_edit_start_rejects_must_cover_without_dekko(stub, repo):
    start = _tool(server.nim_edit_start)
    assert start("rename", str(repo), must_cover=["x"]) == (
        "[Error: must_cover needs the dekko map: dekko is switched off]"
    )
    assert not stub.posts()


def test_nim_edit_start_says_dekko_is_missing(stub, repo, monkeypatch):
    monkeypatch.setenv("CASTOR_NIM_DEKKO", "1")
    monkeypatch.setattr(server, "_dekko_bin", lambda: None)
    out = _tool(server.nim_edit_start)("rename", str(repo), must_cover=["x"])
    assert out == (
        "[Error: must_cover needs the dekko map: dekko is not installed "
        "beside the server's Python]"
    )


def test_nim_edit_start_names_an_old_dekko(
    stub, repo, fake_dekko, monkeypatch
):
    monkeypatch.setattr(server, "_dekko_version", lambda: (1, 9, 0))
    out = _tool(server.nim_edit_start)("rename", str(repo), must_cover=["x"])
    assert out == (
        "[Error: must_cover needs the dekko map: dekko 1.9.0 is below 1.9.35]"
    )


def test_nim_edit_refuses_parse_errors_beside_a_valid_block(stub, repo):
    broken = _GOOD_EDIT + "\n<<<EDIT src/a.py\n<<<OLD\nthree\n>>>END"
    _answers(stub, broken, broken)
    out = _tool(server.nim_edit)("set TARGET to 2", str(repo))
    assert out.split("\n") == [
        "[nim_edit: REFUSED after repair round]",
        "- block 2: missing >>>NEW",
    ]
    assert _patches() == []


def test_nim_edit_failed_repair_run_refuses(stub, repo):
    _answers(stub, _BAD_EDIT)
    stub.queue.append((401, {}, {"error": "bad key"}))
    out = _tool(server.nim_edit)("set TARGET to 2", str(repo))
    lines = out.split("\n")
    assert lines[:2] == [
        "[nim_edit: REFUSED after repair round]",
        "- block 1: old text not found in src/a.py",
    ]
    assert len(lines) == 3 and lines[2].startswith("- repair run failed: [")
    assert _patches() == []


def test_nim_edit_start_refusal_polls_as_a_failed_job(stub, repo):
    _answers(stub, _BAD_EDIT, _BAD_EDIT)
    job_id = _job_id(_tool(server.nim_edit_start)("t", str(repo)))
    for _ in range(200):
        out = _tool(server.gemini_poll)(job_id)
        if "still running" not in out:
            break
        time.sleep(0.05)
    head, rest = out.split("\n", 1)
    assert head.startswith(f"[Job {job_id} failed after ")
    assert rest.startswith("[nim_edit: REFUSED after repair round]")


def test_nim_edit_early_returns_carry_all_stats(stub, repo):
    keys = {"blocks": 0, "files": 0, "repair": False, "outcome": "refused"}
    for args, kwargs in (
        (("  ", str(repo)), {}),
        (("t", str(repo)), {"must_cover": ["x"]}),
    ):
        _, stats = server._run_nim_edit(*args, **kwargs)
        assert keys.items() <= stats.items() and stats["patch"] is None


def test_nim_edit_patch_path_is_absolute_with_a_relative_cache(
    stub, repo, tmp_path, monkeypatch
):
    _git_init(repo)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", "relcache")
    _answers(stub, _GOOD_EDIT)
    out = _tool(server.nim_edit)("set TARGET to 2", str(repo))
    path = Path(out.split("\n")[1].removeprefix("patch: "))
    assert path.is_absolute() and path.is_file()
    assert path.is_relative_to(tmp_path.resolve() / "relcache")


def _notes_repo(repo):
    _cover_repo(repo)
    (repo / "notes").mkdir()
    (repo / "notes" / "plan.md").write_text(
        "# Plan\n    We renamed old_name last week.  \n"
    )


def test_nim_edit_lists_references_outside_files(stub, repo, fake_dekko):
    _notes_repo(repo)
    fake_dekko.queue += [(True, ""), *_cover_dekko()]
    _answers(stub, _FULL_COVER)
    out, stats = server._run_nim_edit(
        "rename", str(repo), files=["src"], must_cover=["old_name"]
    )
    lines = out.split("\n")
    assert lines[0] == "[nim_edit: OK, 2 files, 3 blocks, repair round: no]"
    assert lines[4:6] == [
        "unedited references outside files:",
        "- notes/plan.md:2: We renamed old_name last week.",
    ]
    assert lines[6].startswith("diff --git ")
    (patch,) = _patches()
    assert patch.read_text() == "\n".join(lines[6:])
    assert stats["unedited_references"] == 1
    assert stats["outcome"] == "patch"


def test_nim_edit_refuses_an_unedited_reference_without_files(
    stub, repo, fake_dekko
):
    _notes_repo(repo)
    fake_dekko.queue += [(True, ""), *_cover_dekko(), *_cover_dekko()]
    _answers(stub, _FULL_COVER, _FULL_COVER)
    out = _tool(server.nim_edit)("rename", str(repo), must_cover=["old_name"])
    assert out.split("\n") == [
        "[nim_edit: REFUSED after repair round]",
        "- reference notes/plan.md:2 of old_name not edited",
    ]


def test_nim_edit_refuses_a_call_site_outside_files(stub, repo, fake_dekko):
    _cover_repo(repo)
    fake_dekko.queue += [(True, ""), *_cover_dekko(), *_cover_dekko()]
    answer = _edit_answer(("src/lib.py", "def old_name():", "def new():"))
    _answers(stub, answer, answer)
    out = _tool(server.nim_edit)(
        "rename", str(repo), files=["src/lib.py"], must_cover=["old_name"]
    )
    assert out.split("\n") == [
        "[nim_edit: REFUSED after repair round]",
        "- call site src/use.py:2 of old_name not edited",
        "- call site src/use.py:6 of old_name not edited",
    ]
    assert _patches() == []


def test_nim_edit_reports_zero_unedited_references_by_default(stub, repo):
    _git_init(repo)
    _answers(stub, _GOOD_EDIT)
    out, stats = server._run_nim_edit("set TARGET to 2", str(repo))
    assert "unedited references" not in out
    assert stats["unedited_references"] == 0
