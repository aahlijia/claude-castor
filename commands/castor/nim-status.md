Check the optional NVIDIA NIM backend.

Load the tool schema before calling — MCP tools are deferred and cannot be called until hydrated:

    ToolSearch("select:mcp__claude-castor__nim_status")

Call `nim_status` and report the result clearly:
- If READY: confirm the base URL, default model, whether the default model is in the catalog, the client-side limits, this session's request counters, and any active rate-limit cooldown. Never print or ask for the API key.
- If NOT CONFIGURED: tell the user to create a free key at https://build.nvidia.com/settings/api-keys and register it with the MCP server's environment (the key must never be passed as a tool argument):

      claude mcp add -s user claude-castor -e NVIDIA_API_KEY=nvapi-... -- /absolute/path/to/uv run --extra dekko --directory /absolute/path/to/claude-castor server.py

  (`claude mcp remove claude-castor -s user` first if it is already registered.) They restart Claude Code, then re-run /castor:nim-status.
- If NOT READY: relay the error. A 401 means the key is wrong or revoked; a 404 means the default model is not served (run `nim_models` with a `query` and set `CASTOR_NIM_MODEL`); rate-limit errors mean the free tier is saturated — wait or use the agy tools.

To use NIM after setup, read `~/.claude/GEMINI.md` (section "NVIDIA NIM") — NIM cannot explore the repo, so it only sees content you inline.
