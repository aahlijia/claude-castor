Find the exact lines a task needs, using a free NVIDIA NIM agent that reads the repo.

Load the tool schema before calling — MCP tools are deferred and cannot be called until hydrated:

    ToolSearch("select:mcp__claude-castor__nim_scout")

Call `nim_scout` with:
- `question`: what the work is and what to locate ($ARGUMENTS), with names or paths if you have them
- `cwd`: the absolute path of the current working directory

It returns up to 15 `path:start-end - why` bullets, with every citation checked against the files (failures are tagged `[unverified]`). Use the list to read only those ranges, or to copy the lines into a brief for another agent. If `nim_status` is not ready, say so and fall back to `/castor:search`.

The scout is another model's reading: confirm a range before acting on it.
