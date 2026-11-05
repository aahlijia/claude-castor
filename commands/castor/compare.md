Compare suite logs row by row with Castor: what regressed, what is flaky.

Load the tool schema before calling: MCP tools are deferred and cannot be called until hydrated:

    ToolSearch("select:mcp__claude-castor__castor_compare_runs")

Call `castor_compare_runs` with:
- `cwd`: the absolute path of the current working directory
- `logs`: the log files from $ARGUMENTS, oldest first (inside `cwd` or any worktree of its repo)
- `rows`: optional row names, or pieces of them, to keep
- `same_code`: true when no fix landed between the runs

It returns the rows that changed, by class (regressed, flaky, failing, not assessed, numbers changed, missing, fixed), with the trial lines that differ from the first run. No model reads it: decide harness or game yourself from the trial lines and the code.
