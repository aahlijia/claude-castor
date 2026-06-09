Sign in to the Antigravity (agy) CLI so the MCP tools can run.

Load the tool schema before calling — MCP tools are deferred and cannot be called until hydrated:

    ToolSearch("select:mcp__claude-castor__gemini_auth")

Call the `gemini_auth` tool, then relay its instruction to the user clearly:

- If it returns the sign-in instruction, tell the user to type this directly in
  the Claude Code prompt (the `!` runs it as a live terminal command):

      ! agy -p "ok"

  They then complete the Google consent in the browser when agy prints the
  sign-in URL. Sign-in cannot be driven from the tool itself — an MCP tool call
  blocks while it runs, so the user must run the command interactively. It is a
  one-time step; the token persists in the system keyring.
- If it reports the `agy` CLI is not installed, tell the user to run
  `curl -fsSL https://antigravity.google/cli/install.sh | bash`, then retry.

After the user has signed in, run `/castor:status` to confirm it reports READY.
