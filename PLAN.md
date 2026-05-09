# Claude Castor — Development Plan

## Overview

An MCP server that lets Claude offload large-context work (file exploration, indexing,
summarization, research) to Gemini via the `gemini` CLI. Claude orchestrates and reasons;
Gemini handles the heavy lifting. Phase 1 is stateless — no persistent session.

---

## Goals

- Claude can call Gemini on demand via MCP tools
- Claude constructs thoughtful, context-rich prompts (not raw user input)
- No Google SDK required — uses the free `@google/gemini-cli` (Gemini 2.5 models)
- Files/directories are only passed when the user explicitly provides them
- Responses are structured and useful by default; raw mode available

---

## Non-Goals (Phase 1)

- Persistent Gemini chat sessions (Phase 2)
- Automatic filesystem crawling by Claude on behalf of the user
- Streaming responses
- Multi-turn Gemini conversations

---

## Project Structure

```
claude-gemini-agents/
├── server.py          # FastMCP server — all tool definitions and logic
├── GEMINI.md          # Instructions for Claude: how to prompt Gemini well
├── CLAUDE.md          # Setup guide + tool reference for Claude Code users
├── PLAN.md            # This file
└── requirements.txt   # Python dependencies (fastmcp only)
```

---

## Tech Stack

| Concern | Choice | Reason |
|---|---|---|
| MCP server | FastMCP (Python) | Minimal boilerplate, clean tool definitions |
| Gemini access | `gemini` CLI subprocess | Free tier, Gemini 2.5 models, no API key |
| File reading | MCP server (Python) | Simpler than CLI file args, more portable |
| HTTP | None | No REST API calls needed |

---

## MCP Tools

### `gemini_prompt`

The primary tool. Claude calls this with a fully-formed prompt it has constructed.

**Parameters:**
- `prompt: str` — the complete prompt Claude has constructed (task context + ask)
- `files: list[str] | None` — absolute file paths to inline into the prompt (only if user provided them)
- `directory: str | None` — a directory path to read and inline (only if user provided it)
- `raw: bool` — if `True`, skip Gemini's structured response instructions; return output as-is

**Behavior:**
1. If `files` provided: read each file, append content blocks to the prompt
2. If `directory` provided: read all files in directory (respecting `.gitignore` if present), append
3. Prepend a system instruction block to the prompt (technical, structured output)
4. Shell out: `gemini -p "<assembled prompt>"`
5. Return stdout as the tool result; surface stderr as an error if non-empty

**Returns:** Gemini's response as a string. Claude treats this as research context.

---

### `gemini_status`

Checks that the `gemini` CLI is installed and authenticated.

**Parameters:** None

**Behavior:**
1. Run `gemini --version` — confirm CLI is present
2. Run a minimal test prompt — confirm auth is working

**Returns:** Status string (ready / not installed / not authenticated + fix instructions)

---

## File Inlining Strategy

When files or a directory are passed:

```
[FILE: src/server.py]
<contents>

[FILE: src/utils.py]
<contents>
```

Each file is wrapped in a labeled block so Gemini can reference them by name.

**Directory reads:**
- Max file size per file: 100KB (skip larger, note in prompt)
- Skip binary files (check for null bytes)
- Skip common noise: `node_modules/`, `.git/`, `__pycache__/`, `*.lock`, `*.log`
- If total context would exceed ~800KB, truncate with a note

---

## System Instruction Block

Prepended to every `gemini_prompt` call (unless `raw=True`):

```
You are a technical assistant. Respond with precision and structure.
- Use clear headings and bullet points where appropriate
- Include specific file names, line numbers, and symbol names when referencing code
- Be concise — avoid filler; every sentence should carry information
- If asked to index or summarize, produce output Claude can use as reference context
```

This is embedded in `server.py` — not something Claude needs to manage.

---

## `GEMINI.md` — Prompting Guide for Claude

This file lives in the project root and gives Claude standing instructions on how to
use the Gemini tools well. Key sections:

1. **When to use Gemini** — large files, cross-file exploration, summarization, anything
   that would consume significant Claude context
2. **How to construct the prompt** — always lead with 2-3 sentences of task context
   (what you're trying to accomplish, where you are in the task), then the specific ask
3. **File/directory guidance** — only pass paths the user explicitly mentioned; do not
   crawl the filesystem to find "relevant" files
4. **Interpreting results** — treat as research context, not ground truth; verify
   critical details (line numbers, function names) before acting on them
5. **`raw=True` usage** — only when the user explicitly asks for unfiltered output

### Prompt construction template Claude should follow:

```
Context: [1-2 sentences on current task and step]
Goal: [what you need from Gemini — be specific]
[file blocks if applicable]
[the actual question/request]
```

---

## `CLAUDE.md` — Setup & Reference

### Prerequisites
- Python 3.10+
- Node.js (for `gemini` CLI)
- `@google/gemini-cli` installed and authenticated

### Setup Steps
1. `npm install -g @google/gemini-cli`
2. `gemini` — run once interactively to complete Google OAuth
3. `pip install fastmcp` (or `uv add fastmcp`)
4. Add to Claude Code MCP config (`.claude/settings.json` or global):
   ```json
   {
     "mcpServers": {
       "gemini": {
         "command": "python",
         "args": ["/path/to/claude-gemini-agents/server.py"]
       }
     }
   }
   ```

### Tool Reference
| Tool | When to use |
|---|---|
| `gemini_prompt` | Offload any large-context or exploratory work to Gemini |
| `gemini_status` | Check CLI is installed and authenticated before first use |

---

## Build Order

1. **`server.py`** — FastMCP server with `gemini_prompt` and `gemini_status`
2. **`requirements.txt`** — `fastmcp` only
3. **`GEMINI.md`** — Claude's prompting guide
4. **`CLAUDE.md`** — setup and tool reference
5. **Manual test** — call `gemini_status`, then `gemini_prompt` with a simple ask, then with a file

---

## Phase 1 — Delivered

- `gemini_prompt` with file/directory inlining, `.gitignore` support, size guards
- `gemini_status` health check
- `gemini_setup` OAuth + trust instructions
- Safe headless mode (`--skip-trust`) as default
- Full agent mode via `GEMINI_CLI_TRUST_WORKSPACE=true` (`trust=True` param)
- System instruction block for structured responses
- `GEMINI.md` prompting guide for Claude
- `CLAUDE.md` setup guide for users

---

## Phase 2 Plan

### Goals

- Persistent Gemini sessions so context accumulates across calls within a task
- Streaming output so Claude sees results incrementally rather than waiting 300s
- Smarter directory inlining (structure map when content is truncated)
- Enhanced diagnostics in `gemini_status`

---

### Feature 1: Persistent Sessions + `gemini_reset`

**Problem:** Every `gemini_prompt` call is a fresh subprocess. Gemini has no memory of
prior calls in the same task — Claude must re-explain context every time.

**Design:**

- The MCP server holds a module-level session object: a long-running `gemini` subprocess
  with stdin/stdout pipes kept open between calls
- `gemini_prompt` writes to the session's stdin and reads the response from stdout
- A sentinel token (e.g. `---END---`) marks the end of each response so the server knows
  when to stop reading
- `gemini_reset` kills the subprocess and starts a fresh one, optionally accepting a
  summary string to seed the new session with prior context

**New tools:**
- `gemini_reset(summary: str | None)` — resets the session; if `summary` provided,
  sends it as the first message to seed context

**Tradeoffs:**
- Long-running subprocesses can die unexpectedly — needs auto-restart on broken pipe
- Sentinel approach is fragile if Gemini ever outputs the sentinel string in a response;
  may need a more robust delimiter strategy
- Session state is per-server-process — restarting Claude Code kills it

---

### Feature 2: Streaming Output

**Problem:** Large agentic tasks can take 60–300s with no feedback. Claude and the user
see nothing until the full response arrives.

**Design:**

- Switch `_run_gemini` from `subprocess.run` (blocking) to `asyncio.create_subprocess_exec`
  with async stdout streaming
- FastMCP supports async tool functions — `gemini_prompt` becomes `async def`
- Stream lines back to Claude as they arrive via an async generator or by accumulating
  into a buffer and yielding chunks
- Timeout becomes a per-chunk idle timeout rather than a total wall-clock timeout

**Tradeoffs:**
- Requires moving from sync to async throughout the server — moderate refactor
- FastMCP streaming support needs verification against the MCP protocol spec; not all
  clients handle streamed tool responses identically
- Persistent sessions (Feature 1) and streaming interact — both need async subprocess
  handling, so these should be built together

---

### Feature 3: Directory Structure Map on Truncation

**Problem:** When `_inline_directory` hits `MAX_TOTAL_BYTES`, later files are silently
skipped. Gemini gets a note saying N files were skipped but no idea what they are.

**Design:**

- Before inlining any file content, build a full directory tree listing (paths + sizes)
- Always include the tree at the top of the inlined context, regardless of size limits
- If a file is skipped due to size, it still appears in the tree with a `[skipped]` marker
- This gives Gemini a complete map of the project even when content is truncated,
  so in agent mode (`trust=True`) it can fetch skipped files itself

**Implementation:** Small change to `_inline_directory` — collect the tree first,
then stream file content until the budget is exhausted.

---

### Feature 4: Enhanced `gemini_status` Diagnostics

**Problem:** `gemini_status` only checks install + auth. It doesn't report whether
agent mode (`trust=True`) will work, or how large the current working directory is.

**Design:**

Add to the `READY` response:
- Whether `GEMINI_CLI_TRUST_WORKSPACE` is recognized (quick env-var test call)
- Approximate token count of the working directory (file count + total bytes) so
  Claude knows whether inlining it is practical
- Gemini model version in use (parsed from a `--version` or test call response)

---

### Build Order (Phase 2)

1. **Feature 3** (directory structure map) — pure Python, no async, easiest win
2. **Feature 4** (enhanced diagnostics) — small addition to existing tool
3. **Feature 1 + 2** (persistent sessions + streaming) — async refactor, build together
