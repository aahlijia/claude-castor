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
  `server.py` edits; no reinstall needed.
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
- **NIM probe scripts read keys from `~/.claude.json`** (`mcpServers.claude-castor.env`).
  Never print them; keys pasted into a chat stay in that transcript.
