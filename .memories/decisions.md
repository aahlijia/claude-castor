# Decisions

### 2026-06-05: Keep `gemini_*` tool names after moving from Gemini CLI to agy
- **Why:** renaming breaks every registered MCP install and the slash commands.
- **Rejected:** `agy_*` / `castor_*` renames.

### 2026-06-05: Drive agy as a captured subprocess, not a PTY
- **Why:** non-TTY `agy --print` captures stdout fine; a PTY added complexity
  for nothing.

### 2026-06-05: Hybrid context: `cwd`/`add_dirs` for exploration, inline only user-named files
- **Why:** agy explores on its own when rooted; inlining whole trees hits size limits.

### 2026-06-08: Server persists to `~/.cache/claude-castor`; only skills write Claude memory
- **Why:** keeps `server.py` out of `~/.claude/`. Project-state store lives at
  `~/.cache/claude-castor/projects/<sha256(cwd)>.json`.

### 2026-07-31: Castor owns session transcripts instead of agy `--continue` / `--conversation`
- **Why:** agy gives no way to capture a print-mode conversation id, and
  `--continue` cross-contaminates projects. Transcripts are replayed into
  fresh `agy --print` calls and summarized over ~24k chars.

### 2026-07-31: `gemini_index` does not default to agy's `repo-index` agent
- **Why:** on agy 1.1.9, `repo-index` + sandbox + `--json-schema` restated its
  JSON across turns and broke both schema enforcement and the text fallback.
  `agent=None` was reliable on the same repo.

### 2026-07-31: `--mode` is not used anywhere
- **Why:** `accept-edits` hangs headless print on any shell command; `plan` is
  a no-op (silent auto-deny without trust, identical to no flag with trust).
- **Rejected:** a `mode="plan"` passthrough was built, then reverted as API
  surface with no observable capability.

### 2026-07-31: Streaming output parked
- **Why:** fastmcp 3.2.4 only has numeric `report_progress`, no incremental
  tool-output API.

### 2026-07-31: agy surface drift check lives in tracked `scripts/`, not `.dev/`
- **Why:** it must reach fresh clones to be run before releases.

### 2026-10-26: Sandbox tier = `--sandbox --dangerously-skip-permissions`
- **Why:** since agy 1.3, headless print mode auto-denies every shell command
  outside trust, and one denial ends the run with empty output and exit 0, so
  all six sandboxed workflow tools were broken. Tested on agy 1.4.7: inside
  `--sandbox`, workspace writes fail ("Operation not permitted") while
  find/grep/git work. Writes elsewhere in `~` and network access are NOT
  blocked; the user accepted that trade-off explicitly. Workflow tools keep
  `trust=False`, so they stay cacheable.
- **Rejected:** plain trust for workflow tools (the review's suggestion; agy
  could modify the project); settings.json allow-rules (did not match in
  1.4.7); leaving sandbox broken with only a clearer error.

### 2026-10-26: Kept `PRINT_TIMEOUT = 300s` despite the review's 60s-timeout claim
- **Why:** Claude Code 2.1.310's MCP tool timeout defaults to 1e8 ms, with a
  30-minute idle limit for stdio servers (`CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`).
  `gemini_index` takes ~2 minutes even on a tiny repo, so 45s would break it.
- **Rejected:** lowering to 45s; parallelizing `gemini_status` lookups (models
  and agents take ~1.2s and ~0.6s; the slow step is the live model probe).

### 2026-11-02: NIM default model is nemotron-3-ultra-550b-a55b
- **Why:** `meta/llama-3.3-70b-instruct` reached end of life 2026-08-26 and
  returns 410. Ultra answered live on the free tier, scores highest of the
  usable models on Artificial Analysis (about 47.7 vs DeepSeek V4.1 Flash 45,
  GLM and MiniMax 42; sources conflict), and handled tool calls in 2 to 3 s.
- **Rejected:** Kimi K2.6 (strongest open model but 404 for this account),
  Nemotron 3 Super (404), DeepSeek V4.1 Flash and gpt-oss-20b (timed out or ran
  100+ s per step), GLM-5.3-Flash (95 to 116 s per step).

### 2026-11-02: Several NIM keys rotate; limiter and cooldown are per key
- **Why:** the limit is account-wide, so more accounts is the only way past
  40 req/min. Env `NVIDIA_API_KEYS`; a 429 cools only that key and the retry
  moves to another at once; a 401/402 retires a key for the session unless it is
  the last usable one (single-key setups keep their normal error).
- **Rejected:** model fallback chains for throughput (limit is per account, not
  per model); a global cooldown (one limited key would stall the rest).

### 2026-11-02: nim_agent is read-only; roles are thin wrappers
- **Why:** NIM has no filesystem, so Castor runs grep/read_range/list_dir for it,
  confined to `cwd` with secrets and `.git` denied. The bulk-reading savings come
  first and carry no write risk. Every `path:line` is checked against the files.
- **Rejected for now:** write access. The audit in the NQC repo
  (`docs/drafts/fable-token-usage.md`) puts 56% of cost in implementers, so
  writes could save more, but rework can erase it and Unity work needs the Editor.
  Plan: patch-file output only, Editor-free tasks only, after the review pilot.
- **Rejected:** replacing the agy first-pass review with `nim_first_pass_review`
  before measuring its hit rate.
