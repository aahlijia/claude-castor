# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
 
## [0.2.0] - 2026-06-08

### Changed

- **Migrated from the retired Gemini CLI to the Antigravity CLI (`agy`).**
  Google shut down the free Gemini CLI on 2026-06-18; Antigravity is its
  successor (still powered by Gemini models, free individual tier, Google
  sign-in). The MCP tools keep their `gemini_*` names for compatibility.
  - `_run_gemini` → `_run_agy`: invokes `agy --print` (prompt piped via stdin,
    so prompt size is no longer bound by the argument-length limit),
    `--print-timeout 300s`. Agent mode now passes
    `--dangerously-skip-permissions` instead of setting
    `GEMINI_CLI_TRUST_WORKSPACE`; `add_dirs` maps to repeated `--add-dir`.
  - `SYSTEM_INSTRUCTION` now suppresses agy's print-mode noise: action
    narration ("I will read…"), the trailing "Summary of Work" section, and
    `file://` links.
  - `gemini_prompt` gains an `add_dirs` parameter for granting agy read access
    to extra directories without inlining them.
  - `gemini_status` checks `agy --version` and distinguishes NOT INSTALLED,
    NOT SIGNED IN, and READY. On READY it now also lists the available models
    (best-effort via `agy models`; omitted silently if unavailable).
  - Install/auth docs updated: `curl … install.sh | bash` instead of npm;
    `agy -p "ok"` / the new `gemini_auth` tool instead of bare `gemini` OAuth.
- `_needs_auth` hardened — case-insensitive match against several sign-in
  markers instead of one literal string, so a wording change in agy's output
  no longer silently breaks auth detection across the server.
- `_inline_directory` now lists the skipped file paths (capped, with an
  "…and N more" overflow) instead of only a count, so Claude can request
  specific dropped files in a follow-up.
- `_run_agy` argv assembly extracted into a `_build_agy_cmd` helper.
- **Slash commands renamed to the `castor:` namespace** (`/castor:status`,
  `/castor:auth`, `/castor:explore`, `/castor:research`). They now install under
  `~/.claude/commands/castor/`; the installer removes the old flat
  `castor-*.md` files from prior installs.

### Added

- `gemini_auth` tool — returns the sign-in instruction (`! agy -p "ok"`) for the
  user to run in the Claude Code prompt. An MCP tool call blocks Claude while it
  runs, so it cannot drive agy's interactive browser flow itself; the user
  completes the one-time Google consent (token persists in the system keyring).
- Persistent sessions — `gemini_prompt` gains `continue_session` (resume agy's
  prior conversation via `--continue`) and `conversation_id` (resume a specific
  conversation via `--conversation <id>`), plus a `gemini_reset` tool to drop the
  session so the next call starts fresh.
- Model selection — `gemini_prompt` gains a `model` parameter (`--model`), and a
  new `gemini_models` tool lists the models available to agy.
- Sandbox tier — `gemini_prompt` gains a `sandbox` parameter (`--sandbox`): agy
  explores under terminal restrictions without auto-approving actions. A safe
  middle ground between read-only and `trust`; `trust` takes precedence if both
  are set.
- `/castor:auth` slash command — guides first-time sign-in.
- Response caching — `gemini_prompt` gains a `use_cache` parameter (default
  `true`): an identical prompt sent against an unchanged repo returns a stored
  response instead of re-running agy. The cache key combines the assembled
  prompt, `model`, `sandbox`, and — when `cwd` is a git repo — the repo state
  (`git HEAD` + a hash of `git status --porcelain`, so any change busts it).
  Only side-effect-free calls are cached: never `trust=True`, never session
  continuations, never sandbox calls outside a git repo, never bracketed
  error/status responses. Entries live under `~/.cache/claude-castor/`
  (`$XDG_CACHE_HOME`) with a 7-day TTL. New `gemini_cache_clear` tool wipes them;
  pass `use_cache=False` to force a single fresh run.
- Workflow tools — four purpose-built wrappers over `gemini_prompt` that bake in
  the right prompt and access tier, so Claude needn't assemble them by hand. Each
  takes `cwd` and an optional `model`, is side-effect-free, and inherits caching:
  - `gemini_index(cwd)` — a compact, Claude-consumable repo map (layout, entry
    points, key modules/symbols, how they connect). Sandbox tier; the canonical
    cacheable "give me context" call.
  - `gemini_review(cwd, diff=None)` — a correctness-focused second-opinion review
    of a diff (defaults to `git diff HEAD`). Read-only; complements `/code-review`.
  - `gemini_find_usages(cwd, symbol)` — every use of a symbol, with paths and
    line references. Sandbox tier.
  - `gemini_explain_error(cwd, error)` — ranked root-cause hypotheses for an error
    or stack trace, with the files to check. Sandbox tier.
  - The `gemini_prompt` run/cache/session core was extracted into a shared
    `_dispatch` helper that all of these (and `gemini_prompt`) route through, so
    caching and session bookkeeping behave identically everywhere.
- Slash commands for the workflow tools: `/castor:index`, `/castor:review`,
  `/castor:usages <symbol>`, and `/castor:explain <error>`. Auto-installed by the
  existing `commands/castor/*.md` glob; `/castor:explore` is unchanged (deep
  trust-mode exploration remains distinct from the compact `gemini_index` map).
- Async background jobs — three tools for non-blocking and fan-out work:
  - `gemini_start(prompt, …)` runs an agy prompt off-thread and returns a job id
    immediately. Takes the same arguments as `gemini_prompt` except the session
    ones: background jobs are fresh-only (concurrent jobs would race on the shared
    session state), so they never accept `continue_session`/`conversation_id` and
    never mutate it.
  - `gemini_poll(job_id)` returns `running` (with elapsed seconds), the response
    when done, or a bracketed message on error/unknown id.
  - `gemini_jobs()` lists outstanding jobs with status and age.
  - Backed by a bounded `ThreadPoolExecutor` (4 workers, so a fan-out cannot spawn
    unlimited agy processes) and an in-memory job table capped at 50 (oldest
    finished jobs evicted on the next start; a running job is never evicted). Jobs
    do not survive a server restart. `_dispatch` gained a `track_session` flag so
    the background path reuses the same run/cache logic without touching sessions.

### Carried over (prior unreleased work)

- `gemini_prompt` rejects `trust=True` calls that omit `cwd`.
- File inlining reads each file once; size check precedes the read.
- `ruff check` / `ruff format --check` pass clean.

## [0.1.0] - 2026-05-28

### Added

- Initial release of **Claude Castor**, a Gemini MCP server for Claude Code.
- `gemini_prompt` tool — sends a constructed prompt to the `@google/gemini-cli`
  subprocess and returns the response. Supports inlining `files` and a
  `directory` (with `.gitignore` awareness, binary detection, and 100 KB/file
  and 800 KB-total caps), a `raw` mode, and a `trust` agent mode that grants
  Gemini filesystem access via `GEMINI_CLI_TRUST_WORKSPACE`.
- `gemini_status` tool — verifies the `gemini` CLI is installed and
  authenticated.
- `gemini_setup` tool — returns step-by-step Google OAuth setup instructions.
- `cwd` parameter on `gemini_prompt` to root Gemini's filesystem access in the
  correct project directory during agent mode.
- `install.py` installer — registers the MCP server with `claude mcp add` and
  copies the slash commands and `GEMINI.md` into `~/.claude/`.
- Slash commands: `/castor-explore`, `/castor-research`, `/castor-status`.
- `GEMINI.md` prompting guide and project documentation.

[Unreleased]: https://github.com/aahlijia/claude-castor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/aahlijia/claude-castor/releases/tag/v0.1.0
