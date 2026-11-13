Find the exact lines a task needs, using a free NVIDIA NIM agent that reads the repo.

Load the tool schema before calling — MCP tools are deferred and cannot be called until hydrated:

    ToolSearch("select:mcp__claude-castor__nim_scout")

Call `nim_scout` with:
- `question`: what the work is and what to locate ($ARGUMENTS), with names or paths if you have them
- `cwd`: the absolute path of the current working directory
- `text`: optional. `"inline"` when you will work on the ranges yourself (their numbered lines follow the bullets); `"file"` when you are briefing another agent (Castor writes the lines to a pack under `.castor/packs/` and names it: put that path in the brief)

It returns up to 15 `path:start-end - why` bullets, with every citation checked against the files (failures are tagged `[unverified]`). Without `text`, use the list to read only those ranges. With `text`, Castor has copied the lines from disk after its checks, so they are exact and you can edit from them; a range that failed a check or did not fit the budget stays a pointer. If `nim_status` is not ready, say so and fall back to `/castor:search`.

The scout is another model's reading: confirm a range before acting on it.
