Get a free second-opinion code review from Antigravity (agy).

Load the tool schema before calling — MCP tools are deferred and cannot be called until hydrated:

    ToolSearch("select:mcp__claude-castor__gemini_review")

Read `~/.claude/GEMINI.md` first for context, then call `gemini_review` with:
- `cwd` set to the absolute path of the current working directory
- `diff`: omit it to review the uncommitted changes (`git diff HEAD`, staged and unstaged). Only set it if the user wants a different scope ($ARGUMENTS) — e.g. "staged", "against main", or a commit range. In that case run the matching `git diff …` yourself and pass its output as `diff`.

`gemini_review` returns a correctness-focused review (bugs, broken edge cases, error handling, boundary and concurrency mistakes), not style nitpicks. It is a cheap extra pass that complements — does not replace — your own review.

After agy responds, relay the findings, separating anything that looks like a real bug from lower-confidence notes. Verify specific claims against the actual code before acting on them.
