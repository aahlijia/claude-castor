# Gotchas

- **agy auto-updates and has no version pin; its CLI has broken Castor twice
  (1.1.9, 1.3+).** Run `python3 scripts/check_agy_surface.py` before releases
  and re-test the sandbox path live after any agy update.
- **agy's command line is order-sensitive.** Since 1.1.9 `agy --print` takes the
  prompt inline in argv with no stdin fallback (1.0.5 read stdin). Keep the
  prompt right after `--print` and pass `--print-timeout=<t>` as one `=`-joined
  token or the parser swallows it as the prompt. The workspace comes from
  `--add-dir`, not the subprocess cwd: without `--add-dir <cwd>` agy explores
  its own scratch directory.
- **agy's permission modes are weaker and quieter than they look.** 1.3+ headless
  print mode auto-denies shell commands outside `--dangerously-skip-permissions`
  and exits 0 with empty stdout. The only traces are a stderr line containing
  "auto-denied" (text mode) or `denied_actions` with `response: ""` in the
  stream-json result event; `_empty_output_error` handles both, so don't trust
  the exit code alone. Allow-rules in `~/.gemini/antigravity-cli/settings.json`
  did not take effect in 1.4.7 print mode, and without shell commands agy can't
  enumerate files (it can `read_file` a named path; searching falls back to
  subagents and fails). `--sandbox` blocks writes to the workspace only: writes
  to `~` and `/private/tmp`, and network access, still succeed (1.4.7, macOS).
- **stream-json result layout:** discriminator is `event["event"] ==
  "result"` (not `type`), payload at `event["result"]["structured_output"]`,
  with the same JSON as text in `response`. Unchanged in 1.4.7.
  `--json-schema` needs a root object; a root array fails with no
  structured_output (semantic search wraps hits under `"hits"`).
- **Go easy on agy from polled paths and live probes.** Never call `agy changelog`
  from a polled path; it mutates a disk cache on every call. Heavy live testing
  hits the Antigravity individual quota ("Individual quota reached"), so keep
  probes small.
- **Anything not bracketed gets cached.** A run that returns non-`[` text is
  cached by `_dispatch`, so failure modes must return bracketed strings. Bump
  `CACHE_KEY_VERSION` when a fix means old entries may be bad. The JSON-mode
  NIM dispatchers also read a leading `[` as an error.
- **Every MCP subprocess must pass `stdin=subprocess.DEVNULL`**; inherited
  stdin is Claude Code's JSON-RPC pipe.
- **Each Claude Code session spawns its own castor server process**
  (`uv run --directory <repo> server.py`). Restart the session to pick up
  `server.py` or `castor/` edits; no reinstall needed. It runs whatever branch
  this repo has checked out, so switching branches here changes the tools every
  other project sees (NQC's overnight run uses the NIM roles). Keep `develop`
  checked out unless a session is testing a branch on purpose; work in
  worktrees. A registration change needs a restart too. `claude mcp add ...
  -e KEY=... -- /path/to/uv ...` needs a space after `--` (`--/opt/...` fails
  after the remove already ran, leaving castor unregistered); to add env vars
  without putting keys on a command line, edit `mcpServers.claude-castor.env`
  in `~/.claude.json` (back it up; a running session may rewrite the file, so
  check with `claude mcp get claude-castor` after the restart).
- **Slash commands and `GEMINI.md` are copies, not served from the repo.** The CLI
  reads `~/.claude/commands/castor/` and `~/.claude/GEMINI.md`; after changing
  `commands/castor/*.md` or `GEMINI.md`, `diff` then `cp` them over. Do not rerun
  `install.py` for that: it re-registers the server and can disturb the env
  block. `python3 -c "import install; install.install_commands()"` copies both
  without touching the registration. The installed `GEMINI.md` had drifted by
  whole sections before 2026-11-09.
- **Probing NIM by hand: keys and the catalog.** Probe scripts read keys from
  `~/.claude.json` (`mcpServers.claude-castor.env`); never print them, since keys
  pasted into a chat stay in that transcript. `nim_models` lists models the
  account cannot call: Kimi K2.6 and Nemotron 3 Super return 404 "Function …
  not found for account", and Gemma 4 31B and DeepSeek V4.1 Flash timed out. Make
  a live call before presenting a model as an option.
- **NIM findings need checking; a passing citation proves little.** The citation
  check proves a line exists, not that the claim is true. The scout named a
  function that does not exist at a real line, and review findings of absence
  ("no tests", "not documented") were often wrong. Paths quoted from a log are
  tagged `[unverified]` (false positive). A review reported "citations verified
  9/9" while one cited range was the wrong branch of the function. Nemotron
  reviews also repeat the same false beliefs: that braces in a value passed to
  `str.format` raise KeyError, that taking a lock twice in sequence deadlocks,
  and that a `..HEAD` rev range is rejected. Discard those.
- **Nemotron keeps calling tools while the history holds tool calls.** Omitting
  `tools` and sending `tool_choice: "none"` both still returned a tool call and
  no content (3/3 replays), so the agent's last step failed with "hit its step
  cap". `_agent_answer_messages` now flattens the transcript into system + one
  user message for the final request (6/6 replays answered).
- **Nemotron reasoning has no default bound and spends its whole budget.** A
  step can think for 4k+ tokens and return nothing (`finish_reason=length`);
  tiny `max_tokens` budgets return no content at all (8 tokens made
  `nim_status` say NOT READY; verify uses 512). NIM honors `reasoning_budget`
  (and `nvext.max_thinking_tokens`) exactly; `reasoning_effort` does nothing;
  `chat_template_kwargs.enable_thinking=False` turns it off. At ~55 tokens/s a
  1,024-token step budget is ~18 s before any output, so a 2-request triage
  cannot finish in 15 s. Ultra spends every tool step it is given whatever the
  prompt says (cap steps in code). The answer request can outrun 180 s; it has
  its own 360 s timeout, and each timeout is retried once, so a stuck request
  costs about 6 minutes.
- **NIM summaries over ~35k tokens of input cover only the start.** A 216k-char
  part of DECISIONS.md produced 15 points from its first third and none of
  the C-rulings after it; at 120k-char parts every part was covered. The merge
  also packed all bullets into one newline-joined string once; `_tidy_summary`
  splits them.
- **NIM speed swings by the hour.** 3.5 Super ran 127 tokens/s at 19:00 and 29
  at 21:00 on 2026-11-02 (Ultra 35 to 59). Never compare models on one hour's
  timings; check `nim_usage` or the usage log by hour. The usage log has no
  key and no plain-vs-agent field (model, ok, kind, seconds, tokens only), so
  bucket by local minute to compare models (the Ultra check found only 2
  overlap minutes).
- **Eval scores need checking.** Matching by file and line alone credited the
  old reviewer with 9 of 10 must-catches where hand scoring said 0 of 4, so
  findings need keywords (and loose ones still slip: `empty` matched
  "non-empty"). One run per model is noise (Super looked twice as precise as
  Ultra, then matched it over three). Spot-check hits, repeat runs, re-score
  with `--rescore` after editing cases.
- **Agent grep reads the git index, cached 60 s per root.** A file created
  mid-run is missed until the cache expires or `_nim_reset_state()`; tests
  that add files after a first grep must reset. Outside git it still walks
  (pruning `NIM_AGENT_SKIP_DIRS`).
- **dekko inside the NIM agent: three traps.** Error text can leak denied files:
  an ambiguous name exits 4 and lists every candidate on stderr (incl. `.env*`
  files) even with `--json`, so all dekko output, errors included, goes through
  `_dekko_scrub`. `must_cover` on a module constant always refuses, because dekko
  does not index module-level constants; use it for functions, methods and
  classes, and for a constant scope with `files` and check the diff by eye.
  Offering dekko tools does not mean the agent uses them: in the explain-row gate
  (24 runs, dekko maps built) the model called a dekko tool once, so read the
  traces before crediting dekko for a score. The eval's `dekko=on|off` flips
  agent tools and review context together; isolate a review-context change by
  comparing dekko-on runs of two server files (`--server`) in the same window.
- **The NIM test fixture blocks subprocesses.** `no_agy` in `tests/test_nim.py`
  stubs `subprocess.run` so agy never runs; only `["git", ...]` passes through
  to the real one (the agent's file map needs `git ls-files`). A new helper
  that shells out to anything else fails in tests until the fixture allows it.
- **Patching a castor name only reaches the module you patch.** After the split,
  `from castor.x import name` copies the binding, so `monkeypatch.setattr` on
  the defining module misses importers. Tests and `scripts/nim_eval.py` use
  `castor._namespace.view()`, which patches the definer and every importer. New
  tests should use it too. pytest's undo calls `delattr` on the view, which
  restores the saved originals.
- **Building patches: two ways to get a bad one with no error.** `git apply` reads
  diff header paths from the repo top level, so a diff with cwd-relative paths
  from a subdirectory passes `--check` and then skips the file or patches a
  same-named file at the top; `_edit_make_patch` computes the `--show-prefix`
  itself, so never build a patch another way. Applying only some `-U0` hunks with
  `git apply --cached --unidiff-zero` put an insertion on the wrong line and the
  commit had a syntax error. To split a file across commits, build each version
  from the base blob (`git hash-object -w`, `git update-index --cacheinfo`), then
  test each commit from `git archive`.
- **The run monitor rebuilds its config from the run record, not run.json.**
  `_run_monitor` calls `_run_config_from(rec["config"])`, so any `RunConfig` field
  the monitor or the result needs must round-trip through `_run_config_dict`.
  `log_format` did not, and every run's result parsed with the default layout
  (fixed f4c70bf, test `test_run_record_keeps_log_format_for_the_monitor`). A new
  field that only start-up reads (like `env_allow`) does not need to.
- **Run-tier tests scale minutes and hours separately.** The `fast` fixture sets
  `RUN_MINUTE_S=0.001` and `RUN_HOUR_S=60`. Hours used to shrink with minutes,
  so the default `max_hours` was 0.48 s and a stalled start ended `gave_up`:
  that was the `test_run_args_reach_script_literally` flake (it runs first).
  Overdue tests need `expect_minutes` far enough past the script's first
  output (100 = 0.1 s was flaky; use 400 with a 1 s script).
- **`env_file` is sourced after a run's `env`, so it can override it.** If NQC's
  `Library/lane.env` sets `ROWS` or `TROWS`, the caller's value is lost. The
  child env is built first, then bash sources the file, then execs.
- **NQC's `quiet` filters the console the run tier watches.** The original awk in
  `Tools/playtest/lib/verdict.sh` kept verdict lines, `POLLGUARD`, `log:`,
  `wait_until:`, `RESTARTED` and `start.sh: REFUSED|BLOCKED|lock wait timed out`
  only, so a run.json flag on anything else (NQC's `disk_full`, "No space left on
  device") never fired for output inside a quieted command. NQC reports the fix
  is in lane A (`0d1bece8`) and lane B (`d5d94442` / `0a1bc68a`) as of 2026-11-09;
  a lane or branch without it still loses those flags.
- **NQC logs before 2026-11-03 06:39 have no `@case` stamps.** Verdict lines
  pad names to 34 columns, so a longer name runs into its detail and can only
  be found by its exact name; `verdict_summary` reprints every row after
  `--- verdicts ---`, so count inline lines only. `_verdict_rows` handles both.
- **Subagent implementers can stop without committing.** The nim-edit Task 14
  implementer left ~860 lines uncommitted in its worktree; check `git status`
  in every worktree before trusting a ledger's last line.
- **Shell on this Mac: BSD `sed`, heredocs and no `timeout`.** `sed -E 's/\bX\b/Y/'`
  silently matches nothing because BSD `sed` ignores `\b`; use `perl -pi -e`. In
  `a && cat >f <<'EOF' ... EOF` followed by `git commit`, the commit line is a new
  command and runs even when `a` failed; put heredocs in their own call or end
  the chain before them. `timeout 60 cmd` fails with "command not found" and the
  rest of the call can still run; use `asyncio.timeout` in a Python check, or
  `perl -e 'alarm N; exec @ARGV'`.
- **A `global` rebind only reaches its own module.** A `from x import name` copy in
  another module keeps the old object. In the split NIM code `_nim_slots`,
  `_nim_rotation` and `_nim_http` are rebound only in `limits.py` / `transport.py`
  (`_reset_limits`, `_reset_client`); shared dicts are cleared in place. Keep every
  rebinding function in the module that defines the name.
- **A venv keeps dekko after the registration stops asking for it.** `uv run` syncs
  inexactly, so losing `--extra dekko` changes nothing until the venv is rebuilt;
  then the agent's call-graph tools and `must_cover` vanish with no error. Castor
  only looks beside the server's Python (`_dekko_bin`), so a `uv tool` dekko on
  PATH does not count. Check with `nim_status`.
- **`/save` inside a worktree misses the real memory.** `current_state.md` and
  `session_log.md` are git-ignored, so a worktree has neither and the script reports
  "never saved", and its `gotchas.md` is only the last committed copy. Save to the
  main checkout's `.memories/`.
