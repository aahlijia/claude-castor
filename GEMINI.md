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
scratch. For multi-step work against the same project, pass a `session` name (e.g.
`session="refactor-auth"`), or `continue_session=True` for the project's default
session. Sessions are **Castor-owned**: the transcript is stored on disk and replayed
into a fresh `agy --print` call each turn (agy's own `--continue` is not used), so a
session is project-scoped and survives restarts.

- Sessions require `cwd` (they are keyed by the project root path).
- The **first** call with a given `session` starts fresh and establishes it;
  **subsequent** calls with the same name replay the accumulated transcript.
- Long transcripts auto-compress: once a replay exceeds the budget, older turns are
  folded into a rolling summary and the last few are kept verbatim — you don't manage
  this.
- Call `gemini_reset(cwd)` to clear the **default** session at a task boundary.
  For named sessions, use `gemini_session_delete(cwd, name)`; list with
  `gemini_sessions(cwd)` and inspect with `gemini_session_show(cwd, name)`.

Pattern: run a broad `/castor:explore` first, then follow up with narrower
`session="…"` prompts that build on what agy already learned.

---

## Model Selection

`gemini_prompt` accepts a `model` argument to pick which agy model runs the prompt.
Leave it unset to use agy's default. Call `gemini_models` to see the available names.

- A faster/cheaper model is fine for light summarization or indexing.
- A stronger model is worth it for deep cross-file reasoning or tricky analysis.

Only set `model` when the user asks for a specific one or the task clearly warrants
trading speed for capability — otherwise the default is fine.

---

## Effort Selection

`gemini_prompt` also accepts an `effort` argument (`low` / `medium` / `high`) to
pick how much reasoning effort agy's model applies to the prompt, independent of
which model is selected. Leave it unset to use agy's own default.

- `low` is a good fit for cheap/fast passes — light summarization, quick lookups,
  anything where depth doesn't matter.
- `medium`/`high` are worth it when the task needs deeper reasoning, at the cost
  of more time and quota.

Only set `effort` when the task clearly calls for trading depth for speed (or vice
versa) — otherwise the default is fine. Note: if `effort` is set on a model that
doesn't support reasoning effort, agy fails loudly (non-zero exit) rather than
silently ignoring the flag.

---

## Agent Selection

Call `gemini_agents` to list agy's built-in specialized agents (e.g.
`backend-architect`, `security-engineer`, `code-reviewer`, `root-cause-analyst`).
This is discovery only — no tool currently routes a prompt to a specific agent;
`gemini_prompt` and the workflow tools all run against agy's default agent for
now.

---

## Workflow Tools

For common shapes, prefer a purpose-built tool over assembling a raw
`gemini_prompt` — they bake in the right prompt and access tier, and they inherit
caching. Each takes a `cwd` (project root) and an optional `model`.

| Tool | Use when | Tier |
|---|---|---|
| `gemini_index(cwd)` | You need a map of an unfamiliar codebase before deeper work | sandbox |
| `gemini_review(cwd, diff=None)` | You want a free second opinion on a diff (defaults to `git diff HEAD`) | read-only |
| `gemini_find_usages(cwd, symbol)` | "Where/how is X used across the project?" | sandbox |
| `gemini_explain_error(cwd, error)` | You have a stack trace and want root-cause hypotheses | sandbox |

Notes:
- `gemini_index` is the canonical first call on a new codebase — run it once and
  build on the result (it is cached against the repo state in a git repo).
- `gemini_review` complements your own `/code-review`; it is a cheap extra pass,
  not a replacement, and costs no Claude context.
- These cover the frequent cases. For anything else, construct a `gemini_prompt`.

---

## Background Jobs

`gemini_prompt` blocks until agy responds — fine for a quick ask, but a long
exploration ties you up for the full timeout. For those, run the work in the
background:

- `gemini_start(prompt, …)` returns a job id immediately and runs agy off-thread.
- Do other useful work, then `gemini_poll(job_id)` to collect the result (it
  returns `running` with elapsed time until the job finishes).
- `gemini_jobs()` lists outstanding jobs.

When to reach for it:
- A single long/deep exploration where you have other things to do meanwhile.
- **Fan-out**: start several `gemini_start` jobs across different subtrees or
  questions, then poll them and synthesize — far faster than serial calls.

Rules:
- `gemini_start` is **fresh-only**: it takes no `session` / `continue_session`.
  For stateful multi-turn work, stay on `gemini_prompt` with a `session` name.
- It accepts the same other args as `gemini_prompt` (`trust`, `cwd`, `sandbox`,
  `model`, `files`, `directory`, `use_cache`). A cache hit finishes instantly.
- Don't poll in a tight loop — do real work between polls. Jobs are in-memory, so
  a server restart forgets them.

---

## Caching

Identical prompts against an unchanged codebase reuse a stored response instead
of re-running agy — instant and free. This happens automatically; you do not need
to manage it. The cache key covers the full assembled prompt, the `model`, the
`sandbox` flag, and the repo state (`git HEAD` + working-tree hash) when `cwd` is
a git repo.

What is **not** cached:

- `trust=True` calls — they may write files or run commands, so they always re-run.
- Session calls (`session` / `continue_session`) — stateful; stored separately.
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
| Need to understand a large codebase | `gemini_index` with `cwd` |
| Need to understand a large codebase (deep/custom) | `gemini_prompt` with `trust=True` + `cwd` |
| Review a diff for free | `gemini_review` with `cwd` |
| Trace a symbol's usages | `gemini_find_usages` with `cwd` + `symbol` |
| Find code by intent (no exact name) | `gemini_semantic_search` with `cwd` + `query` |
| Summarize a large file/dir | `gemini_summarize` with `cwd` + `target` |
| Draft docs for a symbol/file | `gemini_document` with `cwd` + `target` |
| Diagnose an error/stack trace | `gemini_explain_error` with `cwd` + `error` |
| User mentions a specific file | `gemini_prompt` with that file in `files` |
| User says "explore the project" | `gemini_prompt` with `trust=True` + `cwd`, no files |
| Need raw output | `gemini_prompt` with `raw=True` |
| Explore safely (no writes/commands) | `gemini_prompt` with `sandbox=True` + `cwd` |
| Follow-up on the same codebase | `gemini_prompt` with `session="…"` (+ `cwd`) |
| Long job — keep working meanwhile | `gemini_start`, then `gemini_poll` |
| Fan-out across subtrees | several `gemini_start`, then poll each |
| Check on background jobs | `gemini_jobs` |
| Clear the default session | `gemini_reset` with `cwd` |
| Force a fresh run (skip cache) | `gemini_prompt` with `use_cache=False` |
| Clear all cached responses | `gemini_cache_clear` |
| Pick a specific model | `gemini_prompt` with `model=...` |
| See available models | `gemini_models` |
| agy CLI not working | `gemini_status` |
| User needs to sign in | `gemini_auth` |
| User needs setup instructions | `gemini_setup` |
