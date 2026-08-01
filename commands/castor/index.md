Produce a structured map of the current codebase using Antigravity (agy).

## Step 1 — Check project memory for a valid cached index

The project memory directory is at:

    ~/.claude/projects/<encoded-cwd>/memory/

where `<encoded-cwd>` is the absolute `cwd` with every `/` replaced by `-`
(for example, `/Users/alice/myproject` → `-Users-alice-myproject`).

Check whether `castor-index.md` exists in that directory.

If it exists, read it and extract the `git_sha` from the frontmatter. Then
get the current HEAD SHA:

    git -C <cwd> rev-parse HEAD

If the SHAs match, the cached index is still current — skip to
"Present the index" at the bottom. Do NOT call `gemini_index`.

## Step 2 — Run a fresh index

Load the tool schema before calling — MCP tools are deferred and cannot be
called until hydrated:

    ToolSearch("select:mcp__claude-castor__gemini_index")

Call `gemini_index` with `cwd` set to the absolute path of the current
working directory. It uses agy's default agent (pass `agent="repo-index"`
to opt into agy's purpose-built indexing agent — measured less reliable
on agy 1.1.9 combined with sandboxed exploration and `--json-schema`
enforcement, so it isn't the default) and enforces the JSON shape below
via agy's `--json-schema` rather than just requesting it — the fallback
envelope described in Step 2 still applies if schema enforcement misses.

`gemini_index` returns a JSON string. Parse it and check whether it
contains a `summary` key:

- **Fully structured response** (has `summary`, `entry_points`, `modules`,
  etc.) — continue to Step 3.
- **Parse-failure fallback** (only `git_sha` + `raw_markdown`) — present
  `raw_markdown` directly and skip to "Present the index." Do not write
  to memory.

## Step 3 — Write the index to project memory

Write `castor-index.md` to the memory directory with the following
content (replace each `<…>` with the value from the JSON response):

    ---
    name: castor-index
    description: "Codebase index — entry points, modules, architecture; git_sha <git_sha>"
    metadata:
      type: project
      git_sha: <git_sha>
    ---

    **git_sha:** `<git_sha>`

    ## Summary

    <summary>

    ## Entry Points

    <entry_points rendered as a bullet list>

    ## Modules

    <each module as: **<name>** — `<file>:<line>` — <role>>

    ## Exception Types

    <exception_types as a bullet list; write *none* if the list is empty>

    ## Architecture

    <architecture>

Then add or update the index pointer in `MEMORY.md` (same directory):

    - [Codebase Index](castor-index.md) — entry points, modules, architecture; SHA <first 8 chars of git_sha>

If a `[Codebase Index]` line already exists in `MEMORY.md`, replace it in
place. Otherwise append it at the end. If `MEMORY.md` does not exist,
create it with just this line.

## Present the index

Present the index (from memory or fresh) and highlight the parts most
relevant to what the user is trying to do (use the argument if provided:
$ARGUMENTS). Treat this as reusable context — prefer building on it over
re-exploring in follow-up steps.
