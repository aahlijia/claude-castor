# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Migrated from the retired Gemini CLI to the Antigravity CLI (`agy`).**
  Google shut down the free Gemini CLI on 2026-06-18; Antigravity is its
  successor (still powered by Gemini models, free individual tier, Google
  sign-in). The MCP tools keep their `gemini_*` names for compatibility.
  - `_run_gemini` → `_run_agy`: invokes `agy --print` (prompt piped via stdin,
    so prompt size is no longer bound by the argument-length limit),
    `--print-timeout 300s`. Agent mode now passes
    `--dangerously-skip-permissions` instead of setting
    `GEMINI_CLI_TRUST_WORKSPACE`; `add_dirs` maps to repeated `--add-dir`.
  - `SYSTEM_INSTRUCTION` now suppresses agy's print-mode noise: action
    narration ("I will read…"), the trailing "Summary of Work" section, and
    `file://` links.
  - `gemini_prompt` gains an `add_dirs` parameter for granting agy read access
    to extra directories without inlining them.
  - `gemini_status` checks `agy --version` and distinguishes NOT INSTALLED,
    NOT SIGNED IN, and READY. On READY it now also lists the available models
    (best-effort via `agy models`; omitted silently if unavailable).
  - Install/auth docs updated: `curl … install.sh | bash` instead of npm;
    `agy -p "ok"` / the new `gemini_auth` tool instead of bare `gemini` OAuth.
- `_needs_auth` hardened — case-insensitive match against several sign-in
  markers instead of one literal string, so a wording change in agy's output
  no longer silently breaks auth detection across the server.
- `_inline_directory` now lists the skipped file paths (capped, with an
  "…and N more" overflow) instead of only a count, so Claude can request
  specific dropped files in a follow-up.
- `_run_agy` argv assembly extracted into a `_build_agy_cmd` helper.

### Added

- `gemini_auth` tool — returns the sign-in instruction (`! agy -p "ok"`) for the
  user to run in the Claude Code prompt. An MCP tool call blocks Claude while it
  runs, so it cannot drive agy's interactive browser flow itself; the user
  completes the one-time Google consent (token persists in the system keyring).
- Persistent sessions — `gemini_prompt` gains `continue_session` (resume agy's
  prior conversation via `--continue`) and `conversation_id` (resume a specific
  conversation via `--conversation <id>`), plus a `gemini_reset` tool to drop the
  session so the next call starts fresh.
- Model selection — `gemini_prompt` gains a `model` parameter (`--model`), and a
  new `gemini_models` tool lists the models available to agy.
- Sandbox tier — `gemini_prompt` gains a `sandbox` parameter (`--sandbox`): agy
  explores under terminal restrictions without auto-approving actions. A safe
  middle ground between read-only and `trust`; `trust` takes precedence if both
  are set.
- `/castor-auth` slash command — guides first-time sign-in.

### Carried over (prior unreleased work)

- `gemini_prompt` rejects `trust=True` calls that omit `cwd`.
- File inlining reads each file once; size check precedes the read.
- `ruff check` / `ruff format --check` pass clean.

## [0.1.0] - 2026-05-28

### Added

- Initial release of **Claude Castor**, a Gemini MCP server for Claude Code.
- `gemini_prompt` tool — sends a constructed prompt to the `@google/gemini-cli`
  subprocess and returns the response. Supports inlining `files` and a
  `directory` (with `.gitignore` awareness, binary detection, and 100 KB/file
  and 800 KB-total caps), a `raw` mode, and a `trust` agent mode that grants
  Gemini filesystem access via `GEMINI_CLI_TRUST_WORKSPACE`.
- `gemini_status` tool — verifies the `gemini` CLI is installed and
  authenticated.
- `gemini_setup` tool — returns step-by-step Google OAuth setup instructions.
- `cwd` parameter on `gemini_prompt` to root Gemini's filesystem access in the
  correct project directory during agent mode.
- `install.py` installer — registers the MCP server with `claude mcp add` and
  copies the slash commands and `GEMINI.md` into `~/.claude/`.
- Slash commands: `/castor-explore`, `/castor-research`, `/castor-status`.
- `GEMINI.md` prompting guide and project documentation.

[Unreleased]: https://github.com/aahlijia/claude-castor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/aahlijia/claude-castor/releases/tag/v0.1.0
