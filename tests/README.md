# Tests

Self-contained test suites for the task-status board: the server, the `taskctl` CLI, the `tasks.sh`
launcher, the skill, and the web UI. Python standard library and bash only.

## Running

```sh
tests/run_all.sh                 # every suite, then a PASS/FAIL/SKIP table (about 3 minutes)
tests/run_all.sh cli ufw         # only these suites
tests/run_all.sh --list          # the suite names
tests/run_all.sh -v server       # stream the output, passing checks included
tests/run_all.sh -j 4            # up to 4 suites at once (faster; timing checks get less slack)
```

`run_all.sh` exits 0 when nothing failed. A suite that cannot run here (an optional tool is
missing) is a SKIP, not a failure, and its reason is printed under the table. A failing suite's
output is printed after the table and its log kept.

Every suite also runs on its own, from any directory:

```sh
python3 tests/test_server.py
bash tests/test_launcher.sh
```

Each prints `FAIL` lines as they happen, a summary, and a final
`RESULT: passed=N failed=N skipped=N` line; it exits 0 (pass), 1 (fail) or 77 (skipped).

## Requirements

- Python 3.10 or newer, SQLite 3.37 or newer (what the server needs), bash, curl.
- Linux: the launcher and some CLI checks use `/proc` and `setsid`, as `tasks.sh` itself does.

Optional; the checks that need one are skipped without it:

| Tool | Used by |
|---|---|
| `firefox` (or `FIREFOX=/path/to/firefox`) | `ui` |
| `zsh`, `fish` | `install` (the install flow is run in each shell that is installed; bash always) |
| `shellcheck` | `launcher` (lints `tasks.sh`) |
| `ss` | `launcher` (the busy-port error names the other program) |
| IPv6 loopback `::1` | `ipv6` (dual-stack and `--host` cases) |
| an IPv6 link-local (`fe80::`) address | `ipv6` (link-local literal cases) |

## Isolation

The suites never touch a real board or a real configuration:

- Every server is a scratch one on a free port (a bind to port 0; never 8765) with a temp `--db`
  and `--pidfile`. It is stopped at the end, pass or fail.
- `HOME` and `CLAUDE_CONFIG_DIR` point into a temp directory, and every `TASKS_*` variable is unset.
- The launcher suites run a copy of the checkout (`tasks.sh` plus a copy of `tasks/`) in a temp
  directory, so pidfiles, logs and the default database land there.
- The launcher's firewall and LAN detection read fake files: the copied `tasks.sh` has its
  `/etc/ufw` paths pointed at a temp tree, and a fake `systemctl` and `ip` come first on `PATH`,
  so the LAN address is always `192.0.2.10` (a documentation address). No result depends on the
  host's firewall, and nothing runs `sudo` (a fake one records any attempt).
- Checks that need files to be missing (the "template missing" errors) use a temp copy of
  `server.py`, never the real tree.
- All state lives in `mktemp`/`tempfile` directories (under `$TMPDIR` if set), removed at exit
  together with any process whose command line mentions them.

## Suites

| Suite | File | Covers |
|---|---|---|
| `taskctl_units` | `test_taskctl_units.py` | `taskctl.py` imported directly: duration and percent parsing, formatters, id classification, global option splitting, timeout and URL resolution, unusable URLs, the CLAUDE.md rule merge, the `run` output monitor |
| `server` | `test_server.py` | the wire contract in the `server.py` docstring: every route, response shape and key order, validation and truncation, id errors and hints, progress and completion rules, request cascades and reopening, the 500-task cap, the Origin guard, delete, list order, counts and filters, stale, 503 while the database is locked, expiry, EADDRINUSE, pidfile handling, logging; rendering against fixture templates; every file route with its file missing; `--public-url` and retention; argument errors |
| `cli_offline` | `test_cli_offline.py` | `taskctl` with no board: usage errors (exit 2), soft failures (exit 0 plus one warning), `--strict` (3 unreachable, 1 for `install-rule`), the `offline` sentinel, `run` without a board (exit codes 126/127/143), `TASKS_TIMEOUT` and `--timeout`, unusable URLs, `-h` for every subcommand, the sh wrapper under an exported `CDPATH`, through symlinks, and without a usable Python |
| `cli` | `test_cli.py` | `taskctl` against a scratch board through the sh wrapper: every subcommand and alias with every aggregate recomputed from the task list, `--eta` forms, the start nudge, show/list output, request cascades, `run` (pass-through, `--percent-regex`, signals, a request closed mid-run, the heartbeat, a background grandchild that outlives `run`), stale and expiry, `install-rule`, the served and downloaded `taskctl` |
| `ipv6` | `test_ipv6.py` | dual-stack listening, the Origin/Host guard and base URLs with IPv6 literals (zones, IPv4-mapped), client addresses in the log, every `--host` form, the IPv4-only fallback, EADDRINUSE, http.server's own rejections (405/501/400/505/414/431) as JSON over IPv4 and IPv6, log injection |
| `concurrency` | `test_concurrency.py` | 40 reader threads polling the list and a detail page while a writer posts progress (`CONC_SECONDS`, default 5): no errors, the last write wins; latencies are printed |
| `launcher` | `test_launcher.sh` | `tasks.sh`: argument errors, `--bg`/`--stop`/`--status`/foreground, the banner, the health fallback, stale and recycled pidfiles, a busy port, a start failure, relative `--db` and `TASKS_*`, `--public-url`, the LAN-open path, container bridges, SIGINT/SIGTERM, the SIGKILL path, a missing server, an old Python, a symlinked launcher |
| `install_skill` | `test_install_skill.sh` | `tasks.sh --install-skill`: files and modes, the baked URL, the rule, the allow-rule block, idempotence, existing CLAUDE.md content, a bad `TASKS_URL`, a symlinked or incomplete skill dir, the HOME fallback, a relative `CLAUDE_CONFIG_DIR`, a failed download keeping the old install |
| `ufw` | `test_ufw.sh` | the launcher's IPv4 and IPv6 firewall detection against fake ufw files: every rule shape, `IPV6=no`, unreadable rule files, ufw disabled or accepting, and what the banner says for each listen address |
| `install` | `test_install.sh` | a remote agent following the served usage text verbatim in bash, zsh and fish: crafted Host headers, the one-line install (also through a dead proxy), `install-rule`, the installed `taskctl` with only its baked URL, the `CLAUDE_CONFIG_DIR` variant |
| `skill_examples` | `test_skill_examples.sh` | every `taskctl` example in `tasks/skill/SKILL.md`, run against a scratch board (under zsh when installed), plus its front matter and Commands table |
| `ui` | `test_ui.py` + `ui/` | the dashboard in headless Firefox at phone (390px) and desktop (1280px) widths, light and dark: no horizontal overflow, meta-line separators, every number against the API, agent text never becoming markup, the ticker, deep links, not-found views, the empty board, keyed reconcile, polling while hidden, the offline banner, Mark done, Delete, deletion behind the page, Clear |

## How the UI suite works

`ui/harness.py` runs the real server with test-only hooks. `test_ui.py` seeds it through the API
(and backdates the few rows that must look old), then loads pages in headless Firefox with one
`ui/*.js` script injected. The script checks the page from inside and posts a JSON result, which
`test_ui.py` asserts on. Firefox's `--screenshot` mode exits once the page has loaded; a slow image
holds the load event until the script is done. `UI_SHOTS_DIR=DIR` keeps every screenshot for a
human look.

## Layout

```
run_all.sh           the runner
lib/harness.py       Python helpers: Suite (check, skip, summary), ScratchServer, free_port, call/api,
                     temp dirs and HOME, clean_env, tasks_copy, link_local
lib/common.sh        bash helpers: check/run/has, finish, start_server, free_port, and the launcher
                     fixtures (make_root, make_fakes, fake_ufw)
fixtures/fake_tasks  templates with tokens and edge cases, for the server's rendering checks
ui/                  the UI harness and its page scripts
test_*.py, test_*.sh the suites
```

## Settings

| Variable | Effect |
|---|---|
| `TESTS_VERBOSE=1` | print passing checks too (`run_all.sh -v` sets it) |
| `CONC_SECONDS` | length of the concurrency run (default 5) |
| `FIREFOX` | the Firefox binary for the UI suite |
| `UI_SHOTS_DIR` | keep the UI screenshots in this directory |
| `TMPDIR` | where the temp directories go |
