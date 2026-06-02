# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `gemini_prompt` now rejects `trust=True` calls that omit `cwd`, returning a
  clear error instead of silently letting Gemini explore the server's working
  directory.

### Changed

- File inlining (`_inline_files`, `_inline_directory`) now reads each file once
  instead of twice — a single `_read_bytes` call feeds both the binary check
  and the content block. In directory walks, the size check runs before the
  read so oversized binaries are never fully loaded.

### Fixed

- Resolved all `ruff` lint and format violations; the codebase now passes
  `ruff check` and `ruff format --check` clean.

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
