Check the status of the Gemini CLI integration.

Call `gemini_status` and report the result clearly:
- If READY: confirm the version and that both default mode and agent mode (trust=True) are available
- If NOT INSTALLED: tell the user to run `npm install -g @google/gemini-cli`
- If NOT AUTHENTICATED: tell the user to type `! gemini --skip-trust` in this prompt to complete Google OAuth, then run /castor:status again to confirm
