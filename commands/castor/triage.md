Boil a long log down to its failures with a free NVIDIA NIM agent.

Load the tool schema before calling — MCP tools are deferred and cannot be called until hydrated:

    ToolSearch("select:mcp__claude-castor__nim_triage_log")

Call `nim_triage_log` with:
- `cwd`: the absolute path of the current working directory
- `log_path`: the log file ($ARGUMENTS), which must be inside `cwd`
- `question`: optional, one extra thing to determine (for example whether a run finished)

It returns a verdict line and `log:LINE - what failed - likely cause` items, so you never read the log. Read only the lines it cites if you need to confirm one.
