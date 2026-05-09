Research a specific topic, feature, symbol, or file in the codebase using Gemini in agent mode.

The research target is: $ARGUMENTS

Read GEMINI.md first for prompting guidance, then call `gemini_prompt` with:
- `trust=True` (full agent mode — Gemini can search, grep, and follow imports)
- No `files` or `directory` — let Gemini locate what it needs
- A prompt that includes:
  - The current working directory path
  - The specific research target: $ARGUMENTS
  - Task context: what you are working on and why you need this information
  - A request for: where the topic is defined, how it is used, key dependencies, and anything non-obvious

After Gemini responds, synthesize the findings into directly actionable context for the current task. Verify any specific line numbers or function signatures before acting on them.
