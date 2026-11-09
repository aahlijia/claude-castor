Run an allowlisted suite command in the background with Castor and collect its report.

Load the tool schemas before calling: MCP tools are deferred and cannot be called until hydrated:

    ToolSearch("select:mcp__claude-castor__castor_run_start,mcp__claude-castor__castor_poll")

Call `castor_run_start` with:
- `cwd`: the absolute path of the current working directory
- `command`: the script and its arguments from $ARGUMENTS, one list item each (the script must match `.castor/run.json`'s `allow`)
- `expect_minutes`: how long the run should take
- `label`: optional short name

Then poll the returned id with `castor_poll` every few minutes until `status` is not `running`. The result has the verdict, the FAIL and NOT ASSESSED rows, the verdict table, flags and a triage of the log. Castor never kills the run: for `stopped` or `gave_up`, report the `pid` and let the user decide. A refusal naming `CASTOR_RUN_ROOTS` or `.castor/run.json` means the project is not set up for runs; tell the user which key is missing.
