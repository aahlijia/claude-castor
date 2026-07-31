# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - 2026-07-31

### Added

- **`gemini_agents()`** — lists agy's built-in specialized agents (calls
  `agy agents`), mirroring `gemini_models()`. Live-verified against a
  signed-in agy 1.1.9: returns a flat, task-named roster
  (`backend-architect`, `security-engineer`, `code-reviewer`,
  `root-cause-analyst`, etc.). New `/castor:agents` skill; `GEMINI.md`
  gains an "Agent Selection" section (discovery only — no tool routes to a
  specific agent yet).
- **`effort` parameter** on `gemini_prompt`/`gemini_start` (`low` / `medium`
  / `high`) — selects agy's reasoning effort independent of model choice,
  threaded through `_build_agy_cmd`/`_run_agy`/`_dispatch` the same way
  `model` already is, including the cache key (so two different `effort`
  values on the same prompt no longer collide on one cache entry).
  `GEMINI.md` gains an "Effort Selection" section.

### Fixed

- **`_cheapest_model()` returned the *most* expensive flash-tier model**
  (`gemini-3.6-flash-high`), not the cheapest. Its needle-matching scanned
  family names before effort suffixes and matched the first line of `agy
  models`' output unconditionally, silently sending every session-transcript
  compaction call to the priciest flash tier since the feature shipped.
  `_cheapest_model()` itself is fixed (prefers an explicit `-low` suffix,
  matches only the model-id token, not the display-name column) and kept as
  a fallback for future callers; the actual compaction call
  (`_summarize_text`) now uses `effort="low"` against agy's default model
  instead of relying on the heuristic at all.
- **`gemini_index` duplicated its full raw response into `raw_markdown`
  even on successful parses** — every successful call was paying roughly
  double the token cost of the single most context-hungry tool in the
  surface. `raw_markdown` is now present only in the fallback envelope on
  parse failure, matching `gemini_summarize`/`gemini_semantic_search`.

### Security

- **`--disable-slash-commands` is now always passed to agy.** Castor's
  prompts routinely inline arbitrary file/directory content; without this
  flag, inlined text starting with `/` at the start of a line could be
  misinterpreted by agy as one of its own skill invocations rather than
  inert content handed over for analysis.

## [0.4.1] - 2026-07-31

### Fixed

- **`agy --print` regression (agy 1.1.9): prompts never reached the CLI.**
  `--print` now requires its value inline in argv — there is no stdin
  fallback. Because `_build_agy_cmd` built `["agy", "--print",
  "--print-timeout", PRINT_TIMEOUT]` and `_run_agy` piped the real prompt via
  `subprocess.run(..., input=prompt)`, agy's parser swallowed the literal
  string `"--print-timeout"` as the entire prompt and narrated trying to
  explain that flag to itself instead of doing the work. Fixed: the prompt is
  now placed inline immediately after `--print`, with
  `--print-timeout={PRINT_TIMEOUT}` passed as a single `=`-joined token so it
  can't be mistaken for the prompt value. `gemini_status`'s own ad-hoc
  auth-check subprocess had the identical bug and is fixed the same way.
- **Workspace never bound in sandbox/trust calls.** agy's workspace comes
  from `--add-dir`, not the subprocess's OS-level `cwd` — every sandbox/trust
  call was silently exploring agy's own default scratch directory instead of
  the project. `cwd` is now always also passed to agy as its own `--add-dir`
  entry.
- **Valid JSON responses misreported as parse failures.** agy sometimes
  prefixes a narration line before its JSON output even when told to return
  bare JSON, so `_parse_index` / `_parse_summary` / `_parse_hits` running
  `json.loads()` on the raw response would fall into the fallback envelope.
  New `_extract_json_blob()` helper strips fences, then — if the result isn't
  already clean JSON — slices from the first `{`/`[` to the last matching
  close bracket; wired into all three parsers in place of bare
  `_strip_fences`.

## [0.4.0] - 2026-06-08

### Added

- **Three new workflow tools** — `gemini_summarize(cwd, target)` returns a
  token-lean `{summary, key_points}` of a file or directory;
  `gemini_semantic_search(cwd, query)` finds code by natural-language intent and
  returns ranked `[{path, line, reason}]` hits (≤20, distinct from the
  symbol-exact `gemini_find_usages`); `gemini_document(cwd, target)` drafts
  docstrings/docs in the project's inferred style (return-only — it never writes
  files). All are sandbox-tier, side-effect-free, and cached against the repo
  state. New `/castor:summarize`, `/castor:search`, and `/castor:document` skills.
- **Client-side sessions** — multi-turn context is now **Castor-owned**. The
  transcript is stored under `~/.cache/claude-castor/sessions/<project>/<name>.json`
  and replayed into a fresh `agy --print` call each turn. Pass `session="<name>"`
  to `gemini_prompt` (or `continue_session=True` for the default session). New
  tools `gemini_sessions`, `gemini_session_show`, and `gemini_session_delete`, plus
  a `/castor:session` skill. Sessions are project-scoped (keyed by repo-root path,
  so they survive commits and restarts), never cached, and evicted after 30 idle
  days. Replays past ~24k chars auto-compress older turns into a rolling summary
  (via a cheap model), keeping the last 3 turns verbatim.

### Changed

- **`gemini_prompt`** gains a `session` param and **drops `conversation_id`**.
  `continue_session=True` now maps to the project's default session
  (`__default__`) on the new transcript machinery, so it works across server
  restarts for the first time. A session requires `cwd`.
- **`gemini_reset`** now takes `cwd` and clears that project's default session
  (it previously cleared a global in-memory flag).

### Removed

- The agy-`--continue` / `--conversation` session path and the in-memory
  `_session_active` flag — superseded by Castor-owned transcript replay. This
  also retires the previously unverified reliance on agy emitting conversation
  state.

## [0.3.0] - 2026-06-08

### Added (continued)

- **`gemini_status` now accepts an optional `cwd` argument** — when provided,
  the response includes the last-index timestamp and git SHA for that project
  (read from the project state store populated by `gemini_index`). The response
  also always includes a `cache_size_mb` line showing the total size of all
  files under `~/.cache/claude-castor/` (response cache + project store). The
  `/castor:status` skill now passes `cwd` automatically.

### Improved

- **Castor skills now load the MCP tool schema automatically** — the manual
  `ToolSearch` pre-load step is no longer required. Each `/castor:*` skill
  opens with the exact `ToolSearch("select:…")` call for the tool it drives,
  so the first invocation succeeds in a cold session without any preamble.

### Fixed

- **Auth error messages are now consistent across all call sites.** Every
  not-signed-in response now names both the `gemini_auth` tool and the
  `! agy -p "ok"` command via a shared `AUTH_HINT` constant, replacing three
  near-duplicate strings that disagreed in wording.
- `gemini_poll` on an unknown or evicted job ID now suggests running
  `gemini_jobs` to list current jobs.

### Added

- **`/castor:index` now persists the index to project memory** — after a
  successful structured `gemini_index` response, the skill writes a compact
  `castor-index.md` to the project's Claude memory directory
  (`~/.claude/projects/<slug>/memory/`) and adds a pointer to `MEMORY.md`.
  On subsequent sessions, the skill reads the memory file and compares its
  `git_sha` against the current HEAD — if they match, the tool call is
  skipped entirely. Only a new commit invalidates the memory index (HEAD-only
  comparison, not the full working-tree fingerprint, so uncommitted edits
  during development do not cause unnecessary re-indexing). Parse-failure
  fallback responses are never written to memory.

- **`gemini_index` records `last_index` in the project state store** —
  timestamp and git SHA of the last successful index run are written to
  `~/.cache/claude-castor/projects/<hash>.json` for consumption by FR-4
  (`gemini_status` with `cwd`).

- **`gemini_index` now returns structured JSON** — the response includes
  `summary`, `entry_points`, `modules` (name → `{file, line, role}`),
  `exception_types`, `architecture`, `raw_markdown`, and `git_sha`.
  Consumers can route on individual fields (e.g. auto-populate
  `gemini_find_usages` targets from `modules`) while `raw_markdown`
  preserves the full agy response. Falls back to a minimal
  `{git_sha, raw_markdown}` envelope when the response cannot be parsed
  as JSON. Implemented by `_parse_index` (calls `_strip_fences` +
  `_validate_index_keys` + `_get_git_sha`); `INDEX_PROMPT` updated to
  request JSON output; `gemini_index` now dispatches with `raw=True` to
  avoid `SYSTEM_INSTRUCTION` interfering with JSON-only output.

- **Project-state store helpers** (`_project_key`, `_project_state_path`,
  `_load_project_state`, `_save_project_state`) under
  `~/.cache/claude-castor/projects/`. Foundation for FR-6 structured index
  output, FR-2 memory persistence, and FR-4 richer status — consumed by later
  Round-3 features. Writes are atomic (`os.replace`) to avoid corruption from
  concurrent background jobs.

---

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
