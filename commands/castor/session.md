Inspect and manage Antigravity (agy) conversation sessions for this project.

The request (e.g. "list", "show refactor-auth", "delete refactor-auth") is: $ARGUMENTS

Sessions are Castor-owned, project-scoped conversations. Continue one from
`gemini_prompt` by passing `session="<name>"` (or `continue_session=True` for the
default session); Castor replays the prior transcript as context and appends each
turn. This skill is for inspecting and pruning them.

Load the tool schemas before calling — MCP tools are deferred and cannot be called until hydrated:

    ToolSearch("select:mcp__claude-castor__gemini_sessions,mcp__claude-castor__gemini_session_show,mcp__claude-castor__gemini_session_delete")

Then, based on $ARGUMENTS, call one of these with `cwd` set to the absolute path of the current working directory:
- **list** (or no argument) — `gemini_sessions(cwd)` to list sessions with turn counts and last-updated times.
- **show `<name>`** — `gemini_session_show(cwd, session="<name>")` to print a session's stored transcript (summary + turns).
- **delete `<name>`** — `gemini_session_delete(cwd, session="<name>")` to remove a session. To clear the default session instead, use `gemini_reset(cwd)`.

After the tool responds, present the result plainly. Sessions older than 30 days are evicted automatically.
