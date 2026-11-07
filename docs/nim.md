# NVIDIA NIM backend

An optional second backend next to agy. See [agy.md](agy.md) for the `gemini_*` tools.

Castor can also offload to [NVIDIA NIM](https://build.nvidia.com)'s hosted
models, which have a free tier for developers. It is a second target next to
agy, useful for an independent second opinion, for spreading load when agy's
free quota is tight, or for fanning one question out to both and comparing.

**It is not an agent.** agy explores the workspace on its own; NIM is a plain
chat endpoint with no filesystem access. Castor reads whatever the model needs
(`files`, `directory`, a diff) and inlines it into one request. So NIM suits
review, summarize and explain over content you supply; for open-ended
exploration of a codebase, stay on `gemini_prompt` with `cwd`.

**Privacy:** everything inlined is sent to NVIDIA's hosted API, under NVIDIA's
free-tier data terms. Don't send secrets or code you haven't cleared for a
third party.

## Setup

1. Create a free API key at <https://build.nvidia.com/settings/api-keys>.
2. Register it with the MCP server's environment (the key is read from the
   environment only, it is never a tool argument and is never logged):

   ```bash
   claude mcp add -s user claude-castor -e NVIDIA_API_KEY=nvapi-... \
     -- /absolute/path/to/uv run --directory /absolute/path/to/claude-castor server.py
   ```

   (If already registered, `claude mcp remove claude-castor -s user` first.
   `install.py` does not forward your key, so re-run this command to add it.
   Claude Code stores the value in `~/.claude.json`.)
3. Restart Claude Code and run `/castor:nim-status` (or the `nim_status`
   tool). It sends one tiny completion to prove the key and model work.

## Tools

| Tool | Signature | Purpose |
|---|---|---|
| `nim_prompt` | `(prompt, files, directory, raw, cwd, continue_session, session, model, use_cache, temperature, max_tokens)` | General prompt; files/directory inlined |
| `nim_start` | same minus session args | Background job on the shared pool; poll with `gemini_poll` |
| `nim_review` / `nim_security_review` | `(cwd, diff=None, model=None)` | Diff review (defaults to `git diff HEAD`) |
| `nim_explain_error` | `(error, files=None, model=None)` | Root causes; source passed via `files` |
| `nim_summarize` | `(cwd, target, model=None)` | `{summary, key_points}` of an inlined file/dir under `cwd` |
| `nim_agent` | `(task, cwd, model=None, max_steps=None, max_tokens=None)` | Read-only tool loop: NIM explores `cwd` with grep/read_range/list_dir, plus dekko's callers/symbol/outline/context/search/trace when dekko is on, run by Castor; cited `path:line` are verified |
| `nim_agent_start` | same | Background `nim_agent`; poll with `gemini_poll` |
| `nim_scout` | `(question, cwd, model=None, max_steps=None)` | Role: the exact `path:start-end` ranges a task needs |
| `nim_first_pass_review` | `(cwd, diff_file=None, rev_range=None, focus=None, model=None, max_steps=None)` | Role: ranked diff review; the diff is read server-side (file, range or `git diff HEAD`), refused over ~1,500 lines |
| `nim_triage_log` | `(cwd, log_path, question=None, model=None, max_steps=None, format="text")` | Role: verdict plus `log:LINE` failures for a log inside `cwd`; `format="json"` returns Castor's parse (verdict, totals, FAIL rows, failures, markers, agent stats) |
| `nim_scout_start` / `nim_first_pass_review_start` / `nim_triage_log_start` | same as the role | Background role; input is checked at start; poll with `gemini_poll`; the triage one takes `format` too |
| `nim_explain_row` | `(cwd, log_path, row, occurrence=0, format="text", model=None, max_steps=None)` | Location only since 2026-11-04: the free models could not read harness vs game (first gate: reading right 4/24; a measurement on 21 cases: Super 7/42, Ultra 9/42), so the caller reads the cited code. Registered by default after its site gate passed (21 NQC cases, 2 runs on Super: 18 and 19 sites within 3 lines, 0 errors; bar 17). Role: for one FAIL or NOT ASSESSED row of a verdict.sh log inside `cwd` (exact name with `occurrence`, else the one name containing `row`; none or several is an error listing at most 10; logs from before `@case` stamps are read from the verdict lines, where a name over 34 characters is found only by its exact name), Castor puts the verdict line, the trial lines (over 60, the first and last 30 with a cut marker) and the script name (the log's folder) in the task; the agent (the prompt says dekko `search`, then `symbol` / `context`; in the first gate it called a dekko tool in 1 of 24 runs, grep and read_range did the work) answers Row, Verdict code, Precondition, Trial lines. Default 6 steps, max 12. `format="json"` returns `row`, `verdict` (both from the row Castor found), `verdict_code` (null for "not found"), `precondition`, `trial_lines`, `verified`, `quote_ok`, `agent` |
| `nim_explain_row_start` | same | Background `nim_explain_row`, input checked at start; poll with `gemini_poll` |
| `nim_edit` | `(task, cwd, files=None, must_cover=None, model=None, max_steps=None)` | Registered only with `CASTOR_NIM_EDIT=1` (its gate has not passed; `_register_gated`). Mechanical edit as a checked patch under `~/.cache/claude-castor/nim-patches/`; NIM never writes files. The model records edits with `propose_edit`/`drop_edit`, each checked on arrival (edit blocks in the answer are the fallback). Each edit's old text must be whole lines matching exactly once inside `cwd` and `files`; `must_cover` (needs dekko) requires every call site and the definition edited, and other mentions inside `files` (ones outside are listed, not refused); one repair round, then a refusal or `git apply --check`. Read the diff, then `git apply <patch>` |
| `nim_edit_start` | same | Background `nim_edit`; input is checked at start; poll with `gemini_poll` |
| `nim_models` | `(query=None)` | List served model ids |
| `nim_usage` | `(days=7)` | Per-day and per-model requests, failures, tokens and agent runs from local records |
| `nim_status` | `(verify=True)` | Key/model/limits/cooldown per key; `verify` spends one request per key |

Caching, sessions and background jobs work as they do for agy. Sessions are
shared across backends, so a session name continues whichever tool you call
next, and compaction of long NIM sessions uses NIM (agy isn't needed).

## Configuration

All optional, set as environment variables on the MCP server:

| Variable | Default | Purpose |
|---|---|---|
| `NVIDIA_API_KEY` (or `NVIDIA_NIM_API_KEY`) | none | API key (required unless `NVIDIA_API_KEYS` is set) |
| `NVIDIA_API_KEYS` | none | Several keys, comma-separated. Requests rotate across them; each key has its own rate limit and cooldown, a 429 moves on to the next key, and a rejected key (401/402) is skipped while another remains |
| `CASTOR_NIM_MODEL` | `nvidia/nemotron-3.5-super-vl-120b-a12b` | Model used when a call names none; see `nim_models` |
| `CASTOR_NIM_AGENT_MODEL` | `nvidia/nemotron-3.5-super-vl-120b-a12b` (or `CASTOR_NIM_MODEL` when that is set) | Model for `nim_agent` and the roles; scored as accurate as Nemotron 3 Ultra there and about 2.5 times faster |
| `CASTOR_NIM_AGENT_STEP_MODEL` | the agent model | Model for the agent's tool steps (picking the next lookup); the agent model still writes the answer |
| `CASTOR_NIM_AGENT_ANSWER_TOKENS` | `16384` | Token cap for the agent's final answer, which reasons over everything it read |
| `CASTOR_NIM_TRACES` | `1` | `0` turns off the local usage log and agent traces under `~/.cache/claude-castor/` (kept 30 days) |
| `CASTOR_NIM_BASE_URL` | `https://integrate.api.nvidia.com/v1` | Endpoint (https only; http allowed for localhost, e.g. a self-hosted NIM) |
| `CASTOR_NIM_RPM` | `40` | Client-side requests per minute, per key |
| `CASTOR_NIM_CONCURRENCY` | `4` per key, at most `12` | Simultaneous in-flight requests; background jobs wait up to 300 s for a slot, foreground calls 30 s |
| `CASTOR_NIM_MAX_INPUT_CHARS` | `300000` | Prompt size budget (~75k tokens); larger prompts are rejected up front |
| `CASTOR_NIM_MAX_TOKENS` | `4096` | Completion cap per request |
| `CASTOR_NIM_TIMEOUT` | `60` | Seconds with no new streamed data before Castor gives up on a reply (a stalled answer keeps its partial text); with streaming off, seconds for the whole reply (default `180`) |
| `CASTOR_NIM_STREAM` | `1` | `0` turns off streaming, for an OpenAI-compatible endpoint without server-sent events |

NVIDIA sets the real free-tier limits and they change; the defaults above are
conservative guesses, so tune them to what your account shows.

## Resilience

Retryable failures (429, 5xx, network errors, one timeout) are retried with
exponential backoff and jitter, honoring `Retry-After`. A 429 also sets a shared
cooldown so concurrent background jobs back off together, and if the wait
would exceed 30 seconds Castor returns a "try again in Ns" error rather than
blocking Claude. Auth, billing, permission, not-found and context-window errors
are never retried and come back with a specific fix. Replies that never
arrive or come back empty return a clear bracketed error, and `<think>` blocks
from reasoning models are stripped.

## Internals

For people changing `server.py`.

A non-agentic backend: an OpenAI-compatible chat endpoint
(`https://integrate.api.nvidia.com/v1`) with no filesystem access, so Castor
inlines whatever the model needs. Needs `NVIDIA_API_KEY` in the MCP server's
environment (`claude mcp add -s user claude-castor -e NVIDIA_API_KEY=... -- ...`);
the key is env-only, never a tool arg, never logged. `NVIDIA_API_KEYS`
(comma-separated) adds more keys: requests rotate across them, each key has its
own limiter and 429 cooldown, and a 401/402 retires a key while another
remains. `_dispatch_nim` mirrors `_dispatch` (same `_assemble_prompt`, response cache, session store) and
`_run_nim` returns `(ok, text)` rather than sniffing for a leading `[`.

`nim_agent` (`_nim_agent_loop`) is confined to `cwd` (symlinks resolved before
the containment check; `.env*`, key files, `.git` and `.dekko` are off limits),
reads at most 120 lines per call, caps each tool result, and never lets the
model edit or run anything. Each step is one NIM request (default 12 steps, max
30; reviews 8). With dekko >= 1.9.35 installed the loop also offers six dekko
tools (`callers`, `symbol`, `outline`, `context`, `search`, `trace`; on by
default, `CASTOR_NIM_DEKKO=0` turns them off). A call to a tool the run did
not offer gets the unknown-tool reply. To offer them, any agent role builds
dekko's map in `.dekko/` under `cwd` (`dekko map --if-stale`, at most once a
minute per root; a failed build is not retried for 30 minutes). It shows in
`git status` as an untracked directory; Castor never edits the repo's
`.gitignore`. `nim_edit` reuses the loop with two more tools and returns
patches Castor checks (`_run_nim_edit`): the model records each edit with
`propose_edit(path, old, new)` and takes one back with `drop_edit(edit)`.
Each proposal is checked on arrival against the file and the edits already
recorded (`_EditStore`), and a refusal comes back with its repair hint.
Recorded edits are the answer: no answer request is made for them, and the
repair round keeps and lists them. Edit calls have their own cap
(`NIM_EDIT_CALLS_PER_STEP`, 16 per step). With nothing recorded, edit blocks
in the answer are parsed as before (the fallback). Agent roles default to
`NIM_AGENT_DEFAULT_MODEL` (Nemotron 3.5 Super); `CASTOR_NIM_AGENT_MODEL`
overrides it, and a set `CASTOR_NIM_MODEL` is followed.
`CASTOR_NIM_AGENT_STEP_MODEL` can run the tool steps on another model while
the agent model answers (scored no better, so unset by default).
`_agent_verify_citations` tags any `path:line` that does not exist as
`[unverified]` (a bare file name is checked against the one project file with
that name), and `_agent_check_quotes` checks each cited line's backticked
quote, tagging `[quote is at line N]` or `[quote not in file]`.
`nim_first_pass_review` gets `_review_context`: numbered new-file lines
around each hunk plus grep hits for the functions the diff touches.
`_run_nim_agent` returns stats and writes a trace to
`~/.cache/claude-castor/nim-traces/`; `_nim_request` logs every chat request
to `nim-usage/` (no prompt text) for `nim_usage`; `CASTOR_NIM_TRACES=0` turns
both off. The last step, or a
tool step that runs out of tokens, goes to `_agent_answer`: the transcript
flattened into one user message (Nemotron keeps calling tools otherwise), with
a 16k token cap (`CASTOR_NIM_AGENT_ANSWER_TOKENS`) and a 360 s timeout
(`NIM_AGENT_ANSWER_TIMEOUT`; other requests keep `CASTOR_NIM_TIMEOUT`). Chat
requests stream (`_nim_send_stream` / `_NimStream` rebuild the plain response
shape; `CASTOR_NIM_STREAM=0` turns it off), so these are idle gaps between
chunks (default 60 s), with a 900 s wall cap; a stalled answer returns its
partial text with a note, a stalled tool step fails as a timeout. Nemotron models get a
`reasoning_budget` of 1024 per tool step and 6144 for the answer
(`_nim_thinking_cap`); plain NIM calls (`_run_nim`) cap it at half of
`max_tokens`, at most 6144. Walks and the file list skip engine assets
(`NIM_AGENT_SKIP_EXTENSIONS`: Unity `.meta`, `.prefab`, `.fbx` and the like),
and `list_dir` hides `.meta` files; a path named directly is still readable.
The system prompt carries a file list from `git ls-files` (with line
counts; directories past 150 files), grep takes `context` lines and reports
match totals past its cap, and the model may batch up to 4 calls per step.

Resilience lives in `_nim_request`: a client-side sliding-window limiter
(`CASTOR_NIM_RPM`, default 40/60s, tracked per key) and a concurrency gate
(`_nim_gate_size`: 4 per key, at most 12, or `CASTOR_NIM_CONCURRENCY`), retry
with exponential backoff + jitter on 429/5xx/network (one timeout retry),
`Retry-After` honored, a per-key cooldown after a 429 (other keys keep going), and a fail-fast "try again in Ns" error once the
wait would exceed `NIM_MAX_WAIT` (30s; `NIM_JOB_MAX_WAIT`, 300 s, on a job
worker). Requests share one `httpx.Client` (`_nim_client`). `_classify_nim_status` decides what is
retryable. Other knobs: `CASTOR_NIM_MODEL`, `CASTOR_NIM_BASE_URL` (https only;
http for localhost), `CASTOR_NIM_MAX_INPUT_CHARS`, `CASTOR_NIM_MAX_TOKENS`,
`CASTOR_NIM_CONCURRENCY`, `CASTOR_NIM_TIMEOUT`, `CASTOR_NIM_JOB_MAX_WAIT`
(background-job wait for a slot, 60..3600 s, default 300). NIM session compaction uses NIM
(`_summarize_text_nim`), so NIM-only users never need agy. `gemini_status` adds
