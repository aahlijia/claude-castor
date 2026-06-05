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
    NOT SIGNED IN, and READY.
  - Install/auth docs updated: `curl … install.sh | bash` instead of npm;
    `agy -p "ok"` / the new `gemini_auth` tool instead of bare `gemini` OAuth.

### Added

- `gemini_auth` tool — launches `agy`, extracts the Google OAuth URL it prints,
  opens it in the browser, and waits for agy's native polling to complete
  sign-in (one-time; the token persists in the system keyring).

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
