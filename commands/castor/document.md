Generate documentation for a symbol or file in the current codebase using Antigravity (agy).

The symbol or file to document is: $ARGUMENTS

Load the tool schema before calling — MCP tools are deferred and cannot be called until hydrated:

    ToolSearch("select:mcp__claude-castor__gemini_document")

Read `~/.claude/GEMINI.md` first for context, then call `gemini_document` with:
- `cwd` set to the absolute path of the current working directory
- `target` set to the file path or symbol name in $ARGUMENTS

`gemini_document` runs agy as a sandboxed explorer and returns proposed docstrings or markdown, matching the project's existing documentation style. It is return-only — it never writes files. It is side-effect-free, so the result is cached against the repo state.

After agy responds, review the proposed documentation, then apply it yourself (agy does not edit files). Adjust anything that does not match the surrounding code's conventions before writing it in.
