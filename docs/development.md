# Development

- Before committing, run:
  ```bash
  uvx ruff check server.py castor
  uvx ruff format --check server.py castor
  python3 -m compileall -q server.py castor
  uv run --with pytest --with httpx --with fastmcp pytest tests
  ```
  `tests/test_nim.py` exercises the NIM backend against a local stub server (no
  network, no real key). The agy path has no automated tests.
  `tests/test_run.py` starts real bash subprocesses and shrinks minutes to
  milliseconds (`RUN_MINUTE_S`); every test ends its own processes, since
  Castor never kills one.
- Before changing a NIM role prompt, the agent loop or a default model, score
  it: `uv run --with httpx --with fastmcp python scripts/nim_eval.py
  scripts/eval/cases.json [private cases] --config label=model[,step=model]`
  (needs `NVIDIA_API_KEYS`; spends free-tier requests). It reports lead
  precision, recall of known findings, must-catch hits, time and steps.
  `--rescore results.json` re-scores saved answers after editing cases. A
  match needs the right file and lines and one of the finding's keywords;
  line proximity alone overstated hits. Cases for private repos stay outside
  this repo (the NQC set is `~/.cache/claude-castor/eval/nqc-cases.json`).
- Before a release, run `python3 scripts/check_agy_surface.py` to catch drift in
  the agy flags and subcommands the `castor` package depends on. After an intended
  agy upgrade, update `castor/agy/cli.py`, then refresh with `--update-snapshot`.
- Before a release and after a dekko bump, run `uv run python
  scripts/check_dekko_surface.py` to catch drift in the dekko flags and JSON
  fields Castor relies on; `--update-snapshot` refreshes it.
- The version string lives only in `pyproject.toml`. Work happens on `develop`.

## Roadmap

The longer plan lives in `.dev/development-plan.md` (kept locally, not tracked). Key items:

- Persistent chat sessions with `castor_reset` tool (agy `--continue`): **done**
- Model selection (`model` param + `gemini_models` tool): **done**
- Sandbox tier (`--sandbox`) between read-only and full trust: **done**
- Directory structure map when content is truncated: **done**
- Enhanced `gemini_status` diagnostics (available models): **done**
  (account/freshness skipped: agy has no whoami command and `agy update`
  mutates rather than checks)
- `/castor:auth` slash command for onboarding: **done**
- Response caching (`use_cache` param + `castor_cache_clear` tool; keyed by
  prompt + model + git HEAD/working-tree state): **done**
- Workflow tools (`gemini_index`, `gemini_review`, `gemini_find_usages`,
  `gemini_explain_error`; thin wrappers over the shared `_dispatch` core): **done**
- Async / background jobs (`gemini_start` / `castor_poll` / `castor_jobs`;
  thread-pool workers, poll model, fresh-only): **done**
- More workflow tools (`gemini_summarize`, `gemini_semantic_search`,
  `gemini_document`; thin wrappers over `_dispatch`): **done**
- Client-side sessions (`session` param + `castor_sessions` / `_show` /
  `_delete`; Castor-owned transcripts replayed into fresh `agy --print`,
  auto-summarized over budget, replaces the agy `--continue` path): **done**
- NVIDIA NIM backend (`nim_*` tools; inline-only, env-configured key, retry/
  rate-limit/cooldown handling): **done**
- Run tier and run compare (`castor_run_start`, `castor_runs`,
  `castor_compare_runs`; allowlisted, never kills): **done**
- Multi-provider routing: more OpenAI-compatible free-tier providers behind the
  same client, ordered fallback chains with per-candidate cooldowns, and
  context-aware candidate skipping (NIM is the first provider)
- Streaming output (token-by-token, vs. the current poll model)
