Find code by natural-language intent across the current codebase using Antigravity (agy).

The thing to search for is: $ARGUMENTS

Load the tool schema before calling — MCP tools are deferred and cannot be called until hydrated:

    ToolSearch("select:mcp__claude-castor__gemini_semantic_search")

Read `~/.claude/GEMINI.md` first for context, then call `gemini_semantic_search` with:
- `cwd` set to the absolute path of the current working directory
- `query` set to the natural-language description in $ARGUMENTS

`gemini_semantic_search` runs agy as a sandboxed explorer and returns a JSON array of up to 20 hits, each `{path, line, reason}`, ranked by relevance. Unlike `gemini_find_usages` (symbol-exact), this works from intent — e.g. "where is auth validated?". It is side-effect-free, so the result is cached against the repo state.

After agy responds, verify the specific line numbers before acting on them, then synthesize the hits into actionable context for the current task.
