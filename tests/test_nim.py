"""Tests for the NVIDIA NIM backend, run against a local stub server.

No network and no real API key: CASTOR_NIM_BASE_URL points at a loopback
HTTP server that replays scripted responses and records every request.
"""

import importlib
import json
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

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


class Stub:
    """Scripted OpenAI-compatible endpoint on a loopback port."""

    def __init__(self):
        self.queue = []  # (status, headers, body) consumed per POST
        self.requests = []  # (method, path, headers, parsed body)
        self.models = [
            "nvidia/nemotron-3-ultra-550b-a55b",
            "nvidia/nemotron-x",
        ]
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, headers, body):
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
    assert body["stream"] is False
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
    assert models == [server.NIM_DEFAULT_MODEL, "env/model", "arg/model"]


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
    assert "nvidia/nemotron-3-ultra-550b-a55b" in out and "(2 of 2)" in out
    out = _tool(server.nim_models)("nemotron")
    assert "nvidia/nemotron-x" in out and "llama" not in out
    assert _tool(server.nim_models)("zzz").startswith("[No NIM models match")


def test_status_ready(stub):
    out = _tool(server.nim_status)()
    assert out.startswith("READY — NVIDIA NIM")
    assert KEY not in out
    assert "default_model: nvidia/nemotron-3-ultra-550b-a55b" in out
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
