# Claude Castor

An MCP server for Claude Code that offloads large-context work (file
exploration, indexing, summarization, deep research, log triage) to free
backends: Google's Antigravity CLI (`agy`, Gemini models, Google sign-in, no API
key) and, optionally, NVIDIA NIM's free hosted models.

> Antigravity CLI replaces the retired Gemini CLI (shut down June 18, 2026). The
> MCP tools keep their `gemini_*` names for compatibility but drive `agy`.

## Docs

Reference lives in `docs/`; this file is the short orientation.

| File | Covers |
|---|---|
| `README.md` | Install, slash commands, tool overview |
| `GEMINI.md` | Prompting guide Claude reads; `install.py` copies it to `~/.claude/` |
| `docs/agy.md` | `gemini_*` tools, agent mode, sessions, caching, background jobs |
| `docs/nim.md` | NVIDIA NIM backend: setup, tools, config, resilience, agent internals |
| `docs/run-tier.md` | `castor_run_start`, `castor_runs`, `castor_compare_runs`, `.castor/run.json` (including `log_format`) |
| `docs/troubleshooting.md` | Common failures |
| `docs/development.md` | Checks before committing, evals, release checks, roadmap |

## Setup

Install `agy` (`curl -fsSL https://antigravity.google/cli/install.sh | bash`),
sign in with `agy -p "ok"` (or `/castor:auth`), then register the server:

```bash
claude mcp add -s user claude-castor /absolute/path/to/uv -- run --directory /absolute/path/to/claude-castor server.py
```

`python install.py` does the same and installs the slash commands. Restart
Claude Code and run `gemini_status`; it should return `READY`. dekko (the code
map behind the NIM agent's call-graph tools and `nim_edit`'s `must_cover`)
installs with the server's dependencies. Full steps are in `README.md`.

## How it works

1. Claude sees a chance to offload work and builds a prompt (see `GEMINI.md`).
2. It calls `gemini_prompt` (or a workflow tool, or a `nim_*` tool).
3. The server prepends a system instruction, inlines any files, and runs
   `agy --print` as a subprocess (NIM: one chat request, or a local tool loop).
4. The response comes back as the tool result.

`trust=True` runs agy with `--dangerously-skip-permissions` rooted at `cwd`;
`sandbox=True` is the middle tier. See `docs/agy.md`.

## Code map

`server.py` is the entry point: it imports the `castor` package, which registers
every `@mcp.tool()` on the shared `mcp` object, then runs it. The code:

| Module | What it holds |
|---|---|
| `castor/app.py` | Logging, `--log-file`, the `mcp` object, `_register_gated` |
| `castor/common.py` | Shared helpers: prompts and schemas, file inlining, git, response cache, project state, sessions, `_assemble_prompt`, agent path confinement |
| `castor/jobs.py` | Background job table and worker (`_run_job`) |
| `castor/logs.py` | Suite log parsing: `LogFormat`, verdict rows, totals |
| `castor/agy.py` | Antigravity: the `agy` runner, `_dispatch`, every `gemini_*` tool |
| `castor/nim/backend.py` | NIM chat: keys, limiter, retries, streaming, `nim_*` chat tools, usage |
| `castor/nim/edit_tools.py` | The edit store and `propose_edit` / `drop_edit` tools the agent loop offers |
| `castor/nim/agent.py` | The read-only tool loop, dekko tools, citation checks, `nim_agent` |
| `castor/nim/roles.py` | Scout, first-pass review, log triage |
| `castor/nim/explain.py` | `nim_explain_row` |
| `castor/nim/edit.py` | `nim_edit` (gated by `CASTOR_NIM_EDIT=1`) |
| `castor/run/tier.py` | `.castor/run.json`, `castor_run_start`, run polling |
| `castor/run/compare.py` | `castor_compare_runs` |
| `castor/_namespace.py` | Test and eval support only: one `server`-like view over all modules |

`common`, `logs` and `app` import no other castor module. `common` and `jobs`
each import `castor.agy` inside one function, because `agy` imports them.

Tests: `tests/test_nim.py` (NIM against a local stub server), `tests/test_run.py`
(run tier, compare, `log_format`), `tests/test_nim_eval.py` (the eval script).
They read and patch names through `castor._namespace.view()`, so a patch
reaches every module that imported the name.
The agy path has no automated tests.

## Development

Before committing:

```bash
uvx ruff check server.py castor
uvx ruff format --check server.py castor
python3 -m compileall -q server.py castor
uv run --with pytest --with httpx --with fastmcp pytest tests
```

The version string lives only in `pyproject.toml`. Work happens on `develop`.
Evals, release surface checks and the roadmap are in `docs/development.md`.
