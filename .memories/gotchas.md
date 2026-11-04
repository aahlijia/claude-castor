# Gotchas

- **agy auto-updates and has no version pin; its CLI has broken Castor twice
  (1.1.9, 1.3+).** Run `python3 scripts/check_agy_surface.py` before releases
  and re-test the sandbox path live after any agy update.
- **`agy --print` takes the prompt inline in argv, with no stdin fallback**
  (since 1.1.9; 1.0.5 read stdin). Keep the prompt right after `--print` and
  pass `--print-timeout=<t>` as one `=`-joined token or the parser swallows it
  as the prompt.
- **agy's workspace comes from `--add-dir`, not the subprocess cwd.** Without
  `--add-dir <cwd>` agy explores its own scratch directory.
- **agy 1.3+ headless print mode auto-denies shell commands outside
  `--dangerously-skip-permissions`, and exits 0 with empty stdout.** The only
  traces are a stderr line containing "auto-denied" (text mode) or
  `denied_actions` with `response: ""` in the stream-json result event.
  `_empty_output_error` handles both; don't trust the exit code alone.
- **Allow-rules in `~/.gemini/antigravity-cli/settings.json` did not take
  effect** in agy 1.4.7 print mode (`command(ls *)` still denied `ls -la`).
- **`--sandbox` blocks writes to the workspace only.** Writes to `~` and
  `/private/tmp`, and network access, still succeed (agy 1.4.7, macOS).
- **Without shell commands agy can't enumerate files.** It can `read_file` a
  named path, but searching falls back to subagents and fails.
- **stream-json result layout:** discriminator is `event["event"] ==
  "result"` (not `type`), payload at `event["result"]["structured_output"]`,
  with the same JSON as text in `response`. Unchanged in 1.4.7.
  `--json-schema` needs a root object; a root array fails with no
  structured_output (semantic search wraps hits under `"hits"`).
- **Anything not bracketed gets cached.** A run that returns non-`[` text is
  cached by `_dispatch`, so failure modes must return bracketed strings. Bump
  `CACHE_KEY_VERSION` when a fix means old entries may be bad.
- **Never call `agy changelog` from a polled path**; it mutates a disk cache
  on every call.
- **Every MCP subprocess must pass `stdin=subprocess.DEVNULL`**; inherited
  stdin is Claude Code's JSON-RPC pipe.
- **Each Claude Code session spawns its own castor server process**
  (`uv run --directory <repo> server.py`). Restart the session to pick up
  `server.py` edits; no reinstall needed. It runs whatever branch this repo
  has checked out, so switching branches here changes the tools every other
  project sees (NQC's overnight run uses the NIM roles). Keep `develop` checked
  out unless a session is testing a branch on purpose.
- **Heavy live testing hits the Antigravity individual quota**
  ("Individual quota reached"); keep probes small.
- **`nim_models` lists models the account cannot call.** Kimi K2.6 and Nemotron 3
  Super are in the catalog but return 404 "Function … not found for account";
  Gemma 4 31B and DeepSeek V4.1 Flash timed out. Probe a model with a live call
  before presenting it as an option.
- **Reasoning models need a real `max_tokens`.** Nemotron 3 Ultra spends tiny
  budgets thinking and returns no content (`finish_reason=length`). 8 tokens made
  `nim_status` say NOT READY on a good key; verify uses 512.
- **Slash commands are copies, not served from the repo.** The CLI reads
  `~/.claude/commands/castor/`; after changing `commands/castor/*.md` copy them
  over (`cp commands/castor/*.md ~/.claude/commands/castor/`).
- **A re-registered MCP server needs a Claude Code restart**, and `claude mcp add
  ... -e KEY=... -- /path/to/uv ...` needs a space after `--` (`--/opt/...` fails
  after the remove already ran, leaving castor unregistered).
- **The citation check proves a line exists, not that the claim is true.** The
  scout named a function that does not exist at a real line, and review findings
  of absence ("no tests", "not documented") were often wrong. Verify before
  acting. Paths quoted from a log are tagged `[unverified]` (false positive).
  A passing count is no proof either: a review reported "citations verified
  9/9" while one cited range was the wrong branch of the function.
- **NIM probe scripts read keys from `~/.claude.json`** (`mcpServers.claude-castor.env`).
  Never print them; keys pasted into a chat stay in that transcript.
- **Nemotron keeps calling tools while the history holds tool calls.** Omitting
  `tools` and sending `tool_choice: "none"` both still returned a tool call and
  no content (3/3 replays), so the agent's last step failed with "hit its step
  cap". `_agent_answer_messages` now flattens the transcript into system + one
  user message for the final request (6/6 replays answered).
- **Nemotron reasoning has no default bound.** A step can think for 4k+ tokens
  and return nothing (`finish_reason=length`) or outrun the 180 s timeout. NIM
  honors `reasoning_budget` (and `nvext.max_thinking_tokens`) exactly;
  `reasoning_effort` does nothing; `chat_template_kwargs.enable_thinking=False`
  turns it off.
- **Nemotron review findings repeat the same false beliefs**: that braces in a
  value passed to `str.format` raise KeyError, and that taking a lock twice in
  sequence deadlocks, and that a `..HEAD` rev range is rejected. Expect them
  and discard them.
- **The NIM test fixture blocks subprocesses.** `no_agy` in `tests/test_nim.py`
  stubs `subprocess.run` so agy never runs; only `["git", ...]` passes through
  to the real one (the agent's file map needs `git ls-files`). A new helper
  that shells out to anything else fails in tests until the fixture allows it.
- **Eval scoring by line proximity overstates hits.** Matching leads to known
  findings by file and line alone credited the old reviewer with 9 of 10
  must-catches; the hand scoring said 0 of 4 on NQC. Each finding needs
  keywords for its substance, and loose ones still slip (`empty` matched
  "non-empty"). Spot-check hits before trusting a score; re-score with
  `--rescore` after editing cases.
- **Single eval runs are noisy.** One run per model put 3.5 Super at twice
  Ultra's precision; three runs made them equal. Repeat NQC cases at least
  twice before changing a default.
- **Ultra spends every tool step it is given,** whatever the prompt says (19 of
  19 steps at about 20 s each with the context already supplied). Cap steps in
  code; prompt limits are ignored.
- **The agent's answer request can outrun 180 s** (6,144 thinking tokens plus
  the review at about 60 tokens/s); it has its own 360 s timeout. Each timeout
  is retried once, so a stuck request costs about 6 minutes.
- **Agent grep reads the git index, cached 60 s per root.** Fixed 2026-11-02
  (it walked Unity's `Library/`, 20 s for review context). A file created
  mid-run is missed until the cache expires or `_nim_reset_state()`; tests
  that add files after a first grep must reset. Outside git it still walks
  (pruning `NIM_AGENT_SKIP_DIRS`).
- **NIM summaries over ~35k tokens of input cover only the start.** A 216k-char
  part of DECISIONS.md produced 15 points from its first third and none of
  the C-rulings after it; at 120k-char parts every part was covered. The merge
  also packed all bullets into one newline-joined string once; `_tidy_summary`
  splits them.
- **NIM speed swings by the hour.** 3.5 Super ran 127 tokens/s at 19:00 and 29
  at 21:00 on 2026-11-02 (Ultra 35 to 59). Never compare models on one hour's
  timings; check `nim_usage` or the usage log by hour.
- **Each Nemotron request spends its full reasoning budget.** At ~55 tokens/s
  a 1,024-token step budget is ~18 s before any output, so a 2-request triage
  cannot finish in 15 s. A tool step that answers without tools can also run
  into the 4,096 cap; its partial text is dropped and the answer step starts
  over (one triage run took 154 s that way).
- **`git apply` reads diff header paths from the repo top level.** A diff
  built with cwd-relative paths from a subdirectory passes `--check` and then
  skips the file or patches a same-named file at the top. `_edit_make_patch`
  computes the `--show-prefix` itself; never build a patch another way.
- **dekko error text can leak denied files.** An ambiguous name exits 4 and
  lists every candidate on stderr (incl. `.env*` files) even with `--json`;
  all dekko output, errors included, goes through `_dekko_scrub`.
- **This session's worktree guard refuses** git aimed outside the worktree
  and some command lines containing the word "eval" (e.g. grep on
  `scripts/eval/...`). Use the Read tool for those files and a launcher
  script (`scratchpad/run_eval.py`) to pass NIM keys to the eval.
- **The eval's `dekko=on|off` flips agent tools and review context
  together.** Isolate a review-context change by comparing dekko-on runs of
  two server files (`--server`) in the same window.
- **`must_cover` on a module constant always refuses.** dekko does not
  index module-level constants, so there is no definition or call site to
  require. Use `must_cover` for functions, methods and classes; for a
  constant, scope with `files` and check the diff by eye.
