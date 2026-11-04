# Claude Castor

An Antigravity MCP for Claude Code. Offloads large-context work — file exploration,
indexing, summarization, deep research — to Google's free Antigravity CLI (`agy`),
the terminal agent powered by Gemini models. No API key required. Uses Google
sign-in (free individual tier).

> Antigravity CLI replaces the retired Gemini CLI (shut down June 18, 2026). The
> MCP tools keep their `gemini_*` names for compatibility but drive `agy`.

---

## Setup

### 1. Install the Antigravity CLI

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash
```

Verify with `agy --version`.

### 2. Sign in

Run `/castor:auth` (or call the `gemini_auth` tool) for the sign-in command, then
run it yourself — sign-in is interactive, so type it in the Claude Code prompt or
a terminal:

```bash
agy -p "ok"
```

Complete the Google consent in your browser. One-time step — the token persists in
the system keyring.

### 3. Install Python dependencies

dekko (the code map behind the NIM agent's call-graph tools and
`nim_edit`'s `must_cover`) is installed with the server's dependencies:
`uv` installs it from `pyproject.toml`. With pip, install it yourself:

```bash
pip install fastmcp "dekko[all]>=1.9.35"
# or, if using uv:
uv add fastmcp
```

### 4. Register with Claude Code

Run this once to register the MCP server (replace the path with the actual absolute path):

```bash
claude mcp add -s user claude-castor /absolute/path/to/uv -- run --directory /absolute/path/to/claude-castor server.py
```

To find your `uv` path: `which uv`

### 5. Verify

Restart Claude Code, then ask Claude to run `gemini_status`. It should return `READY`.

---

## Tools

### `gemini_prompt`

Sends a prompt to Antigravity (`agy`) and returns the response. Claude constructs
the prompt — see `GEMINI.md` for the prompting guide.

| Parameter | Type | Description |
|---|---|---|
| `prompt` | `str` | The fully-formed prompt (task context + ask) |
| `files` | `list[str] \| None` | Absolute file paths to inline (user-provided only) |
| `directory` | `str \| None` | Directory to inline (user-provided only) |
| `raw` | `bool` | Skip structured response instructions (default: false) |
| `trust` | `bool` | Full agent mode — `--dangerously-skip-permissions`; requires `cwd` (default: false) |
| `cwd` | `str \| None` | Project root for agy's workspace (required when `trust=True`) |
| `add_dirs` | `list[str] \| None` | Extra dirs to grant agy read access (`--add-dir`) |
| `continue_session` | `bool` | Resume the project's default session (`__default__`); requires `cwd` (default: false) |
| `session` | `str \| None` | Name a project-scoped session to continue; Castor replays its transcript. Requires `cwd`; never cached |
| `model` | `str \| None` | Select the agy model (see `gemini_models`; defaults to agy's default) |
| `sandbox` | `bool` | Explore with auto-approved commands inside agy's OS sandbox (workspace read-only); middle tier (default: false; ignored when `trust=True`) |
| `use_cache` | `bool` | Reuse a stored response for the same prompt against an unchanged repo (default: true; side-effect-free calls only) |

### Workflow tools

Purpose-built wrappers over `gemini_prompt` that bake in the right prompt and
access tier. Each takes `cwd` (project root) and optional `model`. All are
side-effect-free, so results are cached against the repo state in a git repo.

| Tool | Signature | Tier | Purpose |
|---|---|---|---|
| `gemini_index` | `(cwd, model=None)` | sandbox | Compact repo map (layout, entry points, key symbols) |
| `gemini_review` | `(cwd, diff=None, model=None)` | read-only | Correctness review of a diff (defaults to `git diff HEAD`) |
| `gemini_security_review` | `(cwd, diff=None, model=None)` | read-only | Security-focused review of a diff (`security-engineer` agent; complements `gemini_review`) |
| `gemini_find_usages` | `(cwd, symbol, model=None)` | sandbox | Every use of a symbol, with paths/lines |
| `gemini_explain_error` | `(cwd, error, model=None)` | sandbox | Ranked root-cause hypotheses for an error |
| `gemini_summarize` | `(cwd, target, model=None)` | sandbox | Token-lean `{summary, key_points}` of a file/dir |
| `gemini_semantic_search` | `(cwd, query, model=None)` | sandbox | NL code search → ranked `[{path, line, reason}]` (≤20) |
| `gemini_document` | `(cwd, target, model=None)` | sandbox | Proposed docstrings/docs in the project's style (return-only) |

### Background jobs

`gemini_start` runs a prompt off-thread (same args as `gemini_prompt` minus the
session ones — background jobs are fresh-only) and returns a job id;
`gemini_poll(job_id)` returns `running` / the response / an error; `gemini_jobs`
lists them. Use for long explorations and fan-out. Workers match the NIM gate
(4 per key, at most 8); jobs are in-memory (lost on restart) and bounded to 50
(oldest finished evicted, never a running job). A job waits up to
`NIM_JOB_MAX_WAIT` (300 s) for a NIM slot or key, a foreground call 30 s.
Set `CASTOR_NIM_JOB_MAX_WAIT` (seconds, clamped to 60..3600, default 300) to
change the job wait; `nim_status` reports it as `job wait N s`.

### Sessions

Multi-turn context is **Castor-owned**, not agy's: the transcript is stored under
`~/.cache/claude-castor/sessions/<project>/<name>.json` and replayed into fresh
`agy --print` calls. Sessions are project-scoped (keyed by repo-root path, so they
survive commits and restarts), never cached, and evicted after 30 idle days. When
a replay exceeds ~24k chars, older turns are folded into a rolling summary (via a
cheap model), keeping the last 3 turns verbatim.

| Tool | Signature | Purpose |
|---|---|---|
| `gemini_sessions` | `(cwd)` | List a project's sessions (turn count, last-updated) |
| `gemini_session_show` | `(cwd, session)` | Print a session's stored transcript |
| `gemini_session_delete` | `(cwd, session)` | Delete a session |
| `gemini_reset` | `(cwd)` | Clear the project's default (`__default__`) session |

Drive a session from `gemini_prompt` with `session="<name>"` (or
`continue_session=True` for the default). `gemini_reset` now requires `cwd`.

### NVIDIA NIM tools (optional second backend)

A non-agentic backend: an OpenAI-compatible chat endpoint
(`https://integrate.api.nvidia.com/v1`) with no filesystem access, so Castor
inlines whatever the model needs. Needs `NVIDIA_API_KEY` in the MCP server's
environment (`claude mcp add -s user claude-castor -e NVIDIA_API_KEY=... -- ...`);
the key is env-only, never a tool arg, never logged. `NVIDIA_API_KEYS`
(comma-separated) adds more keys: requests rotate across them, each key has its
own limiter and 429 cooldown, and a 401/402 retires a key while another
remains. `_dispatch_nim` mirrors `_dispatch` (same `_assemble_prompt`, response cache, session store) and
`_run_nim` returns `(ok, text)` rather than sniffing for a leading `[`.

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
| `nim_explain_row` | `(cwd, log_path, row, occurrence=0, format="text", model=None, max_steps=None)` | Gated, off unless `CASTOR_NIM_EXPLAIN_ROW=1` (its eval gate has not run; `_register_gated`). Role: for one FAIL or NOT ASSESSED row of a verdict.sh log inside `cwd` (exact name with `occurrence`, else the one name containing `row`; none or several is an error listing at most 10; logs from before `@case` stamps are read from the verdict lines, where a name over 34 characters is found only by its exact name), Castor puts the verdict line, the trial lines (over 60, the first and last 30 with a cut marker) and the script name (the log's folder) in the task; the agent (dekko `search`, then `symbol` / `context`) answers Row, Verdict code, Precondition, Trial lines, Reading (harness, game or unclear). Default 6 steps, max 12. `format="json"` returns `row`, `verdict` (both from the row Castor found), `verdict_code` (null for "not found"), `precondition`, `trial_lines`, `reading` (null unless exactly one reading and " - "), `why`, `verified`, `quote_ok`, `agent` |
| `nim_explain_row_start` | same | Gated like `nim_explain_row`; background run, input checked at start; poll with `gemini_poll` |
| `nim_edit` | `(task, cwd, files=None, must_cover=None, model=None, max_steps=None)` | Registered only with `CASTOR_NIM_EDIT=1` (its gate has not passed; `_register_gated`). Mechanical edit as a checked patch under `~/.cache/claude-castor/nim-patches/`; NIM never writes files. The model records edits with `propose_edit`/`drop_edit`, each checked on arrival (edit blocks in the answer are the fallback). Each edit's old text must be whole lines matching exactly once inside `cwd` and `files`; `must_cover` (needs dekko) requires every call site and the definition edited, and other mentions inside `files` (ones outside are listed, not refused); one repair round, then a refusal or `git apply --check`. Read the diff, then `git apply <patch>` |
| `nim_edit_start` | same | Background `nim_edit`; input is checked at start; poll with `gemini_poll` |
| `nim_models` | `(query=None)` | List served model ids |
| `nim_usage` | `(days=7)` | Per-day and per-model requests, failures, tokens and agent runs from local records |
| `nim_status` | `(verify=True)` | Key/model/limits/cooldown per key; `verify` spends one request per key |

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
a one-line `nvidia_nim:` summary (no network call).

### `gemini_cache_clear`

Deletes all cached Antigravity responses. `gemini_prompt` reuses a stored
response when the same prompt is re-sent against an unchanged repo (matched by
git HEAD + working-tree state). Clear it to force fresh runs.

### `gemini_models`

Lists the models available to agy — the names you can pass as `model` to
`gemini_prompt`. Requires sign-in.

### `gemini_auth`

Returns instructions for the user to sign in (run `! agy -p "ok"` in the Claude
Code prompt). An MCP tool call blocks Claude, so it cannot drive the interactive
browser flow itself. Call when `gemini_status` reports NOT SIGNED IN.

### `gemini_status`

Checks CLI installation and sign-in. Returns `READY` (with the available
models appended, best-effort) or instructions to fix.

### `gemini_setup`

Returns step-by-step setup instructions. Claude calls this when setup is needed.

---

## How It Works

1. Claude recognizes an opportunity to offload work to Antigravity
2. Claude reads `GEMINI.md` to construct a context-rich prompt
3. Claude calls `gemini_prompt` with the assembled prompt
4. The MCP server prepends a system instruction and inlines any files
5. The prompt is passed inline (argv) to `agy --print` as a subprocess
6. agy's response comes back as the tool result
7. Claude uses the response as research context to continue the task

**Agent mode** (`trust=True`): agy runs with `--dangerously-skip-permissions`,
rooted at `cwd`, giving it full filesystem access to actively explore the project
on its own — no pre-loading required. Best for deep research and large codebases.

---

## Troubleshooting

**`agy` not found**
Run `curl -fsSL https://antigravity.google/cli/install.sh | bash`, then verify with
`agy --version`.

**Not signed in**
Run `/castor:auth` (or call the `gemini_auth` tool) for the sign-in command, then
run `agy -p "ok"` to complete Google sign-in.

**Timeout**
agy print mode has a 300s timeout per call. For large directories in agent mode
(`trust=True`), agy explores on its own — prefer that over pre-loading the directory.

**"Antigravity produced no output: it tried to run a shell command…"**
agy 1.3+ auto-denies shell commands it can't prompt for in headless mode, which
ends the run with no output. Retry with `sandbox=True` (read commands allowed,
workspace read-only) or `trust=True` (full access), with `cwd` set to the
project root.

**Garbled or empty response**
Try `raw=True` to see unfiltered output, which can help diagnose prompt issues.

---

## Development

- Before committing, run:
  ```bash
  uvx ruff check server.py
  uvx ruff format --check server.py
  python3 -m py_compile server.py
  uv run --with pytest --with httpx --with fastmcp pytest tests
  ```
  `tests/test_nim.py` exercises the NIM backend against a local stub server (no
  network, no real key). The agy path has no automated tests.
- Before changing a NIM role prompt, the agent loop or a default model, score
  it: `uv run --with httpx --with fastmcp python scripts/nim_eval.py
  scripts/eval/cases.json [private cases] --config label=model[,step=model]`
  (needs `NVIDIA_API_KEYS`; spends free-tier requests). It reports lead
  precision, recall of known findings, must-catch hits, time and steps.
  `--rescore results.json` re-scores saved answers after editing cases. A
  match needs the right file and lines and one of the finding's keywords;
  line proximity alone overstated hits. Cases for private repos stay outside
  this repo (the NQC set is `~/.cache/claude-castor/eval/nqc-cases.json`).
- Before a release, run `python3 scripts/check_agy_surface.py` to catch drift in
  the agy flags and subcommands `server.py` depends on. After an intended agy
  upgrade, update `server.py`, then refresh with `--update-snapshot`.
- Before a release and after a dekko bump, run `uv run python
  scripts/check_dekko_surface.py` to catch drift in the dekko flags and JSON
  fields Castor relies on; `--update-snapshot` refreshes it.
- The version string lives only in `pyproject.toml`. Work happens on `develop`.

---

## Phase 2

See `.dev/development-plan.md` for the full roadmap. Key items:

- Persistent chat sessions with `gemini_reset` tool (agy `--continue`) — **done**
- Model selection (`model` param + `gemini_models` tool) — **done**
- Sandbox tier (`--sandbox`) between read-only and full trust — **done**
- Directory structure map when content is truncated — **done**
- Enhanced `gemini_status` diagnostics (available models) — **done**
  (account/freshness skipped: agy has no whoami command and `agy update`
  mutates rather than checks)
- `/castor:auth` slash command for onboarding — **done**
- Response caching (`use_cache` param + `gemini_cache_clear` tool; keyed by
  prompt + model + git HEAD/working-tree state) — **done**
- Workflow tools (`gemini_index`, `gemini_review`, `gemini_find_usages`,
  `gemini_explain_error`; thin wrappers over the shared `_dispatch` core) — **done**
- Async / background jobs (`gemini_start` / `gemini_poll` / `gemini_jobs`;
  thread-pool workers, poll model, fresh-only) — **done**
- More workflow tools (`gemini_summarize`, `gemini_semantic_search`,
  `gemini_document`; thin wrappers over `_dispatch`) — **done**
- Client-side sessions (`session` param + `gemini_sessions` / `_show` /
  `_delete`; Castor-owned transcripts replayed into fresh `agy --print`,
  auto-summarized over budget — replaces the agy `--continue` path) — **done**
- NVIDIA NIM backend (`nim_*` tools; inline-only, env-configured key, retry/
  rate-limit/cooldown handling) — **done**
- Multi-provider routing: more OpenAI-compatible free-tier providers behind the
  same client, ordered fallback chains with per-candidate cooldowns, and
  context-aware candidate skipping (NIM is the first provider)
- Streaming output (token-by-token, vs. the current poll model)
