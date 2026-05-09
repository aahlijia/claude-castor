# Gemini Prompting Guide

This file contains instructions for how to use the `gemini_prompt` tool effectively.
Read this before making any Gemini tool calls.

---

## When to Use Gemini

Reach for Gemini when the task would consume significant Claude context or requires
broad exploration:

- Reading, indexing, or summarizing large files or entire codebases
- Cross-file analysis (e.g. "how is X used across the project?")
- Exploratory questions about an unfamiliar codebase
- Summarizing documentation, specs, or long outputs
- Any task where you would otherwise need to read many files sequentially

Do **not** use Gemini for:
- Tasks you can answer directly from what you already know
- Simple, single-file edits where you already have the file contents
- Final implementation decisions — Gemini provides research context, not ground truth

---

## How to Construct the Prompt

Always lead with task context before the actual ask. Gemini has no memory of your
conversation with the user, so the prompt must be self-contained.

**Template:**

```
Context: [1-2 sentences on what you are working on and where you are in the task]
Goal: [specific, concrete ask — what you need Gemini to find, summarize, or explain]

[the request in full]
```

**Example — good:**
```
Context: I am implementing a new authentication middleware for a Node.js API. I need to
understand how the existing session handling works before modifying it.
Goal: Summarize how sessions are created, stored, and validated across the codebase.
Identify the key files, functions, and any non-obvious dependencies.
```

**Example — bad:**
```
How does auth work?
```

The good version gives Gemini what it needs to return a targeted, useful response.
The bad version produces a generic answer that may not apply to this project.

---

## Including Task Context

Always include at least:
- What feature or fix you are working on
- What step you are currently on (e.g. "I have already written the handler, now I need
  to understand how errors are propagated upstream")
- What kind of output you need (summary, list of symbols, specific answer, etc.)

You do not need to include full conversation history — just the relevant current state.

---

## Files and Directories

Only pass `files` or `directory` if the **user explicitly mentioned** specific paths.
Do not scan the filesystem to find "relevant" files on the user's behalf — that is
exactly the kind of work Gemini is for.

If the user says "look at `src/`" or "check `utils.py`", pass those paths.
If the user says "explore the project" without specifying, ask Gemini in the prompt
and let it work with whatever context is available.

---

## Interpreting Responses

- Treat Gemini's output as research context, not ground truth
- Verify specific claims (line numbers, function signatures) before acting on them —
  Gemini can hallucinate details
- If Gemini's response is vague or incomplete, call `gemini_prompt` again with a more
  targeted follow-up prompt
- Use `raw=True` only when the user explicitly asks for unfiltered output

---

## Trust Mode (Agent Mode)

By default, `gemini_prompt` runs in safe headless mode (`trust=False`) — Gemini
receives pre-loaded file content and responds as a Q&A assistant.

With `trust=True`, Gemini runs in full agent mode and can actively explore the
filesystem, grep for patterns, follow imports, and navigate the project on its own.
This produces significantly better results for deep research tasks.

**Prerequisite:** The user must have completed Google OAuth at least once (run `gemini`
in a terminal). No interactive directory trust step is required — the server sets
`GEMINI_CLI_TRUST_WORKSPACE=true` automatically. Call `gemini_setup` if the user
hasn't authenticated yet.

Use `trust=True` when:
- The user explicitly asks for deep research or full codebase exploration
- The task requires following chains of imports or cross-file symbol resolution
- Pre-loading files would be insufficient or impractical

---

## Quick Reference

| Situation | What to do |
|---|---|
| Need to understand a large codebase | `gemini_prompt` with `trust=True` |
| User mentions a specific file | `gemini_prompt` with that file in `files` |
| User says "explore the project" | `gemini_prompt` with `trust=True`, no files |
| Need raw output | `gemini_prompt` with `raw=True` |
| Gemini CLI not working | `gemini_status` |
| User needs to set up trust / auth | `gemini_setup` |
