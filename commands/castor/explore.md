Perform a full codebase exploration using Antigravity (agy) in agent mode.

Load the tool schema before calling — MCP tools are deferred and cannot be called until hydrated:

    ToolSearch("select:mcp__claude-castor__gemini_prompt")

Read `~/.claude/GEMINI.md` first for prompting guidance, then call `gemini_prompt` with:
- `trust=True` (full agent mode — agy navigates the filesystem on its own)
- `cwd` set to the absolute path of the current working directory
- No `files` or `directory` — agy will explore independently
- A prompt that includes:
  - The current working directory path
  - What the user is trying to understand or accomplish (use the argument if provided: $ARGUMENTS)
  - A request for: key files and their roles, how the system works end-to-end, notable patterns or dependencies, and anything surprising or worth flagging

After agy responds, summarize the findings and highlight anything directly relevant to the user's current task.
