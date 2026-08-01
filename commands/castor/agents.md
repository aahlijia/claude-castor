List agy's built-in specialized agents.

Load the tool schema before calling — MCP tools are deferred and cannot be called until hydrated:

    ToolSearch("select:mcp__claude-castor__gemini_agents")

Call `gemini_agents` and print the result clearly:
- If it lists agent names, present them as-is — these are the names you can pass as the `agent` argument to `gemini_prompt` (and any workflow tools that accept one)
- If it reports not signed in, run `/castor:auth` (or call the `gemini_auth` tool) to get the sign-in instruction, then run `/castor:agents` again to confirm
- If it reports the `agy` CLI is not installed, tell the user to run `curl -fsSL https://antigravity.google/cli/install.sh | bash`, then retry
