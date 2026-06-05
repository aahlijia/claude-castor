Check the status of the Antigravity (agy) CLI integration.

Call `gemini_status` and report the result clearly:
- If READY: confirm the version and that both default mode and agent mode (trust=True) are available
- If NOT INSTALLED: tell the user to run `curl -fsSL https://antigravity.google/cli/install.sh | bash`
- If NOT SIGNED IN: run `/castor:auth` (or call the `gemini_auth` tool) to get the sign-in instruction — the user types `! agy -p "ok"` in this prompt and completes the browser consent — then run /castor:status again to confirm
