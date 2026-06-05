# Claude Castor

An Antigravity MCP for Claude Code. Offloads large-context work — file exploration,
indexing, summarization, deep research — to Google's free Antigravity CLI (`agy`),
the terminal agent powered by Gemini models. No API key required. Uses Google
sign-in (free individual tier).

> Antigravity CLI replaces the retired Gemini CLI (shut down June 18, 2026). The
> MCP tools keep their `gemini_*` names for compatibility but drive `agy`.

---

## Setup

### 1. Install the Antigravity CLI

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash
```

Verify with `agy --version`.

### 2. Sign in

Call the `gemini_auth` tool (it opens the Google sign-in URL in the browser), or
open a terminal (outside Claude Code) and run:

```bash
agy -p "ok"
```

Complete the Google consent in your browser. One-time step — the token persists in
the system keyring.

### 3. Install Python dependencies

```bash
pip install fastmcp
# or, if using uv:
uv add fastmcp
```

### 4. Register with Claude Code

Run this once to register the MCP server (replace the path with the actual absolute path):

```bash
claude mcp add -s user claude-castor /absolute/path/to/uv -- run --directory /absolute/path/to/claude-castor server.py
```

To find your `uv` path: `which uv`

### 5. Verify

Restart Claude Code, then ask Claude to run `gemini_status`. It should return `READY`.

---

## Tools

### `gemini_prompt`

Sends a prompt to Antigravity (`agy`) and returns the response. Claude constructs
the prompt — see `GEMINI.md` for the prompting guide.

| Parameter | Type | Description |
|---|---|---|
| `prompt` | `str` | The fully-formed prompt (task context + ask) |
| `files` | `list[str] \| None` | Absolute file paths to inline (user-provided only) |
| `directory` | `str \| None` | Directory to inline (user-provided only) |
| `raw` | `bool` | Skip structured response instructions (default: false) |
| `trust` | `bool` | Full agent mode — `--dangerously-skip-permissions`; requires `cwd` (default: false) |
| `cwd` | `str \| None` | Project root for agy's workspace (required when `trust=True`) |
| `add_dirs` | `list[str] \| None` | Extra dirs to grant agy read access (`--add-dir`) |
| `continue_session` | `bool` | Resume agy's prior conversation instead of re-exploring (default: false) |
| `conversation_id` | `str \| None` | Resume a specific agy conversation by ID (takes precedence over `continue_session`) |
| `model` | `str \| None` | Select the agy model (see `gemini_models`; defaults to agy's default) |

### `gemini_reset`

Forgets the current Antigravity session so the next `gemini_prompt` with
`continue_session=True` starts a fresh conversation. Use at task boundaries.

### `gemini_models`

Lists the models available to agy — the names you can pass as `model` to
`gemini_prompt`. Requires sign-in.

### `gemini_auth`

Returns instructions for the user to sign in (run `! agy -p "ok"` in the Claude
Code prompt). An MCP tool call blocks Claude, so it cannot drive the interactive
browser flow itself. Call when `gemini_status` reports NOT SIGNED IN.

### `gemini_status`

Checks CLI installation and sign-in. Returns `READY` or instructions to fix.

### `gemini_setup`

Returns step-by-step setup instructions. Claude calls this when setup is needed.

---

## How It Works

1. Claude recognizes an opportunity to offload work to Antigravity
2. Claude reads `GEMINI.md` to construct a context-rich prompt
3. Claude calls `gemini_prompt` with the assembled prompt
4. The MCP server prepends a system instruction and inlines any files
5. The prompt is piped via stdin to `agy --print` as a subprocess
6. agy's response comes back as the tool result
7. Claude uses the response as research context to continue the task

**Agent mode** (`trust=True`): agy runs with `--dangerously-skip-permissions`,
rooted at `cwd`, giving it full filesystem access to actively explore the project
on its own — no pre-loading required. Best for deep research and large codebases.

---

## Troubleshooting

**`agy` not found**
Run `curl -fsSL https://antigravity.google/cli/install.sh | bash`, then verify with
`agy --version`.

**Not signed in**
Call the `gemini_auth` tool, or run `agy -p "ok"` in a terminal to complete Google
sign-in.

**Timeout**
agy print mode has a 300s timeout per call. For large directories in agent mode
(`trust=True`), agy explores on its own — prefer that over pre-loading the directory.

**Garbled or empty response**
Try `raw=True` to see unfiltered output, which can help diagnose prompt issues.

---

## Phase 2

See `.docs/development-plan.md` for the full roadmap. Key items:

- Persistent chat sessions with `gemini_reset` tool (agy `--continue`) — **done**
- Model selection (`model` param + `gemini_models` tool) — **done**
- Sandbox tier (`--sandbox`) between read-only and full trust
- Directory structure map when content is truncated
- Enhanced `gemini_status` diagnostics (account, model version)
- Streaming output (async subprocess)
