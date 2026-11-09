# Decisions

## Earlier decisions (summary)

2026-06-05: kept the `gemini_*` tool names after the move to agy (renames
would break every install; `agy_*`/`castor_*` rejected); agy runs as a
captured subprocess, not a PTY; exploration uses `cwd`/`add_dirs`, and
only user-named files are inlined. 2026-06-08: the server persists only to
`~/.cache/claude-castor` (project state at `projects/<sha256(cwd)>.json`);
only skills write Claude memory. 2026-07-31: Castor owns session
transcripts (replayed into fresh `agy --print`, summarized over ~24k
chars) instead of agy `--continue`; `gemini_index` does not default to
agy's `repo-index` agent (broke schema output on 1.1.9); `--mode` is not
used (`accept-edits` hangs headless, `plan` is a no-op; a passthrough was
built and reverted); streaming parked (fastmcp had no incremental output
API; later done for NIM chat requests); the agy surface check lives in
tracked `scripts/`. 2026-10-26: the sandbox tier is `--sandbox
--dangerously-skip-permissions`, since agy 1.3+ auto-denies shell commands
headless (workspace writes blocked, `~` writes and network not; the user
accepted that; plain trust and settings allow-rules rejected);
`PRINT_TIMEOUT` stays 300 s (Claude Code's MCP tool timeout is far longer
and `gemini_index` needs ~2 min; 45 s rejected).

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
- **Superseded 2026-11-09:** dekko is now an optional `dekko` extra, not a required dependency. Castor still runs its own copy beside the server.

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
- **Amended 2026-11-04:** `castor_compare_runs` has no NIM reading, and `cwd`
  must be a `CASTOR_RUN_ROOTS` entry or a git worktree of one (not any
  folder under it). See the entries below.

### 2026-11-04: Ultra is not a fallback for Super
- **Why:** in the 09:00 hour (local) of 2026-11-03 Ultra was rate limited on
  252 of 326 requests, while Super ran 556 ok and 0 rate_limit in that same
  hour (Super was limited later that day, 158 rate_limit at 12:04 to 12:07).
  Counting by local minute over 2026-11-02 and 11-03:
  10 minutes held an Ultra rate_limit, 2 of them (09:37 and 09:38 on 11-03)
  also held successful Super requests (26 in all), and 8 had no Super
  traffic. The 8 are most of the 429s (245 of 257) because the burst ran
  09:39 to 09:42 after Super traffic stopped, so the log alone cannot show
  whether Ultra's limit is separate or just hit first at 64 to 81 requests a
  minute. The overlap is small but real.
- **How:** GEMINI.md no longer tells Claude to pass Ultra when Super is slow
  (all three table rows now say "not as a fallback"); for a slow or limited
  Super it points to the `_start` job form, which waits up to
  `CASTOR_NIM_JOB_MAX_WAIT`. This amends the 2026-11-02 "limit is per
  account, not per model" premise: not confirmed either way (2 overlap
  minutes). Treat a per-model Ultra limit as a lead to test before building
  model fallback chains for a later second-provider design.
- **Rejected:** keeping Ultra as the fallback for a crawling Super (it adds
  a tighter limit exactly when load is high).

### 2026-11-04: nim_explain_row stays gated: its reading failed the gate
- **Why:** the gate run (commit 5013a0d, Super with dekko on, 2 runs, 12 NQC
  cases, results in ~/.cache/claude-castor/eval/results-20261104-094551.json)
  scored run 1 at site 11/12, reading right 1/12, wrong 2, errors 0, and
  run 2 at site 12/12, reading right 3/12, wrong 2, errors 0. The bar needs
  reading right 8 or more and wrong 1 or fewer, so both runs failed on the
  reading. Of 24 readings, 16 were unclear, 7 game and 1 harness. All 4 wrong
  ones were "game" on harness cases (nqc-tear-consoles both runs,
  nqc-washer-consoles run 2, nqc-garage-carry run 1), mostly console-warning
  rows where the model took a Unity warning for a game fault. Finding the
  code that decides the verdict hit 23 of 24.
- **How:** the tool stays behind CASTOR_NIM_EXPLAIN_ROW=1; the bar is
  unchanged. The site part passed on its own, so a variant without the
  harness/game reading is a separate decision for the user. CLAUDE.md and
  GEMINI.md say the gate ran and failed on the reading.
- **Rejected:** tuning the bar or the prompt after seeing these results.
- **Superseded 2026-11-04:** location-only and on by default ("nim_explain_row
  (location only) passed its site gate").

### 2026-11-04: nim_explain_row: improve the reading before shipping (user)
- **Why:** its gate (results-20261104-094551.json) found the verdict code in
  23 of 24 runs but got the harness/game reading right in only 4 of 24
  (unclear 16, all 4 wrong answers "game" on harness cases). The misses need
  NQC's own rulings on what is acceptable (tear-consoles: a host JobTempAlloc
  warning the fix allowed in the harness), which the model never sees.
- **How:** next, give the agent NQC's rulings (`DECISIONS.md`) or the fix
  history, then re-gate on a new or extended case set with the bar set
  before any run. The tool stays behind `CASTOR_NIM_EXPLAIN_ROW=1` until then.
  Note for the design: the gate's model called a dekko tool in 1 of 24 runs.
- **Rejected:** shipping a location-only variant now (Claude's
  recommendation: drop the Reading line, add a precondition check, re-gate
  the site); leaving it gated with no further work.
- **Superseded 2026-11-04:** the reading was cut ("nim_explain_row becomes
  location-only"); the caller reads the cited code.

### 2026-11-04: feature/nim-edit merged with nim_edit gated (user)
- **Why:** gate run 6 failed the strict bar on two whitespace patches, but the
  branch carried the dekko agent tools phase 1 needed. Merged into develop
  (027fda7, no squash) with nim_edit registered only under
  `CASTOR_NIM_EDIT=1` (`_register_gated`); the strict bar stays.
- **Rejected:** another prompt round before merging (more free-tier gate
  runs); relaxing the bar to accept whitespace misses Claude would see in the
  diff.

### 2026-11-04: explain-row eval labels come from fix commits (user)
- **Why:** the user chose no manual check of the 12 cases. A case is
  `harness` when its fix touched only Tools/, probes or test scripts, `game`
  when only game code under Assets/; fixes touching both are dropped. Cases:
  `~/.cache/claude-castor/eval/nqc-explain-cases.json` (7 harness, 5 game, 8
  check rows), reviewed twice; the floorcheck trio was replaced because its
  logs ran on uncommitted layout edits.
- **Rejected:** the user confirming each label (12 calls of their time).

### 2026-11-04: explain-row reading round: fix history, blind screen, bar set (user)
- **Why:** the last gate's labels are partly team choices (the two mansion C
  walk-off cases log the same symptom, one fixed in the harness, one in the
  game), so some cases cannot be decided from pre-fix evidence. Spec:
  `.docs/explain-row-reading-design-2026-11-04.md`.
- **How:** the only new context is the script's fix history
  (`_script_fix_history(repo, rev, name)`, last 15 commits to files named
  like the log's folder, as of HEAD; the eval passes the case commit). A
  pool of ~24 cases (12 old, ~12 new) is screened blind by a Sonnet subagent
  per case; a case stays only if it names the labeled side; under 10
  survivors means no gate. Bar per run over 2 Super runs: site >= 80%,
  reading right >= ceil(2N/3), wrong <= 1 (<= 2 when N > 15), 0 errors; one
  `history=off` control run, reported only.
- **Rejected:** caller-named rulings files, pointing the agent at NQC's
  decision index, prompt-only rules; Claude screening with labels known; the
  user screening; keeping the 12 cases unscreened.
- **Superseded 2026-11-04:** the fix-history block and the eval `history`
  switch were removed with the reading (b60dc85).

### 2026-11-04: explain-row reading round stopped at the screen; stays gated
- **Why:** of 21 cases (12 old, 9 new mined from fix commits; 12 harness, 9
  game), a blind Sonnet reader with the new fix-history block named the
  fixed side on 9 (game 6 of 9, harness 3 of 12; 11 unclear, 1 wrong:
  mansionA-bookcase-walkoff read harness on a game-fixed case, citing the
  hip-y off test at mansion.sh:258). Under the floor of 10 set before any
  run, so no NIM gate ran. Harness faults are the hard side: the evidence
  rarely shows the harness is wrong, only that a check failed. Pool with
  `screen` fields: `~/.cache/claude-castor/eval/nqc-explain-pool.json`;
  survivors: `nqc-explain-gate.json`. Branch `feature/explain-row-reading`
  (fix-history block, eval `history=on|off`, `--print-tasks`, scaled bar).
- **How:** `nim_explain_row` stays behind `CASTOR_NIM_EXPLAIN_ROW=1`. The
  site half still works (23/24 last gate). Merging the branch is the
  user's call; the history block is harmless while the tool is gated.
- **Rejected:** mining more cases to clear the floor, or lowering it, after
  seeing the screen.
- **Superseded 2026-11-04:** no longer gated; see "passed its site gate".

### 2026-11-04: explain-row reading: frontier Claude can read it, free NIM models cannot
- **Why:** an Opus blind re-screen of the 12 cases Sonnet left undecided got
  7 right (5 harness, 2 game), 4 unclear, 1 wrong (mansionA-bookcase-walkoff,
  where Sonnet and Opus both read harness from mansion.sh:258's hip-height
  test: likely a label problem). So 16 of 21 cases are decidable from
  pre-fix evidence at Opus level (Opus did not re-read Sonnet's 9). A
  measurement run (results-20261104-162059.json, history on, dekko on, 2
  runs each, not a gate) on all 21: Super site 41/42, reading right 7/42,
  wrong 3, mostly unclear; Ultra site 37/42, right 9/42, wrong 7, mostly
  "game" on harness cases (the costly error), 64 s per run vs Super's 37 s.
  On the 16 decidable cases: Super right 6/32, Ultra 8/32. Super on the old
  12 cases with history: right 5/24, wrong 3 (morning gate without: 4/24,
  wrong 4), so the history block did not move it.
- **How:** the reading capability is in the calling Claude, not in the
  free models; the site half is reliable on Super. Recommendation to the
  user: location-only NIM tool, the caller reads the cited code.
- **Rejected:** retracted the earlier claim that pre-fix evidence was the
  limit (it rested on one Sonnet pass).

### 2026-11-04: nim_explain_row becomes location-only (user)
- **Why:** the free NIM models cannot read harness vs game (Super 6/32,
  Ultra 8/32 on the 16 decidable cases) while Super finds the verdict code
  (41/42), and NQC's orchestrator is a Claude model that can read it.
- **How:** the answer is four lines (Row, Verdict code, Precondition, Trial
  lines); JSON drops `reading` and `why`; the fix-history block and the
  eval's `history` switch come out. Gate, bar set before any run: all 21
  pool cases, labels ignored, 2 runs, Nemotron 3.5 Super, dekko on; pass
  when every run has the verdict code within 3 lines of a known site on at
  least 17 of 21 and 0 errors. Pass: register by default, update CLAUDE.md,
  GEMINI.md and the NQC handoff. Fail: stays gated. Same branch
  (`feature/explain-row-reading`).
- **Rejected:** keeping a reading step "for later"; raising the site bar
  after seeing 41/42.

### 2026-11-04: castor_compare_runs ships without the NIM reading (user)
- **Why:** the reading measurement above: free models cannot tell a
  harness fault from a game fault, and a confident wrong reading costs
  more than none.
- **How:** phase 2 plan Task 6 drops `explain` and its NIM call; compare
  stays deterministic and the caller reads the rows. Phase 2 starts after
  the location-only explain-row and the rename of the triage-specific
  shared names.

### 2026-11-04: nim_explain_row (location only) passed its site gate; on by default
- **Why:** bar set before the run (above). results-20261104-191044.json: 21
  NQC pool cases, 2 runs, Super, dekko on: site 18/21 and 19/21, 0 errors.
  Spot-checked: hits cite the expected lines; misses are real ("not
  found", a sibling check at doors.sh:973, an editor report for
  paths-pockets). Lower than the 41/42 measurement with the reading step
  (37/42 now): one window, noise likely, not chased.
- **How:** `@mcp.tool()` on both tools; `CASTOR_NIM_EXPLAIN_ROW` gone.
  Branch `feature/explain-row-reading` at 52a8c1c.

### 2026-11-04: compare masks clock stamps; numbers_changed compares N of M only
- **Why:** final review on real NQC logs: the doors logs stamp most lines
  `[HH:MM:SS]` (every trial line differed), and details such as "Sharp
  contacts 110 128 141" are measurements, so PASS rows were listed as
  numbers_changed. User let me decide.
- **How:** `_mask_timings` masks clock stamps and decimals; numbers_changed
  keys on `(\d+) of (\d+)` pairs only. Spec section 2 amended.
- **Rejected:** a per-project mask regex in `.castor/run.json` (not needed
  yet).

### 2026-11-04: Run roots are exact; one castor process watches a run
- **Why:** the final review of phase 2. With "inside a listed root", the
  handoff's `CASTOR_RUN_ROOTS=~/Projects/game-development` let any clone
  dropped there authorize itself; and every Claude session runs its own
  castor, so two sessions polling one run both watched it (overdue command
  twice, exit code overwritten as lost; reproduced).
- **How:** `cwd` must equal an absolute entry or be a git worktree of one
  (NQC lists `no-questions-cleaning`, which covers every `nqc-lane*`). A
  monitor holds an exclusive `flock` on `runs/<id>.lock`; record writes
  use per-writer temp files. Stopped/gave_up runs whose pid is alive count
  toward `max_running`; the overdue command honours `CASTOR_RUN=0`.
- **Rejected:** keeping the spec's "inside" rule with docs telling users to
  list exact lanes (one wrong entry reopens it).

### 2026-11-06: overdue_command is not checked against allow
- **Decision:** `overdue_command` must be an executable file inside the root
  (`_run_resolve_program`) but need not match `allow`. This amends the design's
  "checked against the same allowlist".
- **Why:** `allow` limits what a caller can start; `overdue_command` is fixed by
  the committed config and takes no caller input. NQC keeps `dialog.sh` out of
  `allow` on purpose (suite scripts only) yet wants it on overdue, so the old
  check refused every run and no NQC suite ever used the tier.

### 2026-11-06: server.py split into the castor package
- **Decision:** the 11k-line `server.py` is now a thin entry point over a
  `castor/` package (`app`, `common`, `jobs`, `logs`, `agy`, `nim/*`, `run/*`;
  map in `CLAUDE.md`). The MCP registration still runs `server.py`.
- **Why:** gemini and NIM code were interleaved in one file; the split follows
  the old section banners. Cycles were broken by moving small helpers down
  (log format into `logs`, path confinement and JSON-fence parsers into
  `common`, the edit store into `nim/edit_tools`), not by lazy imports, except
  two (`common._compact_session`, `jobs._run_job`, both reaching `castor.agy`).
- **Tests:** they read and patch through `castor._namespace.view()`, which
  sets a name in its defining module and every `from`-importer, so the old
  `monkeypatch.setattr(server, ...)` calls kept working unchanged.

### 2026-11-09: agy and NIM separated; shared tools renamed castor_* (user)
- **Decision:** `castor/agy` and `castor/nim` never import each other or the run
  tier. Seven tools that serve every backend moved to `castor/tools.py` and were
  renamed `gemini_*` to `castor_*`, with no aliases. Each backend owns its job
  pool. `tests/test_separation.py` enforces the import rules.
- **Why:** agy could not load without NIM, and NIM's concurrency settings set
  agy's background worker count (4 with no keys, 8 with three).
- **How the links were cut:** backends register with neutral modules
  (`_register_status_line`, `_register_poller`, `_register_job_ok_prefix`). The
  run tier imports NIM only inside `_run_triage`, which is the one allowed edge.
- **Rejected:** keeping `gemini_*` aliases, since the user chose the rename.

### 2026-11-09: compare noise is a per-project regex list, not blanket integer masking (user)
- **Why:** NQC asked compare to ignore counters like `group=N`. Masking every integer would also hide `sent=0` vs `sent=10`, the signal in the litigious host row. `log_format.noise` lists the patterns a project names; matches become `#` before trial lines are compared. A pattern that matches empty is refused.
- **Rejected:** masking all integers in trial lines.

### 2026-11-09: compare lists a line shared by several rows once; `new` is a class
- **Why:** NQC saw `haul:` and `briefing:` repeated under every changed row. The rule is generic: a differing line (after masking) that shows under two or more rows of one run is printed once after the rows. It has no knowledge of those names and is untested on a real NQC log. `new` means absent from the first runs and present from some run on; a gap or a vanished row stays `missing`; a new row that FAILs is still `failing`.
- **Rejected:** a rule keyed to `haul:` / `briefing:` prefixes (project-specific).
- **Correction 2026-11-09:** NQC found it had misread its logs: `haul:` and `briefing:` sit under one row, once per compared run. The section never appeared on its real logs and is still untested there. Kept because it is harmless and may help other suites.

### 2026-11-09: castor_run_start takes `env`, limited to names in run.json `env_allow` (user)
- **Why:** row-limited NQC runs (`ROWS`, `TROWS`, `N`) had to go through `nqc-runner`. The project's committed run.json names the allowed variables, so a caller cannot widen it; `NVIDIA_*`, `CASTOR_*`, `LD_*`, `DYLD_*`, `BASH_FUNC_*` and shell or loader names (`PATH`, `BASH_ENV`, `IFS`...) can never be allowed. Values: printable ASCII without `$` or backtick, 500 chars, 20 variables. Kept in the run record, `gemini_poll` and `castor_runs`.
- **Rejected:** a tight safe-character set for values (NQC row names have spaces, parentheses and apostrophes); free-form env from the caller (a cloned repo could not authorize itself, but a caller could inject loader variables).

### 2026-11-09: develop fast-forwarded in the main checkout; installed copies synced by hand
- **Why:** the registration runs `server.py` from this checkout, so merging to `develop` here is the install. `commands/castor/compare.md` and `GEMINI.md` were copied to `~/.claude/` with `cp`, not `install.py`, which would re-register and could disturb the env block.

### 2026-11-09: missing trial lines collapse into one note, worded "trial line"
- **Why:** NQC's `twists` compare had 16 of 45 diff lines as `(no line N; ...)`, one per line a later run lacked. Now one note per row and run (`(no trial lines 7 to 20; run1 trial line 7: ...)`); 45 lines became 33 with nothing real lost. The numbers count a row's trial lines, so the wording says so: NQC read "line 7" as a log line (it was line 25).
- **Rejected:** absolute log line numbers (the row block offsets pass through `_clean_lines`, so they could be wrong); masking more of the diff (NQC: the remaining `haul:` coordinates are real differences).

### 2026-11-09: dekko becomes an optional extra; httpx stays required (user)
- **Why:** dekko adds 14 packages and 22 MB, and until 1.9.35 is on PyPI a failed GitHub fetch stops the whole server from starting. The code already works without it. `install.py` keeps the extra on by default (`--no-dekko` skips it). `httpx` costs 2 packages and 2.7 MB.
- **Rejected:** optional `httpx`, since every `nim_*` tool would vanish on a default install and `server.py` would need guarded NIM imports. On `refactor/backend-separation` until merged.

### 2026-11-09: nim/backend.py split by job, imports one way; NIM reset is one per module
- **Why:** 1921 lines mixed settings, limits, records, HTTP and tools. `config` imports no NIM module, `limits` and `records` import `config`, `transport` imports those, `backend` sits above, `summarize` above it. `_nim_reset_state` lives in `agent.py` and calls `_reset_limits` and `_reset_client`, because a `global` rebind only reaches its own module. `_NIM_CONTEXT_MARKERS` and `_nim_retire_key` sit in `transport` to keep imports one way.
- **Rejected:** a holder object for rebound state (more change for no gain); splitting agent, roles or edit (out of scope). On `refactor/backend-separation` until merged.
