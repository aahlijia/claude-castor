Produce a structured map of the current codebase using Antigravity (agy).

Load the tool schema before calling — MCP tools are deferred and cannot be called until hydrated:

    ToolSearch("select:mcp__claude-castor__gemini_index")

Read `~/.claude/GEMINI.md` first for context, then call `gemini_index` with:
- `cwd` set to the absolute path of the current working directory

`gemini_index` runs agy as a sandboxed explorer and returns a compact index: the directory layout with one-line roles, entry points, key modules and their main symbols, and how the major pieces connect. In a git repo the result is cached against the repo state, so re-running it later is instant.

After agy responds, present the index and highlight the parts most relevant to what the user is trying to do (use the argument if provided: $ARGUMENTS). Treat this as reusable context — prefer building on it over re-exploring in follow-up steps.
