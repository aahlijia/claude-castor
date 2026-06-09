Trace where and how a symbol is used across the current codebase using Antigravity (agy).

The symbol to trace is: $ARGUMENTS

Load the tool schema before calling — MCP tools are deferred and cannot be called until hydrated:

    ToolSearch("select:mcp__claude-castor__gemini_find_usages")

Read `~/.claude/GEMINI.md` first for context, then call `gemini_find_usages` with:
- `cwd` set to the absolute path of the current working directory
- `symbol` set to the symbol named in $ARGUMENTS

`gemini_find_usages` runs agy as a sandboxed explorer and returns every use of the symbol — definitions, calls, imports, re-exports, tests — grouped by file with line references, plus a one-line summary of its role.

After agy responds, synthesize the usages into directly actionable context for the current task. Verify specific line numbers and signatures before acting on them.
