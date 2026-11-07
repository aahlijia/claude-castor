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

`server.py` is one file, in sections (search for the banner comments):

| Section | What it holds |
|---|---|
| top to `NVIDIA NIM backend` | agy runner, prompt assembly, cache, sessions, `gemini_*` tools, background jobs |
| `NVIDIA NIM backend` | key rotation, limiter, retries, `nim_*` chat tools |
| `NIM agent` | read-only tool loop, dekko tools, citation checks |
| `Verdict rows` | suite log parsing (`LogFormat`) |
| `NIM agent roles` | scout, first-pass review, triage |
| `Explain a verdict row` | `nim_explain_row` |
| `NIM edit` | `nim_edit` (gated by `CASTOR_NIM_EDIT=1`) |
| `Run tier` | `.castor/run.json`, `castor_run_start`, `gemini_poll` on runs |
| `Compare runs` | `castor_compare_runs` |

Tests: `tests/test_nim.py` (NIM against a local stub server), `tests/test_run.py`
(run tier, compare, `log_format`), `tests/test_nim_eval.py` (the eval script).
The agy path has no automated tests.

## Development

Before committing:

```bash
uvx ruff check server.py
uvx ruff format --check server.py
python3 -m py_compile server.py
uv run --with pytest --with httpx --with fastmcp pytest tests
```

The version string lives only in `pyproject.toml`. Work happens on `develop`.
Evals, release surface checks and the roadmap are in `docs/development.md`.
