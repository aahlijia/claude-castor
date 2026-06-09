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

```bash
pip install fastmcp
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
| `sandbox` | `bool` | Explore under terminal restrictions — safe middle tier (default: false; ignored when `trust=True`) |
| `use_cache` | `bool` | Reuse a stored response for the same prompt against an unchanged repo (default: true; side-effect-free calls only) |

### Workflow tools

Purpose-built wrappers over `gemini_prompt` that bake in the right prompt and
access tier. Each takes `cwd` (project root) and optional `model`. All are
side-effect-free, so results are cached against the repo state in a git repo.

| Tool | Signature | Tier | Purpose |
|---|---|---|---|
| `gemini_index` | `(cwd, model=None)` | sandbox | Compact repo map (layout, entry points, key symbols) |
| `gemini_review` | `(cwd, diff=None, model=None)` | read-only | Correctness review of a diff (defaults to `git diff HEAD`) |
| `gemini_find_usages` | `(cwd, symbol, model=None)` | sandbox | Every use of a symbol, with paths/lines |
| `gemini_explain_error` | `(cwd, error, model=None)` | sandbox | Ranked root-cause hypotheses for an error |
| `gemini_summarize` | `(cwd, target, model=None)` | sandbox | Token-lean `{summary, key_points}` of a file/dir |
| `gemini_semantic_search` | `(cwd, query, model=None)` | sandbox | NL code search → ranked `[{path, line, reason}]` (≤20) |
| `gemini_document` | `(cwd, target, model=None)` | sandbox | Proposed docstrings/docs in the project's style (return-only) |

### Background jobs

`gemini_start` runs a prompt off-thread (same args as `gemini_prompt` minus the
session ones — background jobs are fresh-only) and returns a job id;
`gemini_poll(job_id)` returns `running` / the response / an error; `gemini_jobs`
lists them. Use for long explorations and fan-out. Concurrency is capped at 4
workers; jobs are in-memory (lost on restart) and bounded to 50 (oldest finished
evicted, never a running job).

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
5. The prompt is piped via stdin to `agy --print` as a subprocess
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

**Garbled or empty response**
Try `raw=True` to see unfiltered output, which can help diagnose prompt issues.

---

## Phase 2

See `.docs/development-plan.md` for the full roadmap. Key items:

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
- Streaming output (token-by-token, vs. the current poll model)
