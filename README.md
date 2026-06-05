# Claude Castor

> An Antigravity MCP for Claude Code.

Offloads large-context work — file exploration, indexing, summarization, deep
research — to Google's free [Antigravity CLI](https://antigravity.google)
(`agy`), the terminal coding agent powered by Gemini models. Claude
orchestrates and reasons; Antigravity handles the heavy lifting.

No API key required. Uses Google sign-in (free individual tier).

> **Note:** Antigravity CLI replaces the retired Gemini CLI (shut down
> June 18, 2026). The MCP tools keep their `gemini_*` names for
> compatibility, but they now drive the `agy` binary.

---

## How It Works

1. Claude recognizes a task that would benefit from a large context window
2. Claude constructs a context-rich prompt describing the task and what it needs
3. Claude calls `gemini_prompt` via the MCP bridge
4. The server pipes the prompt to the `agy --print` CLI as a subprocess
5. agy's response comes back as a tool result Claude uses to continue the task

In **agent mode** (`trust=True`), agy auto-approves tool actions
(`--dangerously-skip-permissions`) and, rooted at `cwd`, actively explores the
project on its own — navigating files, grepping for patterns, following
imports — rather than working from pre-loaded context.

---

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- [Claude Code](https://claude.ai/code)

---

## Installation

### 1. Install the Antigravity CLI

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash
```

Verify with `agy --version`.

### 2. Sign in with Google

The easiest path is to call the `gemini_auth` tool from Claude (it opens the
sign-in URL in your browser). To do it manually, open a terminal and run:

```bash
agy -p "ok"
```

Complete the Google consent in your browser. One-time step — the token is
stored in the system keyring.

### 3. Clone the repo

```bash
git clone https://github.com/yourusername/claude-castor
cd claude-castor
```

### 4. Run the installer

```bash
python install.py
```

This registers the MCP server with Claude Code and installs the slash commands
globally. Works on macOS, Linux, and Windows.

### 5. Restart Claude Code

Then run `/castor-status` to confirm everything is wired up.

---

## Claude Desktop App

To use Claude Castor in the Claude desktop app, edit the config file at
`~/Library/Application Support/Claude/claude_desktop_config.json` and add an
entry under `mcpServers`:

```json
{
  "mcpServers": {
    "claude-castor": {
      "command": "/absolute/path/to/uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/claude-castor",
        "server.py"
      ]
    }
  }
}
```

Get your `uv` path with `which uv`. Restart the desktop app after saving.

If you already signed in to Antigravity for Claude Code, authentication carries
over via the system keyring — no extra steps needed.

---

## Slash Commands

| Command | Description |
|---|---|
| `/castor-status` | Check Antigravity CLI installation and sign-in |
| `/castor-explore` | Full codebase exploration in agent mode |
| `/castor-research <topic>` | Deep-dive on a symbol, feature, or file |

---

## MCP Tools

| Tool | Description |
|---|---|
| `gemini_prompt` | Send a prompt to Antigravity and get a response |
| `gemini_reset` | Forget the current session so the next prompt starts fresh |
| `gemini_models` | List the models available to agy |
| `gemini_auth` | Get sign-in instructions when not authenticated |
| `gemini_status` | Check CLI installation and sign-in |
| `gemini_setup` | Get step-by-step setup instructions |

### `gemini_prompt` parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `prompt` | `str` | required | The prompt Claude constructs |
| `files` | `list[str]` | `None` | File paths to inline (user-provided only) |
| `directory` | `str` | `None` | Directory to inline (user-provided only) |
| `raw` | `bool` | `false` | Skip structured response instructions |
| `trust` | `bool` | `false` | Enable full agent mode (requires `cwd`) |
| `cwd` | `str` | `None` | Project root for agy's workspace |
| `add_dirs` | `list[str]` | `None` | Extra dirs to grant agy read access (`--add-dir`) |
| `continue_session` | `bool` | `false` | Resume agy's prior conversation instead of re-exploring |
| `conversation_id` | `str` | `None` | Resume a specific agy conversation by ID |
| `model` | `str` | `None` | Select the agy model (see `gemini_models`) |
| `sandbox` | `bool` | `false` | Explore under terminal restrictions (safe middle tier; ignored if `trust`) |

---

## Agent Mode

By default, agy runs read-only and responds in Q&A mode. With `trust=True`
(which requires `cwd`), agy runs with `--dangerously-skip-permissions` — it can
read files, search the codebase, follow imports, and auto-approve tool actions
on its own without needing content pre-loaded.

`sandbox=True` is a safer middle tier: agy still explores the filesystem
actively, but under terminal restrictions and without blindly auto-approving
actions. `trust` takes precedence if both are set. Prefer `sandbox` unless the
task genuinely needs agy to write files or run arbitrary commands.

Best for (trust or sandbox):
- Deep codebase exploration
- Cross-file symbol resolution
- Large projects where pre-loading is impractical

agy is rooted at the `cwd` you pass; grant access to additional directories
with `add_dirs` (mapped to repeated `--add-dir` flags).

---

## Troubleshooting

**`agy` not found** — Run
`curl -fsSL https://antigravity.google/cli/install.sh | bash`

**Not signed in** — Call the `gemini_auth` tool, or run `agy -p "ok"` in a
terminal to complete Google sign-in

**Timeout** — agy print mode has a 300s limit. For large projects, prefer
agent mode (`trust=True`) over pre-loading a full directory

**Empty response** — Try `raw=True` to see unfiltered output

---

## Roadmap

Phase 1 (current) is stateless — each call is independent. See `PLAN.md` for
the full Phase 2 roadmap:

- Persistent sessions with `gemini_reset`
- Streaming output
- Directory structure map on truncation
- Enhanced diagnostics
