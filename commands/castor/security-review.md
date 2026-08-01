Get a free security-focused code review from Antigravity (agy).

Load the tool schema before calling — MCP tools are deferred and cannot be called until hydrated:

    ToolSearch("select:mcp__claude-castor__gemini_security_review")

Read `~/.claude/GEMINI.md` first for context, then call `gemini_security_review` with:
- `cwd` set to the absolute path of the current working directory
- `diff`: omit it to review the uncommitted changes (`git diff HEAD`, staged and unstaged). Only set it if the user wants a different scope ($ARGUMENTS) — e.g. "staged", "against main", or a commit range. In that case run the matching `git diff …` yourself and pass its output as `diff`.

`gemini_security_review` returns a security-focused review (injection risks, auth/authz gaps, secrets or credentials handled unsafely, unsafe deserialization, path traversal, SSRF, insecure defaults, anything exploitable under attacker-influenced input) and explicitly skips correctness/style nitpicks — that's `/castor:review`'s job. It runs agy's `security-engineer` agent by default (pass `agent=None` to fall back to agy's default agent, or another name to override). It complements — does not replace — `/castor:review`; run both for full coverage.

After agy responds, relay the findings, separating anything that looks like a real vulnerability from lower-confidence notes. Verify specific claims against the actual code before acting on them.
