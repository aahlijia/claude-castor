# Run tier

Castor's one path that runs project commands: an allowlisted suite script
started in the background and reported when it ends (replaces a subagent
that waits on a long run). Two keys open it, and both are needed:

- the project commits `.castor/run.json`: `allow` (globs relative to the
  root, matched against the resolved script path, which must be an
  executable file inside the root), optional `env_file` (sourced before the
  command; must be inside the root), `log_line` (regex whose group 1 is the
  log path the suite prints, default `^log: (.+)$`), `overdue_command` (run
  once at `expect_minutes + grace_minutes` while no verdict table is out;
  it must be an executable file inside the root but need not match `allow`,
  which only limits what callers can start),
  `grace_minutes` (10), `max_hours` (8), `max_running` (4), `flags`
  (`{name, pattern, stop}`, matched on whole console lines), `log_format`
  and `env_allow` (both below). Unknown keys and bad regexes are errors.
- `log_format` says how the suite log spells its verdicts, for the run
  result, `castor_compare_runs`, `nim_triage_log` and `nim_explain_row`.
  Absent means NQC's `verdict.sh` layout. `"pytest"` reads only pytest
  `FAILED` / `ERROR` lines and the pytest result line. An object takes an
  optional `profile` (`default` or `pytest`) plus overrides: `case` (stamp
  regex, two groups: status with `_` for spaces, name), `totals` (three
  groups: total, fail, not assessed), `name_start` / `name_end` (the
  fixed-column name field of unstamped lines) and `reprint` (the line that
  starts the summary table) and `noise` (a list of regexes that
  `castor_compare_runs` ignores when it diffs trial lines, for counters
  like `group=\d+`; none may match the empty string). `null` turns `case`,
  `totals` or `reprint` off. Compare, triage and explain-row read only this key, so they work
  without `allow` or `CASTOR_RUN_ROOTS`; a bad `log_format` is an error
  there.
- `env_allow` lists the variable names a caller may set per run (see
  below). Absent or empty means none. A name must be a plain variable name
  and may not be `NVIDIA_*`, `CASTOR_*`, `LD_*`, `DYLD_*`, `BASH_FUNC_*`,
  or one of `PATH`, `HOME`, `IFS`, `ENV`, `BASH_ENV`, `SHELL`, `SHELLOPTS`,
  `BASHOPTS`, `PS4`, `PROMPT_COMMAND`, `CDPATH`, `GLOBIGNORE`.
- the castor registration lists the root in `CASTOR_RUN_ROOTS`
  (colon-separated absolute paths). `cwd` must be an entry itself or a git
  worktree of one (list the main repo and every lane is covered), never a
  folder under one, so a cloned repo cannot authorize itself.
  `CASTOR_RUN=0` turns the tier off, the overdue command included. POSIX
  only.

The command runs as `bash -c '<source env>; exec "$@"'` with the arguments
as positional parameters (no shell parsing), in its own session, with
every `NVIDIA_*` and `CASTOR_*` variable removed. `castor_run_start`'s
`env` adds variables on top of that, only names in `env_allow`: values are
printable ASCII without `$` or backtick, at most 500 characters, at most 20
variables, and a value beats a server variable of the same name. The env
file is sourced after, so it can still override them. The variables are
kept in the run record, shown by `gemini_poll` and as `env=NAMES` in
`castor_runs`; do not put secrets in them. **Castor never kills,
signals or restarts a process**: a stop flag, `max_hours` or an overdue run
ends the watching, not the run, and the result gives the `pid`. Records and
captures live in `~/.cache/claude-castor/runs/` (30 days), so a run
survives a server restart: `gemini_poll` reattaches when the pid is alive
with its recorded start time, else finishes the run from its capture
(`exit_code` null; `lost` with no table). A monitor holds an exclusive
`flock` on `runs/<id>.lock`, so when several Claude sessions (each with
its own Castor) poll one run, only one watches it.

| Tool | Signature | Purpose |
|---|---|---|
| `castor_run_start` | `(cwd, command, expect_minutes, triage=True, label=None, env=None)` | Start a run; collect it with `gemini_poll("run-...")`. Refuses past `max_running` live processes under the root (running, or stopped / given up but still alive), naming them |
| `castor_runs` | `(cwd=None)` | Runs on record, newest first: id, status, label, seconds, verdict |
| `castor_compare_runs` | `(cwd, logs, rows=None, same_code=False)` | Two to 12 suite logs (oldest first, inside `cwd` or any worktree of its repo, labelled by their `YYYYMMDD-HHMMSS` stamp) row by row. Each row (name and occurrence) gets a class, first match wins: `flaky` (PASS and FAIL with `same_code=True`, or two or more switches), `regressed` / `fixed` (one switch), `failing`, `not_assessed`, `numbers_changed` (the detail's `N of M` counts differ; other integers are measurements and ignored), `missing` (absent from a later run, or with a gap), `new` (absent from the first runs, present from some run on), `same` (counted, not listed). Text: the rows by class, a line per run and the trial lines that differ from the first run's, with decimals and clock stamps (`02:41`, `02:41:07`) masked as timings, plus any `log_format.noise` matches (at most 12 per run). Trial lines a later run lacks are one note per row (`(no trial lines 3 to 6; run1 trial line 3: ...)`; the numbers count the row's trial lines, not lines of the log file), not one per line. A differing line that shows under two or more rows of one run is listed once, after the rows, under "lines shared by several rows". No model call (its NIM reading was cut: the free models cannot tell harness from game faults). Runs anywhere, not only POSIX |

`gemini_poll` on a run id returns progress (`run`, `status`, `elapsed_s`,
`pid`, `flags`, `overdue`) and then the result: `status` (`done`,
`stopped`, `gave_up`, `lost`), `command`, `cwd`, `exit_code`, `wall_s`,
`verdict` (exit 0 passed, 1 failed, 2 inconclusive, else from the totals),
`totals`, `rows` (FAIL and NOT ASSESSED rows, from the printed log when it
resolves inside the root, else from the console), `table` (the last
`--- verdicts ---` block), `log`, `flags`, `overdue`, `triage` (phase 1's
triage JSON when a row failed and `triage=True`, or an error object) and
`pid` for `stopped` / `gave_up`.
