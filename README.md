# Claude Castor

> A Gemini MCP for Claude Code.

Offloads large-context work — file exploration, indexing, summarization, deep
research — to Gemini via the free `@google/gemini-cli`. Claude orchestrates and
reasons; Gemini handles the heavy lifting.

No API key required. Uses Google OAuth (free tier, Gemini 2.5 models).

---

## How It Works

1. Claude recognizes a task that would benefit from Gemini's large context window
2. Claude constructs a context-rich prompt describing the task and what it needs
3. Claude calls `gemini_prompt` via the MCP bridge
4. The server passes the prompt to the `gemini` CLI as a subprocess
5. Gemini's response comes back as a tool result Claude uses to continue the task

In **agent mode** (`trust=True`), Gemini gets full filesystem access and actively
explores the project on its own — navigating files, grepping for patterns,
following imports — rather than working from pre-loaded context.

---

## Prerequisites

- Python 3.10+
- Node.js
- [uv](https://docs.astral.sh/uv/)
- [Claude Code](https://claude.ai/code)

---

## Installation

### 1. Install the Gemini CLI

```bash
npm install -g @google/gemini-cli
```

### 2. Authenticate with Google

Open a terminal and run:

```bash
gemini
```

Follow the Google OAuth prompt in your browser. One-time step — exit when done.

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

## Slash Commands

| Command | Description |
|---|---|
| `/castor-status` | Check Gemini CLI installation and auth |
| `/castor-explore` | Full codebase exploration in agent mode |
| `/castor-research <topic>` | Deep-dive on a symbol, feature, or file |

---

## MCP Tools

| Tool | Description |
|---|---|
| `gemini_prompt` | Send a prompt to Gemini and get a response |
| `gemini_status` | Check CLI installation and auth |
| `gemini_setup` | Get step-by-step OAuth instructions |

### `gemini_prompt` parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `prompt` | `str` | required | The prompt Claude constructs |
| `files` | `list[str]` | `None` | File paths to inline (user-provided only) |
| `directory` | `str` | `None` | Directory to inline (user-provided only) |
| `raw` | `bool` | `false` | Skip structured response instructions |
| `trust` | `bool` | `false` | Enable full agent mode with filesystem access |

---

## Agent Mode

By default, Claude pre-loads file content and Gemini responds in Q&A mode. With
`trust=True`, Gemini runs in full agent mode — it can read files, search the
codebase, and follow imports on its own without needing content pre-loaded.

Best for:
- Deep codebase exploration
- Cross-file symbol resolution
- Large projects where pre-loading is impractical

No interactive directory trust step is required. The server handles workspace
trust automatically via `GEMINI_CLI_TRUST_WORKSPACE`.

---

## Troubleshooting

**`gemini` not found** — Run `npm install -g @google/gemini-cli`

**Auth failed** — Open a terminal and run `gemini` to complete Google OAuth

**Timeout** — Gemini has a 300s limit. For large projects, prefer agent mode
(`trust=True`) over pre-loading a full directory

**Empty response** — Try `raw=True` to see unfiltered output

---

## Roadmap

Phase 1 (current) is stateless — each call is independent. See `PLAN.md` for
the full Phase 2 roadmap:

- Persistent sessions with `gemini_reset`
- Streaming output
- Directory structure map on truncation
- Enhanced diagnostics
