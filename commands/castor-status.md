Check the status of the Antigravity (agy) CLI integration.

Call `gemini_status` and report the result clearly:
- If READY: confirm the version and that both default mode and agent mode (trust=True) are available
- If NOT INSTALLED: tell the user to run `curl -fsSL https://antigravity.google/cli/install.sh | bash`
- If NOT SIGNED IN: call the `gemini_auth` tool to open the Google sign-in URL in the browser (or tell the user to type `! agy -p "ok"` in this prompt), then run /castor-status again to confirm
