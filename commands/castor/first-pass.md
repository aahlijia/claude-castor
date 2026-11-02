Get a free first-pass review of a diff from a NVIDIA NIM agent that can read the surrounding code.

Load the tool schema before calling — MCP tools are deferred and cannot be called until hydrated:

    ToolSearch("select:mcp__claude-castor__nim_first_pass_review")

Call `nim_first_pass_review` with:
- `cwd`: the absolute path of the current working directory
- `rev_range` for committed work (for example `abc123..HEAD`), or `diff_file` for a saved diff. Omit both to review uncommitted changes. Never paste the diff into the call: it is read server-side, so it costs no Claude context.
- `focus`: a few lines of project rules to check, if the project has them ($ARGUMENTS may name them)

Diffs over about 1,500 lines are refused: review those in parts. The result ranks Critical, Important and Minor findings as `path:line - failure - smallest fix`, with citations checked. Treat each as a lead: verify it against the code before reporting or fixing it, and never cite it as evidence. It complements a Claude review, it does not replace one.
