# Tests

Self-contained test suites for the task-status board: the server and its live stream, the `taskctl` CLI,
the `tasks.sh` launcher, the skill, and the web UI. Python standard library and bash only (a real `ffmpeg`,
when there is one, adds checks).

## Running

```sh
tests/run_all.sh                 # every suite, then a PASS/FAIL/SKIP table (about 10 minutes)
tests/run_all.sh cli ufw         # only these suites
tests/run_all.sh --list          # the suite names
tests/run_all.sh -v server       # stream the output, passing checks included
tests/run_all.sh -j 4            # up to 4 suites at once (about 5 minutes; timing checks get less slack)
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
| `ffmpeg` and `ffprobe` with libx264 (or `FFMPEG=`, `FFPROBE=`) | `stream` (its real-ffmpeg phase, and the parsers on ffmpeg's own output), `ui` (the `streamplay` case: real decoding in Firefox; it also needs a Firefox that can decode H.264) |
| `openssl` (a self-signed certificate) | `tls`, `launcher` (the HTTPS options), `stream` (the media route over https), `ui` (the https link on an insecure page) |
| `git` with the v1 release commit `c57f075` | `server_v2` (the migration of a database the v1 server made) |
| `zsh`, `fish` | `install` (the install flow is run in each shell that is installed; bash always) |
| `shellcheck` | `launcher` (lints `tasks.sh`) |
| `ss` | `launcher` (the busy-port error names the other program) |
| IPv6 loopback `::1` | `ipv6` (dual-stack and `--host` cases) |
| an IPv6 link-local (`fe80::`) address | `ipv6` (link-local literal cases) |

## Isolation

The suites never touch a real board or a real configuration:

- Every server is a scratch one on a free port (a bind to port 0; never 8765) with a temp `--db`,
  `--pidfile` and `--config` (its own settings file). It is stopped at the end, pass or fail.
- `HOME` and `CLAUDE_CONFIG_DIR` point into a temp directory, and every `TASKS_*` variable is unset,
  except `TASKS_SPOOL_DIR`, which points taskctl's offline queue into a temp directory too (so a check
  against a dead board never queues into the real `~/.cache/taskctl`); `XDG_CACHE_HOME` is unset.
- A `TASKS_URL` exported for everyday use (pointing at a real board) never reaches a test: `run_all.sh`
  and every suite drop it before anything runs.
- The launcher suites run a copy of the checkout (`tasks.sh` plus a copy of `tasks/`) in a temp
  directory, so pidfiles, logs and the default database land there.
- The launcher's firewall and LAN detection read fake files: the copied `tasks.sh` has its
  `/etc/ufw` paths pointed at a temp tree, and a fake `systemctl` and `ip` come first on `PATH`,
  so the LAN address is always `192.0.2.10` (a documentation address). No result depends on the
  host's firewall, and nothing runs `sudo` (a fake one records any attempt).
- Checks that need files to be missing (the "template missing" errors) use a temp copy of
  `server.py`, never the real tree.
- The stream suites reach no other host. The board's `ffmpeg` is a stand-in (`lib/fake_ffmpeg.py`, copied
  into a `fakebin` directory under the temp dir and put first on that board's `PATH`) that plays what its URL
  asks for. Where a real ffmpeg runs, it reads loopback sources only (a stand-in upstream, or one of ffmpeg's
  own listeners on `127.0.0.1`); a real source is used only when you pass `UI_STREAM_URL`.
- All state lives in `mktemp`/`tempfile` directories (under `$TMPDIR` if set), removed at exit
  together with any process whose command line mentions them.

## Suites

| Suite | File | Covers |
|---|---|---|
| `taskctl_units` | `test_taskctl_units.py` | `taskctl.py` imported directly: duration and percent parsing, formatters (waiting included), id classification, client-made ids, global option splitting, timeout and URL resolution, unusable URLs, the CLAUDE.md rule merge, the `run` output monitor; the offline queue's records (qids), coalescing and paths, its locks (a held one waited for about a second, one replay at a time), the refused-create list, connect-vs-sent error classification (a local firewall's EPERM/EACCES queued), which board a skill belongs to (`norm_url`), the changelog's "what's new" |
| `server` | `test_server.py` | the wire contract in the `server.py` docstring: every route, response shape and key order, validation and truncation, id errors and hints, progress and completion rules, request cascades and reopening, the 500-task cap, the Origin guard, delete, list order, counts and filters, stale, 503 while the database is locked, expiry, EADDRINUSE, pidfile handling, logging; rendering (the four tokens) against fixture templates; every file route with its file missing; `--public-url` and retention; argument errors |
| `server_v2` | `test_server_v2.py` | the v2 additions: `X-Tasks-Version` and `X-Tasks-Docs` on every response (http.server's own errors too), `docs_version` against an independent hash and after edits, `/api/changelog`; client-made request ids and the idempotent create; attention on tasks and requests (every state rule, auto-clear, `waiting`, `tasks_waiting`, `counts.waiting`, the stale and expiry exemptions); the events each change writes, their order and shape, `/api/events` (baseline, cursor, limit, truncation, pruned detection); replayed writes (`X-Tasks-Replay`, `at` and its clamping); the settings file (defaults, PUT validation, the atomic write, hand edits, a broken file, a volume too large for a float, DELETE, `--config`, `TASKS_CONFIG`, the default path); event pruning; the in-place migration of a database made by the v1 server (taken from git) |
| `stream` | `test_stream.py` | the live stream's server side (`GET`, `POST` and `DELETE /api/stream`, `GET /api/stream/media`) in four phases. Units: the URL rules and redaction, the ffmpeg command line per scheme and probe level (no `-rw_timeout` on rtsp, no `-timeout` on rtmp, network protocols only), ffmpeg's error text against stderr it really wrote, the MP4 parsing (every way a keyframe fragment can be marked, the splitter fed a byte at a time). API, with the fake ffmpeg: shapes and key order, validation, replace / keep / close, the Origin guard, headers, a password shown to no one, eight POSTs at once. The media route and the supervisor against the fake: body order, late joiners, audio first as on the real source, HEAD, the 16-viewer cap, a reader that stops reading, replace and close leaving no process, the backoff and its reset, stalls and stubborn processes, garbage and truncated output, the probe ladder, audio that is silent or stops, no ffmpeg yet, persistence across a restart (mode 0600, a corrupt file), SIGTERM with viewers attached, a SIGKILLed board, polls with 16 viewers, https. And with the real ffmpeg (skipped without it), over loopback: paced MPEG-TS sources, refusals and a black hole, a redirect to `file:`, five viewers on one upstream connection, rtmp / tcp / srt / rtsp through ffmpeg's own listeners, orphans after a SIGKILL |
| `tls` | `test_tls.py` | the optional HTTPS listener with a self-signed certificate: dual-stack, the same routes and database, agents kept on plain http in what it renders (BASE_URL on the http port, human links https), a taskctl downloaded over https, plain HTTP and idle clients on the TLS port, `TASKS_TLS_*`, every argument, certificate and port error |
| `cli_offline` | `test_cli_offline.py` | `taskctl` with no board: usage errors (exit 2) for every subcommand, soft failures (exit 0 plus one warning), `--strict` (3 unreachable, 1 for `install-rule` and `update`), `new`'s client-made ids while the board is down, the `offline` sentinel, `run` without a board (exit codes 126/127/143), `TASKS_TIMEOUT` and `--timeout`, unusable URLs, `-h` for every subcommand, the sh wrapper under an exported `CDPATH`, through symlinks, and without a usable Python |
| `cli` | `test_cli.py` | `taskctl` against a scratch board through the sh wrapper: every subcommand and alias with every aggregate recomputed from the task list (waiting and stale included), `--eta` forms, the start nudge, show/list output, request cascades, `run` (pass-through, `--percent-regex`, signals, a request closed mid-run, the heartbeat, a background grandchild that outlives `run`), stale and expiry, `ask`/`resume` (and `waiting` in show and list), `usage`, `changelog`, `api`, `flush`, `install-rule`, the served and downloaded `taskctl` |
| `spool` | `test_spool.py` | taskctl's offline queue end to end: the spool file (path, format, mode, one per board URL, the private temp fallback, a planted symlink refused), queued writes replayed in order at their own times by the next command or `flush`, coalescing, the exact warnings, `--strict`, a live write after a backlog, 8 parallel processes under the lock, connect failures (refused, DNS, connect timeout) queued and a read timeout not, a 5xx stopping a replay, a 4xx dropping a write (and a refused create its request's writes, then and in every later command, for 7 days), one process replaying while the others never wait (reads go direct, writes queue behind it, in order), a stuck replay lock and a stuck queue lock, `run` against a dead board, `add` never queued |
| `self_update` | `test_self_update.py` | an installed skill (in a temp config dir) updating itself when a reply's `X-Tasks-Docs` changes: the three files, their modes, the CLAUDE.md rule where one is installed, the exact notice with the newer changelog entries, stdout and exit codes untouched; `TASKS_NO_UPDATE`, `update`, `update --force`; only its own board updates it (another board via `--url` or `TASKS_URL` changes neither the skill nor the rule, `update` refuses it, `update --force` moves the skill there), whatever the spelling of its URL; the refusals (a repo copy, its board down, a read-only dir, a served SKILL.md or taskctl.py that fails validation, no changelog) |
| `ipv6` | `test_ipv6.py` | dual-stack listening, the Origin/Host guard and base URLs with IPv6 literals (zones, IPv4-mapped), client addresses in the log, every `--host` form, the IPv4-only fallback, EADDRINUSE, http.server's own rejections (405/501/400/505/414/431) as JSON over IPv4 and IPv6, log injection |
| `concurrency` | `test_concurrency.py` | 40 reader threads polling the list, a detail page and `/api/events` from their own cursor while a writer posts progress (`CONC_SECONDS`, default 5): no errors, the last write wins, every reader sees every event once and in order; latencies are printed |
| `launcher` | `test_launcher.sh` | `tasks.sh`: argument errors (the TLS options' all-or-none rule too), `--bg`/`--stop`/`--status`/foreground, the banner (https and settings lines included), the health fallback, stale and recycled pidfiles, a busy port, a start failure, relative `--db`/`--config` and `TASKS_*`, `--public-url`, the settings file, the HTTPS listener with a self-signed certificate (the ufw box for its port), the LAN-open path, container bridges, SIGINT/SIGTERM, the SIGKILL path, a missing server, an old Python, a symlinked launcher |
| `install_skill` | `test_install_skill.sh` | `tasks.sh --install-skill`: files and modes, the baked URL, the rule (with its ask/resume line), the permission recommendation (one allow rule for `taskctl *`, ask rules for `run` and `update`, also behind a global option, as pasteable JSON), idempotence, existing CLAUDE.md content, a bad `TASKS_URL`, a symlinked or incomplete skill dir, the HOME fallback, a relative `CLAUDE_CONFIG_DIR`, a failed download keeping the old install |
| `ufw` | `test_ufw.sh` | the launcher's IPv4 and IPv6 firewall detection against fake ufw files: every rule shape, `IPV6=no`, unreadable rule files, ufw disabled or accepting, and what the banner says for each listen address |
| `install` | `test_install.sh` | a remote agent following the served usage text verbatim in bash, zsh and fish: crafted Host headers, the one-line install (also through a dead proxy), `install-rule`, the installed `taskctl` with only its baked URL, the `CLAUDE_CONFIG_DIR` variant; the usage text's curl examples run in order against the board |
| `skill_examples` | `test_skill_examples.sh` | every `taskctl` example in `tasks/skill/SKILL.md`, run against a scratch board (under zsh when installed), plus its front matter (what `allowed-tools` pre-approves, and that `run` and `update` are not), its sections and Commands table |
| `ui` | `test_ui.py` + `ui/` | the dashboard in headless Firefox at phone (390px) and desktop (1280px) widths, light and dark: no horizontal overflow, meta-line separators, every number against the API, agent text never becoming markup, the ticker, deep links, not-found views, the empty board, keyed reconcile, polling while hidden (every 5s), the offline banner, Mark done, Delete, deletion behind the page (its polls stop, the alerts go on), Clear; and v2: needs input (badge, question, pulse, reduced motion, sorting, the summary), the alert engine (notifications, chimes and their 2s gap, device overrides, replayed events, the title and favicon, stale), an outage, the reminder, audio held by the browser until a click (nothing stacked, one chime on the click), the settings panel, the notification permission flow, an insecure page; and, in the `stream` group, the live stream (below) |

## How the UI suite works

`ui/harness.py` runs the real server with test-only hooks. `test_ui.py` seeds it through the API
(and backdates the few rows that must look old), then loads pages in headless Firefox with one
`ui/*.js` script injected. The script checks the page from inside and posts a JSON result, which
`test_ui.py` asserts on. Firefox's `--screenshot` mode exits once the page has loaded; a slow image
holds the load event until the script is done. `UI_SHOTS_DIR=DIR` keeps every screenshot for a
human look (and the v2 cases' raw results, as `v2_results.json`).

The v2 cases run on boards of their own, one per case that writes, so no case sees another's events.
Page options the harness understands: `__stub=1` replaces `Notification` and `AudioContext` with
recorders (what was notified, closed and played, with timestamps; a chime is known by its first
tone's frequency), `__suspended=1` starts that AudioContext held (as before any click) until the
script sets `__rec.gesture`, `__perm=` sets the stub's permission, `__local=<json>` presets this
browser's alert settings, and `__insecure=1` makes `window.isSecureContext` false. The stream cases
add `__shim=1` (the shim, but no test script and no slow image), `__media=stub` with
`__autoplay=allowed|muted|blocked`, `__vw`/`__vh`, `__mse=ms|mms|both|none`, `__open=never` and
`__rate=N`, `__keep=1` (the shim leaves localStorage alone), `__skew=S` (this browser's clock runs S
seconds ahead of the board's), `__tune=STREAM_X:N` (a page constant given another value, so a timer
of minutes is tested in seconds) and `__shot=1`, and two hooks, `/__frame?__test=NAME` and
`/__stream?url=...|raw=...|off=1` (which makes `/api/events` carry a stream object of the test's
own, an odd one included). `ui/harness.py`'s docstring documents them all. Two hooks change the
board under the page: `/__offline?ms=N` drops every connection without an `X-Test` header for N ms
(the page sees the board go away while the script, through `__t.api`, still reaches it), and
`/__backdate?rid=R&secs=N` ages a request so it goes stale. A case may also pass Firefox preferences
(reduced motion, for one).

The `stream` group runs on boards of its own too (a board has one stream), each with `lib/fake_ffmpeg.py` first
on its `PATH` as `ffmpeg`, so a stream's URL (`http://fake.invalid/live`, `/hang`, `/audio`, `/hevc`,
`/live?die=30`...) says how the "source" behaves. A case loads its boards in iframes of a `/__frame` page, so any
window size and orientation can be had, and the slow image holds only that page's load event; each case closes
its stream and its frames before it posts, so no media request is left in flight. The browser's MediaSource and
media element are replaced by a stub (`__media=stub`, `ui/_media.js`) that models what the page depends on, so
the page's real player code runs against it. The cases: `streamlayout` (the pane in every state and orientation,
exact sizes, the two lines of text), `streamaudio`, `streamheld` (sound the browser holds back, and the click that
lets it out), `streampanel` (the settings block), `streamswap`, `streamretry`, `streammulti`, `streamhevc` (a codec
the browser cannot play), `streamplayer` (the first seek, the jump to the live edge, trimming, a full buffer,
stalls, ManagedMediaSource), `streamfault`, `streamlife` (hidden tabs, the page cache), `streamnav` (scroll position
and deep links in the split window), `streamoffline`, `streamodd` (a board that answers oddly), and `streamplay`
(a real source through the real MediaSource, when ffmpeg can encode H.264 and this Firefox can decode it);
`stream_check_*` repeat the generic page check with a stream open. `test_ui.py stream` runs just this group
(`base` and `v2` are the others), and `UI_STREAM_URL=URL` adds `streamreal`: that real source, through a scratch
board with the real ffmpeg.

## Layout

```
run_all.sh           the runner
lib/harness.py       Python helpers: Suite (check, skip, summary), ScratchServer (with .events() and
                     .stream_file), free_port, call (http or https)/api, temp dirs and HOME, clean_env (and
                     its temp spool dir), tasks_copy, docs_version, self_signed_cert, link_local; for the
                     stream suites fake_ffmpeg_dir / install_fake_ffmpeg / real_ffmpeg_dir, FFMPEG and
                     FFPROBE, make_ts / ts_from / ts_without_audio / probe / split_boxes (ffmpeg's own
                     output), FakeUpstream (an MPEG-TS server), stream_get (a media reader), children_of and
                     find_procs (leftover processes)
lib/fake_ffmpeg.py   a stand-in for ffmpeg: plays the run its URL asks for as fragmented MP4 and writes
                     ffmpeg's kind of stderr (its docstring lists the modes and parameters); also a library
                     of MP4 box builders (box, build_init, build_fragment, sample_stream, ...)
lib/common.sh        bash helpers: check/run/has, finish, start_server, free_port, and the launcher
                     fixtures (make_root, make_fakes, fake_ufw)
fixtures/fake_tasks  templates with tokens and edge cases, for the server's rendering checks
ui/                  the UI harness and its page scripts (_lib.js: their helpers; check.js: the generic
                     page check; engine, detailengine, offline, remind, held, panel, perm, insecure, motion:
                     the v2 cases; _media.js: the media stub; stream*.js: the live stream's cases)
test_*.py, test_*.sh the suites
```

## Settings

| Variable | Effect |
|---|---|
| `TESTS_VERBOSE=1` | print passing checks too (`run_all.sh -v` sets it) |
| `CONC_SECONDS` | length of the concurrency run (default 5) |
| `FIREFOX` | the Firefox binary for the UI suite |
| `FFMPEG`, `FFPROBE` | the ffmpeg and ffprobe the stream suites use as the real ones (default: from `PATH`) |
| `UI_SHOTS_DIR` | keep the UI screenshots in this directory (the stream cases then leave their frames open, and their raw results go to `stream_results.json`) |
| `UI_STREAM_URL` | also play this real stream source in the UI suite (`streamreal`); off by default |
| `TMPDIR` | where the temp directories go |
