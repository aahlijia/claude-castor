# Claude Castor

> An Antigravity MCP for Claude Code.

Offloads large-context work — file exploration, indexing, summarization, deep
research — to Google's free [Antigravity CLI](https://antigravity.google)
(`agy`), the terminal coding agent powered by Gemini models. Claude
orchestrates and reasons; Antigravity handles the heavy lifting.

No API key required. Uses Google sign-in (free individual tier).

> **Note:** Antigravity CLI replaces the retired Gemini CLI (shut down
> June 18, 2026). The MCP tools keep their `gemini_*` names for
> compatibility, but they now drive the `agy` binary.

---

## How It Works

1. Claude recognizes a task that would benefit from a large context window
2. Claude constructs a context-rich prompt describing the task and what it needs
3. Claude calls `gemini_prompt` via the MCP bridge
4. The server passes the prompt inline to the `agy --print` CLI as a subprocess
5. agy's response comes back as a tool result Claude uses to continue the task

In **agent mode** (`trust=True`), agy auto-approves tool actions
(`--dangerously-skip-permissions`) and, rooted at `cwd`, actively explores the
project on its own — navigating files, grepping for patterns, following
imports — rather than working from pre-loaded context.

---

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- [Claude Code](https://claude.ai/code)

---

## Installation

### 1. Install the Antigravity CLI

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash
```

Verify with `agy --version`.

### 2. Sign in with Google

Run `/castor:auth` (or call the `gemini_auth` tool) for guided sign-in. It hands
you the command to run — sign-in is interactive, so you run it yourself in the
Claude Code prompt or a terminal:

```bash
agy -p "ok"
```

Complete the Google consent in your browser. One-time step — the token is
stored in the system keyring.

### 3. Clone the repo

```bash
git clone https://github.com/yourusername/claude-castor
cd claude-castor
```

### 4. Run the installer

```bash
python install.py
```

This registers the MCP server with Claude Code and installs the slash commands
globally. Works on macOS, Linux, and Windows.

### 5. Restart Claude Code

Then run `/castor:status` to confirm everything is wired up.

---

## Claude Desktop App

To use Claude Castor in the Claude desktop app, edit the config file at
`~/Library/Application Support/Claude/claude_desktop_config.json` and add an
entry under `mcpServers`:

```json
{
  "mcpServers": {
    "claude-castor": {
      "command": "/absolute/path/to/uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/claude-castor",
        "server.py"
      ]
    }
  }
}
```

Get your `uv` path with `which uv`. Restart the desktop app after saving.

If you already signed in to Antigravity for Claude Code, authentication carries
over via the system keyring — no extra steps needed.

---

## Slash Commands

| Command | Description |
|---|---|
| `/castor:status` | Check Antigravity CLI installation and sign-in |
| `/castor:agents` | List agy's built-in specialized agents |
| `/castor:auth` | Sign in to the Antigravity CLI |
| `/castor:explore` | Full codebase exploration in agent mode |
| `/castor:research <topic>` | Deep-dive on a symbol, feature, or file |
| `/castor:index` | Compact, reusable map of the current codebase |
| `/castor:review` | Free second-opinion review of your uncommitted changes |
| `/castor:security-review` | Free security-focused review of your uncommitted changes |
| `/castor:usages <symbol>` | Trace where and how a symbol is used |
| `/castor:explain <error>` | Diagnose an error or stack trace against the codebase |
| `/castor:summarize <path>` | Token-lean summary of a large file or directory |
| `/castor:search <query>` | Find code by natural-language intent |
| `/castor:document <target>` | Draft docstrings/docs in the project's style |
| `/castor:session [list\|show\|delete]` | Inspect and manage conversation sessions |

---

## MCP Tools

| Tool | Description |
|---|---|
| `gemini_prompt` | Send a prompt to Antigravity and get a response |
| `gemini_index` | Produce a structured map of a codebase for use as context; schema-enforced JSON |
| `gemini_review` | Free second-opinion review of a code diff; runs agy's `code-reviewer` agent by default |
| `gemini_security_review` | Free security-focused review of a code diff; runs agy's `security-engineer` agent by default |
| `gemini_find_usages` | Find where and how a symbol is used across a codebase |
| `gemini_explain_error` | Explain an error or stack trace against the codebase; runs agy's `root-cause-analyst` agent by default |
| `gemini_summarize` | Token-lean `{summary, key_points}` of a file or directory |
| `gemini_semantic_search` | Natural-language code search → ranked `[{path, line, reason}]` |
| `gemini_document` | Draft docstrings/docs for a symbol or file (return-only) |
| `gemini_start` | Start a prompt in the background; returns a job id |
| `gemini_poll` | Check a background job; returns its result when done |
| `gemini_jobs` | List background jobs and their status |
| `gemini_sessions` | List a project's conversation sessions |
| `gemini_session_show` | Print a session's stored transcript |
| `gemini_session_delete` | Delete a session |
| `gemini_reset` | Clear the project's default session (requires `cwd`) |
| `gemini_cache_clear` | Delete all cached responses to force fresh runs |
| `gemini_models` | List the models available to agy |
| `gemini_agents` | List agy's built-in specialized agents |
| `gemini_auth` | Get sign-in instructions when not authenticated |
| `gemini_status` | Check CLI installation and sign-in; lists available models and agents when READY |
| `gemini_setup` | Get step-by-step setup instructions |

### `gemini_prompt` parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `prompt` | `str` | required | The prompt Claude constructs |
| `files` | `list[str]` | `None` | File paths to inline (user-provided only) |
| `directory` | `str` | `None` | Directory to inline (user-provided only) |
| `raw` | `bool` | `false` | Skip structured response instructions |
| `trust` | `bool` | `false` | Enable full agent mode (requires `cwd`) |
| `cwd` | `str` | `None` | Project root for agy's workspace |
| `add_dirs` | `list[str]` | `None` | Extra dirs to grant agy read access (`--add-dir`) |
| `continue_session` | `bool` | `false` | Resume the project's default session (requires `cwd`) |
| `session` | `str` | `None` | Name a project-scoped session to continue (requires `cwd`; never cached) |
| `model` | `str` | `None` | Select the agy model (see `gemini_models`) |
| `agent` | `str` | `None` | Route the prompt to one of agy's built-in agents (see `gemini_agents`) |
| `sandbox` | `bool` | `false` | Explore under terminal restrictions (safe middle tier; ignored if `trust`) |
| `use_cache` | `bool` | `true` | Reuse a stored response for the same prompt against an unchanged repo |

---

## Agent Mode

By default, agy runs read-only and responds in Q&A mode. With `trust=True`
(which requires `cwd`), agy runs with `--dangerously-skip-permissions` — it can
read files, search the codebase, follow imports, and auto-approve tool actions
on its own without needing content pre-loaded.

`sandbox=True` is a safer middle tier: agy still explores the filesystem
actively, but under terminal restrictions and without blindly auto-approving
actions. `trust` takes precedence if both are set. Prefer `sandbox` unless the
task genuinely needs agy to write files or run arbitrary commands.

Best for (trust or sandbox):
- Deep codebase exploration
- Cross-file symbol resolution
- Large projects where pre-loading is impractical

agy is rooted at the `cwd` you pass; grant access to additional directories
with `add_dirs` (mapped to repeated `--add-dir` flags).

---

## Sessions

By default each `gemini_prompt` call is independent. For multi-step work on the
same codebase, pass a `session` name (e.g. `session="refactor-auth"`) — or
`continue_session=True` for the project's default session. Sessions are
**Castor-owned**: the transcript is stored under
`~/.cache/claude-castor/sessions/<project>/<name>.json` and replayed as context
into a fresh `agy --print` call on each turn (agy's own `--continue` is not used).

That makes sessions project-scoped (keyed by repo-root path, so they survive
commits and server restarts) and free of cross-project contamination. They require
`cwd`, are never cached, and are evicted after 30 idle days. When a replay grows
past ~24k characters, older turns are folded into a rolling summary (using a cheap
model), keeping the last 3 turns verbatim.

Inspect them with `gemini_sessions(cwd)` / `gemini_session_show(cwd, name)`,
delete one with `gemini_session_delete(cwd, name)`, or clear the default with
`gemini_reset(cwd)`.

---

## Model Selection

Leave `model` unset to use agy's default. Pass `model="…"` to pick a specific
model — a faster one for light summarization, a stronger one for deep reasoning.
Call `gemini_models` to list what's available.

---

## Workflow Tools

Beyond the general `gemini_prompt`, several purpose-built tools bake in the right
prompt and access tier for common shapes — so Claude doesn't have to assemble
them by hand. Each takes a `cwd` (the project root) and an optional `model`.

| Tool | What it does | Tier |
|---|---|---|
| `gemini_index(cwd)` | Compact repo map: layout, entry points, key symbols, how they connect | sandbox, schema-enforced JSON |
| `gemini_review(cwd, diff=None)` | Correctness-focused review of a diff (defaults to `git diff HEAD`) | read-only, agent `code-reviewer` |
| `gemini_security_review(cwd, diff=None)` | Security-focused review of a diff (defaults to `git diff HEAD`) | read-only, agent `security-engineer` |
| `gemini_find_usages(cwd, symbol)` | Every use of a symbol, with paths and line refs | sandbox |
| `gemini_explain_error(cwd, error)` | Ranked root-cause hypotheses for an error/stack trace | sandbox, agent `root-cause-analyst` |
| `gemini_summarize(cwd, target)` | Token-lean `{summary, key_points}` of a file or directory | sandbox, schema-enforced JSON |
| `gemini_semantic_search(cwd, query)` | NL code search → ranked `[{path, line, reason}]` (≤20) | sandbox, schema-enforced JSON |
| `gemini_document(cwd, target)` | Proposed docstrings/docs in the project's style (return-only) | sandbox |

All are side-effect-free, so their results are cached against the repo state when
`cwd` is a git repo (see below). `gemini_index` is the canonical "give me context
I can reuse" call — run it once, then build on it. `gemini_review` complements
(does not replace) Claude's own `/code-review`; `gemini_security_review`
complements `gemini_review` the same way — correctness vs. security are separate
passes, run both for full coverage. `gemini_semantic_search` finds code by
intent, where `gemini_find_usages` needs an exact symbol name. The four
agent-defaulting tools (`gemini_index`, `gemini_review`,
`gemini_security_review`, `gemini_explain_error`) each accept an `agent`
override, or `agent=None` to fall back to agy's own default agent (see
`gemini_agents`). "Schema-enforced JSON" means the shape is a contract agy
validates via `--json-schema`, not just a prompt request — informational only,
it doesn't change how these tools are called or what they return.

---

## Background Jobs

`gemini_prompt` blocks until agy responds (up to 300s). For long explorations —
or fan-out, where you start several jobs across subtrees and synthesize the
results — run them in the background instead:

1. `gemini_start(prompt, …)` spawns the agy run off-thread and returns a job id.
2. Do other work.
3. `gemini_poll(job_id)` returns `running` (with elapsed time), the response when
   `done`, or a message on error. `gemini_jobs()` lists everything outstanding.

`gemini_start` takes the same arguments as `gemini_prompt` except the session
ones: background jobs are **fresh-only** by construction (concurrent jobs would
race on the shared session), so for stateful multi-turn work use the synchronous
`gemini_prompt`. Concurrency is capped (4 workers), and a cache hit completes a
job immediately. Jobs live in memory only — a server restart forgets them.

---

## Caching

Identical prompts against an unchanged codebase return a stored response instead
of re-running agy — instant, and free. The cache key combines the full prompt,
the `model`, the `sandbox` flag, and (when `cwd` is a git repo) the repo state:
`git HEAD` plus a hash of `git status --porcelain`, so any commit or working-tree
change busts the cache.

Caching is **side-effect-free only**:

- `trust=True` calls are never cached (they may write files or run commands).
- Session calls (`session` / `continue_session`) are never cached — they are
  stateful, and their own transcripts live under `sessions/` (kept separate from
  the response cache, so `gemini_cache_clear` does not touch them).
- `sandbox=True` explore calls are cached only inside a git repo, where the repo
  fingerprint can prove the code is unchanged.
- Bracketed error/status responses are never stored.

Entries live under `~/.cache/claude-castor/` (or `$XDG_CACHE_HOME`) and expire
after 7 days. Pass `use_cache=False` to force a fresh run, or call
`gemini_cache_clear` to wipe the cache.

---

## Troubleshooting

**`agy` not found** — Run
`curl -fsSL https://antigravity.google/cli/install.sh | bash`

**Not signed in** — Run `/castor:auth` (or call the `gemini_auth` tool) for the
sign-in command, then run `agy -p "ok"` to complete Google sign-in

**Timeout** — agy print mode has a 300s limit. For large projects, prefer
agent mode (`trust=True`) over pre-loading a full directory

**Empty response** — Try `raw=True` to see unfiltered output

---

## Maintainer Notes

Before releasing, run `python3 scripts/check_agy_surface.py` to check that
agy's CLI (the flags/subcommands `server.py` depends on) hasn't drifted since
the last release. It diffs a live `agy --help` against a checked-in snapshot
and exits non-zero on drift; pass `--update-snapshot` to refresh the snapshot
after a deliberate agy upgrade.

---

## Roadmap

Recently shipped: model selection (`gemini_models`), a sandbox tier,
skipped-file listing on truncation, richer `gemini_status`, response caching
(`gemini_cache_clear`), workflow tools (`gemini_index`, `gemini_review`,
`gemini_find_usages`, `gemini_explain_error`, `gemini_summarize`,
`gemini_semantic_search`, `gemini_document`), async background jobs
(`gemini_start` / `gemini_poll` / `gemini_jobs`), and Castor-owned client-side
sessions (`session` param + `gemini_sessions` / `_show` / `_delete`) that replace
the agy `--continue` path.

Still planned:

- Streaming output (token-by-token, vs. the current poll model)
