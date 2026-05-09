Perform a full codebase exploration using Gemini in agent mode.

Read GEMINI.md first for prompting guidance, then call `gemini_prompt` with:
- `trust=True` (full agent mode — Gemini navigates the filesystem on its own)
- No `files` or `directory` — Gemini will explore independently
- A prompt that includes:
  - The current working directory path
  - What the user is trying to understand or accomplish (use the argument if provided: $ARGUMENTS)
  - A request for: key files and their roles, how the system works end-to-end, notable patterns or dependencies, and anything surprising or worth flagging

After Gemini responds, summarize the findings and highlight anything directly relevant to the user's current task.
