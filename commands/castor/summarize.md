Summarize a large file or directory using Antigravity (agy), to save Claude's context.

The file or directory to summarize is: $ARGUMENTS

Load the tool schema before calling — MCP tools are deferred and cannot be called until hydrated:

    ToolSearch("select:mcp__claude-castor__gemini_summarize")

Read `~/.claude/GEMINI.md` first for context, then call `gemini_summarize` with:
- `cwd` set to the absolute path of the current working directory
- `target` set to the file or directory path named in $ARGUMENTS

`gemini_summarize` runs agy as a sandboxed explorer and returns a JSON object with `summary` (a short paragraph) and `key_points` (terse bullets; per-area roll-ups for a directory). It is side-effect-free, so the result is cached against the repo state.

After agy responds, present the summary and the key points. Treat this as reusable context — prefer building on it over re-reading the file yourself.
