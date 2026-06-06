Diagnose an error or stack trace against the current codebase using Antigravity (agy).

The error or stack trace is: $ARGUMENTS

Read `~/.claude/GEMINI.md` first for context, then call `gemini_explain_error` with:
- `cwd` set to the absolute path of the current working directory
- `error` set to the error text in $ARGUMENTS

`gemini_explain_error` runs agy as a sandboxed explorer, traces the error to its likely source, and returns the most probable root causes — ranked — each with the specific files and lines to check and why.

After agy responds, present the ranked hypotheses and the files to inspect. Verify the specifics against the actual code before acting, and propose a concrete next step.
