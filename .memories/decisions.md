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
- **Superseded 2026-11-02:** agent roles moved to Nemotron 3.5 Super first,
  then plain calls too (see "Plain NIM calls default to Nemotron 3.5 Super").

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

### 2026-11-02: Agent reasoning budgets are 1024 per tool step, 6144 for the answer
- **Why:** Nemotron 3 Ultra's reasoning is unbounded: tool steps sometimes ran
  4,000 tokens (80 s) and one answer outran the 180 s timeout. Tool steps only
  pick the next lookup. On the same review request, a 2,048 or 0 budget made the
  answer claim a false bug that the unbounded run avoided, so the answer keeps
  6,144. Sent as `reasoning_budget`, only to `nvidia/nemotron*` models.
- **Rejected:** thinking off everywhere (11 s vs 79 s, but worse reviews);
  raising timeouts instead (slower, still unbounded).

### 2026-11-02: No self-verification pass for nim_first_pass_review
- **Why:** asked to verify a review's findings against the code, Nemotron
  refuted 1 of 5 known-false findings and "confirmed" a deadlock that isn't one
  (a lock taken twice in sequence). Across 3 reviews after the prompt fixes,
  roughly half the findings were false and severity ran high. The review stays
  a lead generator that Claude must verify.
- **Rejected:** more prompt rules (the concrete-input format and severity rules
  were only partly followed).

### 2026-11-02: nim_first_pass_review is an occasional extra, not a pipeline step
- **Why:** the NQC pilot ran it on 5 reviewed diffs (game logic, networked
  code, harness checks, one mostly clean diff) with an NQC focus block. 25
  leads: 5 confirmed, all repeats of Claude findings (Minor or Important); 12
  refuted; 8 n/a. It caught 0 of 3 known findings: it called C449's test that
  can't pass fine, called bagsync.sh's fail-open check correct (the opposite of
  the bug), and missed a shared-stair-flight Critical while raising a false
  one. The focus block didn't stop false Criticals. NQC removed the first pass
  from its overnight workflow and kept NIM for log triage and summaries.
  Nemotron 3 Ultra is the strongest model these keys can call, so retry only
  with a stronger model.
- **Rejected:** finishing the planned 15 reviews (each refuted lead costs
  reviewer tokens, and 5 reviews already showed nothing unique); more prompt
  rules (tried in castor before, partly followed).

### 2026-11-02: Agent roles default to Nemotron 3.5 Super; no split-model default
- **Why:** scored with `scripts/nim_eval.py` (3 castor cases plus the 5 NQC
  pilot diffs, 15 NQC runs per model). On NQC, Ultra and 3.5 Super had the same
  lead precision (14%), recall 17% and 12%, and 3 of 12 must-catch hits each, but
  3.5 Super took 80 s per review against 203 s. Both got full marks on the
  castor scout, triage and planted-bug cases; 3.5 Super did them in 10 to 30 s.
  A set `CASTOR_NIM_MODEL` is still followed (self-hosted setups).
- **Rejected:** Mistral Large 4 (every request timed out at 180 s on the free
  tier); fast step model plus Ultra answer (Lightning or 3.5 Super steps scored
  12 to 13% precision, no better, and still slow); deciding on one run (3.5
  Super first looked twice as precise as Ultra, then matched it over three).

### 2026-11-02: Reviews get Castor-built context and quote-checked citations
- **Why:** against the old code on the same NQC diffs (Ultra), recall rose from
  10% to 17%, must-catch hits from 0 of 4 to 3 of 12, failures from 1 in 5 to 0
  in 15, and time fell from 336 s to 203 s (80 s on 3.5 Super). Precision did not
  move (12% to 14%): the model still invents bugs on real diffs, so the first
  pass stays an occasional extra. Quote checks catch wrong-line citations (about
  1 in 4 quotes on NQC answers did not match their line).
- **Rejected:** narrow cross-checked questions (not worth building until the
  remaining problem is shown to be precision the model could fix); caching role
  results (repeat runs are rare).

### 2026-11-02: Plain NIM calls default to Nemotron 3.5 Super
- **Why:** `scripts/nim_eval.py` plain roles (3 explain cases with known
  causes, 3 docs with known key points; 2 runs each, response cache off).
  Super recall 44/48, must 16/18; Ultra 39/48, must 14/18 (missed GEMINI.md's
  "NIM is not an agent" and CLAUDE.md's "agy replaces the Gemini CLI" in both
  runs). The decision rule (keep Ultra only if its recall is higher on both
  roles by more than its own run spread) did not hold on either role.
  Results: `~/.cache/claude-castor/eval/results-20261102-212138.json`.
- **Speed is load-dependent, not settled:** in that eval (21:00 local) Super
  averaged 75 s per call against Ultra's 16 s. The day's usage log says that
  hour was the exception: Super ran 127 tokens/s at 19:00 and 93 at 20:00, then
  29 at 21:00; Ultra ran 35 to 59 all day. GEMINI.md tells Claude to pass Ultra
  when Super is crawling.
- **Rejected:** keeping Ultra on that hour's latency alone (one hour against a
  day of logs showing Super faster per token).

### 2026-11-02: Review steps stay at 8
- **Why:** the nim-performance design's gate for cutting `NIM_REVIEW_STEPS`
  to 4 was "must-catch must not drop". At 4 steps (2 runs, 3.5 Super) NQC
  must-catch fell from 2/8 to 0/8, though lead precision rose from 9% to 17%
  (both scored with SCORING_VERSION 2; the 8-step file was rescored).
  Time could not be judged: NIM was slow that hour (168 s median at 4 steps
  vs 70 s at 8 earlier). Castor's planted-bug case was 2/2 both ways.
  Results: `results-20261102-213502.json` (4 steps),
  `results-20261102-200125.json` (8 steps).
- **Rejected:** 4 steps on precision alone (two runs is noise-level, and a
  missed must is the costlier error); re-running until it passes.


### 2026-11-02: Log triage follows the log's own verdict when it has one
- **Why:** the real NQC hallway log has 258 harness Tracebacks but its table
  says `fail 0`; the design's marker rule ("failed" on any hard marker) called
  it failed. Now the log's verdict or totals decide; otherwise "failed" on hard
  markers, "inconclusive" when only NOT ASSESSED matched. Both live runs gave
  "inconclusive" with all 7 NOT ASSESSED rows.
- **Rejected:** the design's marker-only verdict.

### 2026-11-02: Summarize parts are 120k chars on their own thread pool
- **Why:** at the design's 240k chars, each part's summary covered only its
  first third. Parts run on a 4-worker pool of their own so a full job pool
  can't block them. Summary requests get 6,144 tokens with 1,024 thinking (at
  4,096 the JSON was cut off).
- **Rejected:** 240k parts; running parts on the shared job pool.

### 2026-11-02: Unity skip dirs are NIM-agent-only
- **Why:** the design put `Library`, `Temp`, `obj` in the shared `SKIP_DIRS`,
  but it also ruled out agy-path changes. They live in `NIM_AGENT_SKIP_DIRS`.

### 2026-11-03: dekko agent tools stay on by default (0b gate passed)
- **Why:** `scripts/nim_eval.py`, Nemotron 3.5 Super, scout/review/triage cases
  (3 castor + 5 NQC), 2 runs each, configs interleaved in the same window.
  dekko on vs off: recall 23/62 vs 23/62, must-catch 14/20 vs 13/20, lead
  precision 57% vs 51%, quotes matched 61/70 vs 52/69, median 52 s vs 65 s,
  6.2 steps both. The gate (recall and must-catch do not drop; median steps
  or seconds fall) holds on time. Results:
  `~/.cache/claude-castor/eval/results-20261103-075502.json`.
- **Caveat:** two runs per config; NQC review hits move between runs (e.g.
  M6-nrm-sign found once with dekko off, not with it on). Speed is
  load-dependent; both configs shared the hour.
- **Rejected:** shipping dekko off by default (no measured loss to justify it).

### 2026-11-03: Review context keeps grep for call sites (0c gate failed)
- **Why:** review cases (1 castor + 5 NQC), Super, 2 runs, all in one window,
  dekko agent tools on in both: dekko-sourced `_review_uses` recall 8/44,
  must-catch 6/12, precision 26%, median 70 s; grep-sourced recall 12/44,
  must-catch 6/12, precision 28%, 71 s. Recall dropped and time did not
  fall. Part of the gap is one long grep-context run on nqc-11d2-3a (28
  leads), so it may be noise, but nothing showed a gain. Reverted
  (c8d5d29, bca5f83). Results: `results-20261103-080727.json` (dekko
  context, with an on/off pair) and `results-20261103-080211.json` (grep
  context).
- **Note:** the plan's on/off comparison mixed agent tools and review
  context; the gate compared dekko-on runs of the two servers instead.
- **Rejected:** keeping it on the 0b gate's strength (different change).

### 2026-11-03: No `[1m]` NIM model ids; Castor's input cap stays at 300k chars
- **Why:** the catalog has no `[1m]` ids and the API 404s them (`[1m]` is a
  Claude Code naming convention). The plain ids already take a ~950k-token
  needle prompt (Super 109 s, Mistral Large 4 216 s). The 1,048,576 figure
  NIM reports is a gateway `max_tokens` cap. Castor's 300k-char budget is
  about answer quality (summaries cover only the start past ~35k tokens),
  not the window.

### 2026-11-03: Mistral Large 4 stays out (confirmed with an eval)
- **Why:** `scripts/nim_eval.py`, 14 cases x2 vs Super: 9/28 runs failed
  (NQC reviews ran 40-60 min), median 1,262 s vs 79 s, recall 62/110 vs
  64/110, must-catch 27/38 both. It rejects `reasoning_budget` (400);
  `reasoning_effort: "low"` works but it still ran ~9 tokens/s.
- **Rejected:** background-only use (no quality gain to pay for the wait).

### 2026-11-03: NIM write access starts as `nim_edit`, staged behind gates (user)
- **Why:** the review pilot closed negative, so start with mechanical edits
  only. NIM returns exact-text blocks; Castor checks them, builds the diff,
  runs `git apply --check`, writes a patch file; Claude reads and applies.
  Stages after this one (fix rounds, NQC Editor-free tasks, a general edit
  tool) each need their own spec, eval and pilot. Gates: offline eval
  (>=90% correct, 0 wrong-but-applies, 0 out-of-scope, refusals refused,
  >=2 runs), then a 10-edit pilot (>=9 applied as-is).
- **Rejected:** Castor applying patches or running checks in a worktree;
  NIM-written unified diffs (fuzzy repair hides misplaced edits); whole-file
  rewrites; setting the bar after a baseline.

### 2026-11-03: dekko is a declared dependency; Castor builds the map (user)
- **Why:** the CLI (callers, symbol, outline, context, search, trace) needs
  a map; a `.dekko/` folder in a project is acceptable. Castor runs its own
  pinned dekko (`Path(sys.executable).parent / "dekko"`), never PATH's, and
  `scripts/check_dekko_surface.py` guards the contract. Not used: `dekko
  sanity` (whole-tree grep ignoring .gitignore and the deny list) and
  `dekko diff` (needs a rev; eval snapshots have no commits).
- **Rejected:** existing maps only; waiting for an out-of-tree map option.

### 2026-11-03: must_cover rules for nim_edit
- **Why:** call sites and the definition must be edited everywhere;
  grep-only references must be edited inside `files` (all of cwd when
  absent); references outside `files` are listed in the OK result, not
  refused, so dated notes can stay unedited and visible. The map and file
  index are refreshed before every coverage round (a 60 s cache let a new
  call site slip through). Limitation: dekko does not index module
  constants, so must_cover cannot be used for them.
- **Rejected:** every reference everywhere (forces rewriting history or
  refusing); call sites only (non-call references slipped through).

### 2026-11-04: nim_edit stage 1 gate run 6 failed on two whitespace patches
- **Why:** after the propose_edit tool (Task 14) and its review fixes,
  Super with dekko on scored 30/33 (91%) correct, 0 out of scope, 6/6
  refusals, 0 errors, but 2 wrong patches; dekko off scored 24/24 without
  the must_cover cases. Both wrong patches are valid edits with a whitespace
  mistake: a missing blank line (first-pass-docs, 1 of 3) and six lines
  wrapped that were within 79 columns (rename-test-helper, 1 of 3, as in run
  2). Results: `~/.cache/claude-castor/eval/results-20261104-075545.json`.
- **How:** the strict bar (0 wrong applies) stands until the user rules;
  the next step is theirs.

### 2026-11-04: NQC asks go in two phases; run and compare live in castor (user)
- **Why:** the NQC orchestrator's five asks after the 2026-11-03 night.
  Sonnet runners cost ~300K tokens a night babysitting `Tools/playtest`
  commands, and unaligned reruns cost ~1.5 h of fix rounds. Design and
  plans: `.docs/nqc-asks-design-2026-11-04.md`, `nqc-asks-plan-phase1-` and
  `-phase2-2026-11-04.md`; NQC handoff `.docs/castor-for-nqc-2026-11-04.md`.
- **How:** phase 1 = triage `format="json"` (Castor parses the model's text,
  plus a shared `@case` verdict-row parser), `CASTOR_NIM_JOB_MAX_WAIT`,
  GEMINI.md drops Ultra as a fallback, gated `nim_explain_row` (after
  `feature/nim-edit` merges; bar set before baseline). Phase 2 =
  `castor_run_start` (allowlist in a committed `.castor/run.json` plus
  `CASTOR_RUN_ROOTS` in the registration; never kills; records on disk) and
  `castor_compare_runs` (deterministic, `same_code` flag, optional reading).
  This supersedes, for `castor_run_*` only, "nim_agent is read-only" and the
  `nim_edit` rejection of Castor running checks.
- **Rejected:** triage `confidence` (uncalibrated; `verified`/`quote_ok`
  instead) and `owner` (triage reads no code; explain-row's `reading`);
  building run/compare as NQC scripts (Claude's recommendation; the user
  wants them in castor); Castor killing or taking the lock (scripts own it).
