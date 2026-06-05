# Antigravity Prompting Guide

This file contains instructions for how to use the `gemini_prompt` tool effectively.
The tool drives the Antigravity CLI (`agy`); read this before making any calls.

---

## When to Use Antigravity

Reach for `gemini_prompt` when the task would consume significant Claude context or
requires broad exploration:

- Reading, indexing, or summarizing large files or entire codebases
- Cross-file analysis (e.g. "how is X used across the project?")
- Exploratory questions about an unfamiliar codebase
- Summarizing documentation, specs, or long outputs
- Any task where you would otherwise need to read many files sequentially

Do **not** use it for:
- Tasks you can answer directly from what you already know
- Simple, single-file edits where you already have the file contents
- Final implementation decisions — agy provides research context, not ground truth

---

## How to Construct the Prompt

Always lead with task context before the actual ask. agy has no memory of your
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

The good version gives agy what it needs to return a targeted, useful response.
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

Prefer rooting agy at the project via `cwd` and letting it explore — that is exactly
the kind of work it is for. Only pass `files` or `directory` to inline content when
the **user explicitly mentioned** specific paths. Do not scan the filesystem to find
"relevant" files on the user's behalf.

If the user says "look at `src/`" or "check `utils.py`", pass those paths.
If the user says "explore the project" without specifying, use `trust=True` with `cwd`
set and let agy explore on its own. Use `add_dirs` to grant read access to extra
directories without inlining them.

---

## Interpreting Responses

- Treat agy's output as research context, not ground truth
- Verify specific claims (line numbers, function signatures) before acting on them —
  agy can hallucinate details
- If agy's response is vague or incomplete, call `gemini_prompt` again with a more
  targeted follow-up prompt
- Use `raw=True` only when the user explicitly asks for unfiltered output

---

## Access Tiers

`gemini_prompt` runs at one of three access levels. They are mutually exclusive —
`trust` wins over `sandbox` if both are set.

| Tier | Flag | What agy can do |
|---|---|---|
| read-only (default) | — | Answers from inlined content; no filesystem actions |
| sandbox | `sandbox=True` | Explores the filesystem under terminal restrictions; does not blindly auto-approve actions |
| trust | `trust=True` | Full agent mode (`--dangerously-skip-permissions`); explores, greps, follows imports, and auto-approves all tool actions |

`sandbox=True` is the safe middle ground: agy actively explores (so you get
agent-quality results) but cannot run unrestricted commands. Prefer it over `trust`
unless the task genuinely needs agy to write files or run arbitrary commands.

**Prerequisites for sandbox and trust:**
- `cwd` is **required** for `trust=True` — pass the absolute project path so agy's
  workspace is rooted correctly. The call errors without it. Pass `cwd` for `sandbox`
  too so agy is rooted in the right project.
- The user must be signed in to Antigravity. If `gemini_status` reports NOT SIGNED IN,
  call the `gemini_auth` tool (it returns the `! agy -p "ok"` sign-in instruction).

Use `trust=True` when:
- The user explicitly asks for deep research or full codebase exploration
- The task requires following chains of imports or cross-file symbol resolution
- Pre-loading files would be insufficient or impractical
- The work needs agy to write files or run commands (otherwise prefer `sandbox=True`)

---

## Sessions

By default every `gemini_prompt` call is **cold** — agy re-explores the codebase from
scratch, re-spending its time budget. For multi-step work against the same project,
pass `continue_session=True` so agy resumes its prior conversation and keeps the
context it already built.

- The **first** call of a server run (or the first after `gemini_reset`) always starts
  fresh, even with `continue_session=True` — that call establishes the session.
- **Subsequent** calls with `continue_session=True` resume it.
- Call `gemini_reset` at a task boundary to drop stale context before starting an
  unrelated job, so the next continued call does not carry over the prior topic.
- `conversation_id` resumes a specific agy conversation by ID and takes precedence over
  `continue_session`. Only use it when you have an ID to resume.

Pattern: run a broad `/castor:explore` first, then follow up with narrower
`continue_session=True` prompts that build on what agy already learned.

---

## Model Selection

`gemini_prompt` accepts a `model` argument to pick which agy model runs the prompt.
Leave it unset to use agy's default. Call `gemini_models` to see the available names.

- A faster/cheaper model is fine for light summarization or indexing.
- A stronger model is worth it for deep cross-file reasoning or tricky analysis.

Only set `model` when the user asks for a specific one or the task clearly warrants
trading speed for capability — otherwise the default is fine.

---

## Caching

Identical prompts against an unchanged codebase reuse a stored response instead
of re-running agy — instant and free. This happens automatically; you do not need
to manage it. The cache key covers the full assembled prompt, the `model`, the
`sandbox` flag, and the repo state (`git HEAD` + working-tree hash) when `cwd` is
a git repo.

What is **not** cached:

- `trust=True` calls — they may write files or run commands, so they always re-run.
- Session continuations (`continue_session` / `conversation_id`) — stateful.
- `sandbox=True` calls outside a git repo — the code's freshness can't be proven.
- Error/status responses (bracketed output).

Implications for how you call the tool:

- Re-asking the same question mid-task is cheap — don't contort prompts to avoid
  "repeating" yourself; an identical prompt is a feature (a free hit).
- If you genuinely need agy to re-run despite an identical prompt (e.g. you
  suspect a non-deterministic explore answer), pass `use_cache=False`.
- A cached explore answer is "a prior good answer for this question against this
  code," not a guarantee of bit-identical regeneration. Inline-only calls are
  fully deterministic from their input.
- Call `gemini_cache_clear` only when the user changes agy's config/default model
  or wants to reclaim disk — not as part of normal flow.

---

## Quick Reference

| Situation | What to do |
|---|---|
| Need to understand a large codebase | `gemini_prompt` with `trust=True` + `cwd` |
| User mentions a specific file | `gemini_prompt` with that file in `files` |
| User says "explore the project" | `gemini_prompt` with `trust=True` + `cwd`, no files |
| Need raw output | `gemini_prompt` with `raw=True` |
| Explore safely (no writes/commands) | `gemini_prompt` with `sandbox=True` + `cwd` |
| Follow-up on the same codebase | `gemini_prompt` with `continue_session=True` |
| Start a fresh session | `gemini_reset` |
| Force a fresh run (skip cache) | `gemini_prompt` with `use_cache=False` |
| Clear all cached responses | `gemini_cache_clear` |
| Pick a specific model | `gemini_prompt` with `model=...` |
| See available models | `gemini_models` |
| agy CLI not working | `gemini_status` |
| User needs to sign in | `gemini_auth` |
| User needs setup instructions | `gemini_setup` |
