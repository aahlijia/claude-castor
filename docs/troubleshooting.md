# Troubleshooting

## Antigravity (agy)

**`agy` not found.** Run
`curl -fsSL https://antigravity.google/cli/install.sh | bash`, then verify with
`agy --version`.

**Not signed in.** Run `/castor:auth` (or call the `gemini_auth` tool) for the
sign-in command, then run `agy -p "ok"` and finish the Google consent in your
browser.

**Timeout.** agy print mode has a 300 s limit per call. For large projects,
prefer agent mode (`trust=True`) over pre-loading a full directory.

**"Antigravity produced no output: it tried to run a shell command…"** agy 1.3+
auto-denies shell commands it can't prompt for in headless mode, which ends the
run with no output. Retry with `sandbox=True` (read commands allowed, workspace
read-only) or `trust=True` (full access), with `cwd` set to the project root.

**Garbled or empty response.** Try `raw=True` to see the unfiltered output.

## NVIDIA NIM

**Not configured or key rejected.** Run `/castor:nim-status`. Set
`NVIDIA_API_KEY` in the MCP server's environment (see [nim.md](nim.md#setup)).
A 401 means the key is wrong or revoked.

**Rate-limited.** NVIDIA caps the free tier. Castor retries with backoff and
then reports how long to wait. Lower `CASTOR_NIM_RPM`, retry later, or use the
agy tools meanwhile.

## Run tier

See [run-tier.md](run-tier.md). A refused run names the switch that refused it
(`CASTOR_RUN=0`, a missing `CASTOR_RUN_ROOTS` entry, no `.castor/run.json`, or a
command outside `allow`).
