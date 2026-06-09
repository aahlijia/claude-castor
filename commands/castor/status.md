Check the status of the Antigravity (agy) CLI integration.

Load the tool schema before calling — MCP tools are deferred and cannot be called until hydrated:

    ToolSearch("select:mcp__claude-castor__gemini_status")

Call `gemini_status` with `cwd` set to the absolute path of the current working directory and report the result clearly:
- If READY: confirm the version, cache size, and — if shown — the last-index timestamp and git SHA for this project
- If NOT INSTALLED: tell the user to run `curl -fsSL https://antigravity.google/cli/install.sh | bash`
- If NOT SIGNED IN: run `/castor:auth` (or call the `gemini_auth` tool) to get the sign-in instruction — the user types `! agy -p "ok"` in this prompt and completes the browser consent — then run /castor:status again to confirm
