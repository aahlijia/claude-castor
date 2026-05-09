# Claude Castor

A Gemini MCP for Claude Code. Offloads large-context work — file exploration, indexing,
summarization, deep research — to Gemini via the free `@google/gemini-cli`.
No API key required. Uses Google OAuth (free tier, Gemini 2.5).

---

## Setup

### 1. Install the Gemini CLI

```bash
npm install -g @google/gemini-cli
```

### 2. Authenticate

Open a terminal (outside Claude Code) and run:

```bash
gemini
```

Follow the Google OAuth prompt in your browser. Exit after authentication completes
(`/exit` or Ctrl+C). This is a one-time step.

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

Sends a prompt to Gemini and returns the response. Claude constructs the prompt —
see `GEMINI.md` for the prompting guide.

| Parameter | Type | Description |
|---|---|---|
| `prompt` | `str` | The fully-formed prompt (task context + ask) |
| `files` | `list[str] \| None` | Absolute file paths to inline (user-provided only) |
| `directory` | `str \| None` | Directory to inline (user-provided only) |
| `raw` | `bool` | Skip structured response instructions (default: false) |
| `trust` | `bool` | Enable full agent mode — Gemini can explore the filesystem (default: false) |

### `gemini_status`

Checks CLI installation and authentication. Returns `READY` or instructions to fix.

### `gemini_setup`

Returns step-by-step instructions for completing OAuth and enabling agent mode.
Claude calls this automatically when setup is needed.

---

## How It Works

1. Claude recognizes an opportunity to offload work to Gemini
2. Claude reads `GEMINI.md` to construct a context-rich prompt
3. Claude calls `gemini_prompt` with the assembled prompt
4. The MCP server prepends a system instruction and inlines any files
5. The assembled prompt is passed to the `gemini` CLI as a subprocess
6. Gemini's response comes back as the tool result
7. Claude uses the response as research context to continue the task

**Agent mode** (`trust=True`): Gemini runs with `GEMINI_CLI_TRUST_WORKSPACE=true`,
giving it full filesystem access to actively explore the project on its own — no
pre-loading required. Best for deep research and large codebase exploration.

---

## Troubleshooting

**`gemini` not found**
Run `npm install -g @google/gemini-cli`, then verify with `gemini --version`.

**Auth failed**
Open a terminal and run `gemini` to complete the Google OAuth flow in your browser.

**Timeout**
Gemini has a 300s timeout per call. For large directories in agent mode (`trust=True`),
Gemini explores on its own — prefer that over pre-loading the directory.

**Garbled or empty response**
Try `raw=True` to see unfiltered output, which can help diagnose prompt issues.

---

## Phase 2 (Not Yet Implemented)

See `PLAN.md` for the full Phase 2 roadmap. Key items:

- Persistent chat sessions with `gemini_reset` tool
- Streaming output (async subprocess)
- Directory structure map when content is truncated
- Enhanced `gemini_status` diagnostics (workspace size, trust state, model version)
