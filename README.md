# agent-task-display

A small live status board for [Claude Code](https://claude.com/claude-code) agents. Stop asking an
agent "status?". It reports to a web page as it works: one entry per thing you asked for, one line per
step or subagent, each with a percent, a hint of what is happening right now, and an ETA. You keep the
page open on another screen or on your phone. The board is a Python standard-library HTTP server with
SQLite and a single-file web UI. Agents report with `taskctl`, a small CLI, and a Claude Code skill
tells them when to use it. Agents on other machines can install the skill and CLI from the board with
one `curl` line.

![Dashboard: active and finished requests](docs/list.png)

## Features

- **Live progress**: requests and their tasks, with percent, hint, ETA, elapsed time, X/Y counts and
  stale detection, on a dashboard that works on a phone and follows the system's dark mode.
- **Needs input**: an agent that stops to ask you something flags it first (`taskctl ask`). The
  request pulses, shows the question and sorts to the top until the agent resumes.
- **Notifications**: per kind of event, a row highlight, an unread counter and blinking tab title,
  a chime and a desktop notification. Board-wide defaults live in a settings file; each browser can
  override them. Browsers on other machines need the optional [HTTPS listener](#https-for-notifications).
- **Works offline**: when the board is unreachable, `taskctl` queues the reports on the agent's machine
  and replays them, with their original times, once the board is back.
- **Keeps agents current**: when the board's agent instructions change, every installed `taskctl`
  updates its skill on its next call and tells the agent what is new.
- **One-line setup** for agents on other machines, and a launcher (`tasks.sh`) for the board host.

## Contents

- [Features](#features)
- [Concepts](#concepts)
- [Quickstart (board host)](#quickstart-board-host)
- [Agents on other machines](#agents-on-other-machines)
- [Using it from Claude Code](#using-it-from-claude-code)
- [The dashboard](#the-dashboard)
- [Notifications and settings](#notifications-and-settings)
- [taskctl reference](#taskctl-reference)
- [HTTP API](#http-api)
- [Networking and security](#networking-and-security)
- [Data and retention](#data-and-retention)
- [Running the tests](#running-the-tests)
- [Repository layout](#repository-layout)

## Concepts

- **Request**: one per user request, such as "Build the app and deploy it to staging". It has an id
  like `k3m9qa`, a page at `/r/k3m9qa`, and an origin (`taskctl` sets it to `host:directory`).
- **Task**: one per step, or one per agent in a workflow. Its id is the request id plus a sequence
  number, such as `k3m9qa-3`. The agent plans tasks when it opens the request and can add more later.
- **Task status**: `pending` (the UI shows it as *queued*), `running`, `done`, `failed` or `cancelled`.
  A request is `running`, `done` or `failed`.
- **Stale** is derived, not stored. Running work shows as stale when it has sent no update for 10
  minutes (`--stale-after`). Work inside its ETA, or less than 60 s past it, is not stale.
- **Waiting** (needs input): a request, or one of its tasks, whose agent has asked you something and
  is waiting for your answer. It carries the question and the time it was asked. Waiting work is never
  stale, and a waiting request never expires. The flag clears when the agent resumes, reports
  progress on that task, or closes it.
- **Percent**: the request's percent is the average over its tasks, with done tasks counted as 100.
  **X/Y** counts done tasks out of all tasks. Cancelled tasks are left out of both.
- **Elapsed** runs from when a task started, or from when the request was created, until it finishes.
  **ETA**: a task's ETA is whatever the agent last reported. A request's ETA is the latest ETA among
  its running tasks.
- **Closing a request cascades**: its pending tasks become cancelled, and its running tasks take the
  request's status. Adding a task to a closed request reopens it.

## Quickstart (board host)

Requirements: Python 3.10+ (with SQLite 3.37+) and `curl` on the machine that runs the board. Agent
machines need only Python 3.8+ and `curl`. `tasks.sh` is a bash script for Linux. On other systems,
run `python3 tasks/server.py` directly.

```sh
git clone https://github.com/RetroZelda/agent-task-display.git
cd agent-task-display
./tasks.sh --bg              # start the board in the background (port 8765); prints its URLs
```

Open `http://localhost:8765/`, or `http://<lan-ip>:8765/` from another device.

```sh
./tasks.sh --install-skill   # starts the board if needed, then installs the skill and the rule
./tasks.sh --status          # URLs, health, database, settings, log, skill and rule state; exit 3 if not running
./tasks.sh --stop            # SIGTERM, then SIGKILL after 5 s
./tasks.sh                   # run in the foreground instead (Ctrl-C stops it)
```

`--install-skill` writes to `${CLAUDE_CONFIG_DIR:-~/.claude}`:

- `skills/task-status/SKILL.md`, `taskctl` and `taskctl.py`. This is the skill plus its CLI, which
  has this board's URL built in.
- A short rule block in `CLAUDE.md`, between `<!-- task-status:begin -->` and
  `<!-- task-status:end -->`. The rule tells every session when to use the skill. The command creates
  the file if it is missing and updates the block in place, leaving the rest of the file as it was.

It never edits `settings.json`. Instead it prints the recommended permission rules for you to add.
Every report is a Bash call, so without them you get a permission prompt for each report, and
subagents may be refused:

```json
{
  "permissions": {
    "allow": ["Bash(/home/you/.claude/skills/task-status/taskctl *)"],
    "ask": [
      "Bash(/home/you/.claude/skills/task-status/taskctl run *)",
      "Bash(/home/you/.claude/skills/task-status/taskctl --* run *)",
      "Bash(/home/you/.claude/skills/task-status/taskctl update*)",
      "Bash(/home/you/.claude/skills/task-status/taskctl --* update*)",
      "Bash(/home/you/.claude/skills/task-status/taskctl install-rule*)",
      "Bash(/home/you/.claude/skills/task-status/taskctl --* install-rule*)"
    ]
  }
}
```

Add the entries to the `permissions.allow` and `permissions.ask` lists of your
`~/.claude/settings.json` (create the lists if they are missing). The allow rule covers every
`taskctl` command, including the ones a later version adds. `taskctl run` runs an arbitrary command
and `taskctl update` replaces the skill's code, and an ask rule wins over an allow rule, so every
`run` and `update` still asks you first, also with a global option such as `--url` in front of it.
Start a new Claude Code session, or run `/reload-skills`, to load the skill.

Launcher options:

| Option | Environment | Default | Meaning |
|---|---|---|---|
| `--host HOST` | `TASKS_HOST` | `0.0.0.0` | Listen address |
| `--port PORT` | `TASKS_PORT` | `8765` | HTTP port |
| `--db PATH` | `TASKS_DB` | `tasks/data/tasks.db` | SQLite database |
| `--public-url URL` | `TASKS_PUBLIC_URL` | the LAN address, when the LAN can reach it | Base of the links the board hands out |
| `--config PATH` | `TASKS_CONFIG` | `settings.json` next to the database | The [settings file](#the-settings-file) |
| `--tls-port PORT` | `TASKS_TLS_PORT` | off | Also serve [HTTPS](#https-for-notifications) on this port |
| `--tls-cert FILE` | `TASKS_TLS_CERT` | | Its PEM certificate (chain) |
| `--tls-key FILE` | `TASKS_TLS_KEY` | | Its PEM private key |

The three TLS options go together: all of them or none. Relative paths are taken from the directory
you run `tasks.sh` in. The server itself (`python3 tasks/server.py`) takes the same flags and
variables. See `./tasks.sh -h`.

## Agents on other machines

Give the agent this one line. `./tasks.sh --bg` and `./tasks.sh --status` print it with the board's
address filled in (when the board listens on the LAN, i.e. not with `--host 127.0.0.1`):

```
Run `curl -fsS --noproxy '*' --connect-timeout 5 http://<board-host>:8765/api/usage` and follow it.
```

`/api/usage` is written for agents. It has the command that installs the skill and CLI from
`/api/skill/SKILL.md`, `/api/skill/taskctl` and `/api/skill/taskctl.py` (the served `taskctl.py` has
the URL the agent used built in). It also has the rule block from `/api/rule`, the permission rules
to propose, and the HTTP API for agents that are not Claude Code. The instructions tell the agent to
ask its user before it installs anything, before it adds the rule to `CLAUDE.md`, and before any
permission change. The agent never edits `settings.json` itself.

- **Windows**: run the commands from Git Bash (Claude Code's Bash tool), not PowerShell. The
  `taskctl` wrapper tries `python3`, `python` and `py -3`.
- **Proxies**: every command uses `--noproxy '*'`, because a proxy set in `http_proxy` would not reach
  a LAN address.
- **Hostname URLs** can resolve to an IPv6 link-local address, which is common on Windows and with
  `.local` names. If `http://myhost:8765` hangs, use the IP address, or open IPv6 in the firewall
  ([below](#networking-and-security)).

**Installed skills update themselves.** Every board response carries an `X-Tasks-Docs` header, a hash
of the agent-facing files (the skill, `taskctl`, the usage text, the rule and the changelog). When it
differs from the one built into an installed `taskctl`, that `taskctl` downloads the board's current
`SKILL.md`, `taskctl` and `taskctl.py` into its own directory, refreshes the `CLAUDE.md` rule block if
one is installed, and prints a notice on stderr with the new changelog entries, telling the agent to
re-read `SKILL.md`. So after you pull a new version of this repository and restart the board, every
agent picks up the new features on its next report, with nothing to reinstall. It happens at most
once per `taskctl` run, never changes a command's stdout or exit code, and only for an installed
skill (a directory with `SKILL.md` in it), not for `tasks/taskctl` in a clone. Only the board the
skill was installed from (the URL built into its `taskctl.py`, compared with the case of the scheme
and host, a trailing slash and a default port ignored) can update it: a command sent to another
board with `--url` or `TASKS_URL`, a scratch board say, prints a note and leaves the skill and the
rule alone. `taskctl update` updates right away (`--force` even when it is current); it refuses
another board unless given `--force`, which moves the skill to that board. `TASKS_NO_UPDATE=1`
turns updates off.

## Using it from Claude Code

Once the skill is installed, it triggers on its own before multi-step or long work, before background
commands, before subagents or a Workflow, when a prompt contains a task id, and when you ask for
status. The agent opens a request with its tasks planned, gives you the page link, reports at
milestones, and closes every task and then the request. It closes them on failure too. A status
question is answered from `taskctl show`, not from the agent's memory.

**When it needs you**, the agent flags the request before it asks you a question or stops to wait
for your input, and clears the flag when you answer:

```sh
taskctl ask k3m9qa 'Deploy to the eu or the us staging cluster?'   # the request waits for you
taskctl ask k3m9qa-3 'Waiting for your OK to deploy to staging'     # or just one of its tasks
taskctl resume k3m9qa                                               # you answered
```

The dashboard then pulses the request and shows the question, and your
[notification settings](#notifications-and-settings) decide whether it also chimes or sends a
desktop notification. The skill and the `CLAUDE.md` rule both tell the agent to do this.

**Workflows and subagents.** A Workflow script has no network access, and a subagent may not have the
skill loaded. So the orchestrating session does the setup:

1. It opens the request with one `-t` per planned agent:

   ```sh
   taskctl new 'Review the payments PR before merge' -t 'review:correctness' -t 'review:security' -t 'review:performance'
   # stdout: k3m9qa, k3m9qa-1, k3m9qa-2, k3m9qa-3   stderr: the page link
   ```

2. It puts a reporting block in each agent's prompt, with that agent's task id and the absolute
   `taskctl` path:

   ```
   ## STATUS REPORTING (required, the user is watching this live at http://<board-host>:8765/r/k3m9qa)
   You own task k3m9qa-2. Titles and hints go in SINGLE quotes, with no backticks, $ or quote characters:
     /home/you/.claude/skills/task-status/taskctl start k3m9qa-2 'short hint'
     /home/you/.claude/skills/task-status/taskctl progress k3m9qa-2 <0-100> 'what you are doing now' --eta 5m
     /home/you/.claude/skills/task-status/taskctl done k3m9qa-2 'one-line result'   (or: fail k3m9qa-2 'reason')
   Run start first, report at milestones only, and make done/fail your last action.
   ```

3. Agents that are only known at run time register themselves with
   `taskctl add --start k3m9qa 'fix:test_login'` and use the task id it prints.

4. When the agents return, the orchestrator runs `taskctl show k3m9qa`. It fails any task left behind
   by an agent that crashed, then closes the request with `taskctl done k3m9qa 'one-line summary'`.

The full wording, including the template for agents that register themselves, is in
[`tasks/skill/SKILL.md`](tasks/skill/SKILL.md).

## The dashboard

<p>
  <img src="docs/detail.png" alt="A request's page with its tasks" width="66%">
  <img src="docs/phone.png" alt="A workflow request at phone width" width="27%">
</p>

- The list shows the active requests first, then the finished ones, with running, stale, done and
  failed counts at the top. Each request's page lists its tasks, and `/r/<rid>#<tid>` links straight
  to one task.
- **Waiting** requests come first. They and their waiting tasks pulse (a steady outline if your
  system asks for reduced motion), carry a *needs input* badge and show the agent's question. The
  summary line starts with how many are waiting for you.
- The page polls every 1.5 s, and every 5 s in a background tab, so notifications and the tab title
  keep working while you look elsewhere. It follows the system's light or dark theme and works at
  phone width.
- **Mark done**, **Mark failed** and **Delete** act on a request. **Clear** deletes the finished
  requests listed. **Agent setup** opens `/api/usage`. The gear button opens the **Alerts**
  settings, and the bell turns this device's sound on or off.

## Notifications and settings

Each kind of event can use four channels:

- **highlight**: the affected row flashes briefly (a waiting one keeps pulsing).
- **title**: while the tab is in the background, an unread counter `(N)` in its title; while anything
  waits, a blinking "Needs input" title and an alert favicon.
- **sound**: a short chime, synthesised in the browser: one for a question, one for a failure, one for
  success and one for the rest. The question chime repeats every 2 minutes while something waits
  (the *remind* interval; 0 turns it off).
- **notify**: a desktop notification. Clicking it focuses the tab and opens the request. A question's
  notification stays until you dismiss it.

The defaults:

| Event | highlight | title | sound | notify |
|---|---|---|---|---|
| `waiting` (an agent asked a question) | on | on | on | on |
| `task_failed`, `request_failed` | on | on | on | on |
| `request_done` | on | on | on | on |
| `task_done`, `task_started`, `task_progress`, `request_created` | on | off | off | off |
| `stale` (a request went stale) | on | on | off | off |

The **Alerts** settings (the gear button in the top bar) have two tabs:

- **All viewers (saved on the board)**: the defaults for everyone, saved in the board's
  [settings file](#the-settings-file). **Reset to defaults** deletes the file, which brings the
  defaults above back.
- **This device**: overrides for this browser only, kept in its local storage. Each cell inherits the
  board-wide value until you set it on or off. Sound and notifications are also off on every device
  until you switch them on there, because a browser only plays sound or asks for notification
  permission after you click something. This tab also has the volume, a test sound and the remind
  interval.

Writes replayed from an agent's [offline queue](#offline-queue) highlight their rows but never chime
or notify: they are old news.

### The settings file

The board-wide settings are a JSON file on the board host: `--config PATH` (or `TASKS_CONFIG`),
by default `settings.json` next to the database, so `tasks/data/settings.json`. `./tasks.sh --status`
shows its path, and `/api/health` has it as `config_path`. It does not exist until someone saves the
settings, and the defaults apply until then. The format:

```json
{
  "events": {
    "waiting": {"highlight": true, "title": true, "sound": true, "notify": true},
    "task_done": {"highlight": true, "title": false, "sound": false, "notify": false}
  },
  "sound": {"volume": 0.6}
}
```

The event keys are `waiting`, `task_failed`, `request_failed`, `request_done`, `task_done`,
`task_started`, `task_progress`, `request_created` and `stale`. You can edit the file by hand: leave
out anything you do not want to change (it keeps its default), and open dashboards pick up the
change within seconds. If the file is not valid JSON, or has an unknown key or a value that is not `true` or
`false`, the board uses the defaults and reports the problem (in `/api/settings`, and on the
`settings` line of `./tasks.sh --status`). Saving from the panel rewrites the file in full, with
every key.

### HTTPS for notifications

Browsers show notifications only on a secure page: `https://`, or `http://localhost` on the board host
itself. A browser on your phone or another PC that opens `http://<lan-ip>:8765/` gets the sounds, the
highlights and the tab title, but no notifications. There are two ways to fix that.

**Serve the board over HTTPS as well** (recommended). The board opens a second listener with the same
pages and data; agents keep using the plain http port. With [mkcert](https://github.com/FiloSottile/mkcert)
(packaged by most Linux distributions, Homebrew and Chocolatey), on the board host:

```sh
mkcert -install        # creates a local certificate authority and trusts it on this machine
mkcert -cert-file tasks/data/board.pem -key-file tasks/data/board-key.pem \
    myhost.local myhost 192.168.1.10 localhost 127.0.0.1
./tasks.sh --stop
./tasks.sh --bg --tls-port 8766 --tls-cert tasks/data/board.pem --tls-key tasks/data/board-key.pem
```

- List every name and address you will type in a browser: the certificate is valid for those only.
  `tasks/data/` is gitignored, so the key stays out of the repository.
- Each other device must trust the mkcert authority once. `mkcert -CAROOT` prints the folder with
  `rootCA.pem`; copy that file (never `rootCA-key.pem`) to the device and install it as a trusted
  certificate authority: on Windows in *Manage computer certificates* under *Trusted Root
  Certification Authorities*, on macOS in Keychain Access (then set it to *Always Trust*), on Android
  in the security settings under *Encryption & credentials* > *Install a certificate* > *CA
  certificate*, on iOS as a profile, then enabled under *Settings* > *General* > *About* >
  *Certificate Trust Settings*. Firefox may keep its own list: *Settings* > *Privacy & Security* >
  *Certificates* > *View Certificates* > *Authorities* > *Import*.
- Open `https://<lan-ip>:8766/`. `./tasks.sh --status` prints the https address, and if ufw blocks
  the port it prints the rule to add, as it does for the http port.
- The Alerts settings link to the https address when you open the dashboard over plain http.
- Some mobile browsers, such as Safari on iOS, do not show notifications for an ordinary web page at
  all; there the sound and the tab title still work.

**Or mark the plain http address as secure in Chrome.** No certificate needed, but only for the
browser you change: open `chrome://flags/#unsafely-treat-insecure-origin-as-secure` (in Edge,
`edge://flags/#unsafely-treat-insecure-origin-as-secure`), enter the board's address exactly as you
open it, such as `http://192.168.1.10:8765`, set the flag to *Enabled* and relaunch the browser.

## taskctl reference

The installed CLI is `~/.claude/skills/task-status/taskctl`. From a clone, it is `tasks/taskctl`.

| Command | What it does |
|---|---|
| `new TITLE [-t TASK]...` | Open a request. Prints its id, then one task id per `-t`. |
| `add RID TITLE... [--start]` | Add pending tasks, or one running task with `--start`. Prints their ids. Reopens a closed request. |
| `start TID [HINT]` | Mark a task running. Its elapsed time counts from now. |
| `progress TID PCT HINT [--eta DUR]` | Percent (`45`, `45.5` or `45%`), what is happening now, and an optional ETA (`90`, `90s`, `5m`, `1h30m`, `1:30:00`). Leaving out `--eta` clears the ETA. |
| `ask ID QUESTION` | Flag a request, or one task, as waiting for your input, with the question. A pending task starts. |
| `resume ID` | Clear that flag once the question is answered. |
| `done ID [MSG]` | Close a task, or a whole request (cascades), as done. |
| `fail ID MSG` | Close a task or request as failed, with a reason. |
| `show ID` | A request with its tasks, or one task, including what it waits for. |
| `list [--all]` | Every running request, waiting ones marked. `--all` adds the 20 most recently finished. |
| `ping` | Check the board. Prints its URL, and how many writes are queued. |
| `flush` | Replay the [offline queue](#offline-queue) now. |
| `run TID [--every SEC] [--percent-regex RE] -- CMD...` | Run a command and report its output as it goes (below). |
| `usage` | Print the board's instructions for agents (`/api/usage`). |
| `api METHOD PATH [JSON]` | Call any board route (`PATH` starts with `/api/`) and print the response body. |
| `changelog` | Print what each board version added (`/api/changelog`). |
| `update [--force]` | Update the installed skill and CLI from the board now (above). |
| `install-rule [--file PATH]` | Add or update the rule block in `${CLAUDE_CONFIG_DIR:-~/.claude}/CLAUDE.md`. |

Global options, before or after the subcommand:

- `--url URL`. Otherwise it uses `$TASKS_URL`, then the URL built into the file, then
  `http://127.0.0.1:8765`.
- `--timeout SEC`. Otherwise `$TASKS_TIMEOUT`, then 3 s. An invalid value produces a warning and
  falls back to 3 s.
- `--strict`, or `TASKS_STRICT=1`.

Environment: `TASKS_SPOOL_DIR` (where the offline queue lives) and `TASKS_NO_UPDATE=1` (no
self-update).

The id's shape decides whether `done`, `fail`, `show`, `ask` and `resume` act on a request or a task.
stdout carries only ids, URLs, the `show`/`list` tables and the text `usage`, `changelog` and `api`
print, so scripts can capture them. Notes go to stderr, prefixed `taskctl:`.

**Reporting never breaks the real work.** If the board is unreachable or rejects a call, `taskctl`
prints a warning and exits 0. Any command given the id `offline` does nothing, so captured ids keep
working. If no Python 3.8+ is found, the wrapper behaves the same way. Usage errors always exit 2.
With `--strict`, a rejected call exits 1 and an unreachable board exits 3 (even when the write was
queued). `install-rule` and `update` are setup commands, so their failures exit 1.

**`run`** wraps a command so that background work reports on its own:

```sh
taskctl run k3m9qa-2 --percent-regex '(\d+)/(\d+)' -- ./run_tests.sh
```

It starts the task and passes the command's output through unchanged. It sends the latest output line
as the hint, at most every `--every` seconds (default 10), plus a heartbeat every minute. The regex
takes one group (a percent) or two groups (done and total). When the command exits, `run` closes the
task as done, or as failed with the exit code and the last line of output, and exits with the
command's own status.

### Offline queue

When `taskctl` cannot connect to the board, it saves the write in a queue on the agent's machine and
says so: `taskctl: warning: board unreachable at URL (REASON); queued for replay (N queued)`.

- Queued: `new`, `start`, `progress`, `ask`, `resume`, `done` and `fail`. `new` makes the request
  id itself, so it prints real ids even offline, and later commands on those ids queue behind it.
  `add` needs the board to number the tasks, so offline it still prints `offline` for each id, and
  reads (`show`, `list`) just warn.
- Only a connection that certainly failed (refused, no route, unknown host, blocked by a local
  firewall, a connect timeout) is queued. A timeout after the request was sent may have reached the
  board, so that one is only a warning.
- The next `taskctl` command that reaches the board replays the queue first, in order, marked as a
  replay and with the time each write really happened (`taskctl: replayed N queued writes`). Progress
  superseded by a later write on the same task is dropped, and a write the board rejects is dropped
  with a warning. `taskctl flush` replays it right away; `taskctl ping` shows how many are waiting.
- A request whose queued create the board refused (another request took its id meanwhile, which is
  rare) is remembered for 7 days, and every later write for it is dropped with a warning instead of
  landing on the other request.
- The queue is a file per board URL,
  `${TASKS_SPOOL_DIR:-${XDG_CACHE_HOME:-~/.cache}/taskctl}/spool-<hash>.jsonl` (in the temp
  directory if that is not writable), shared by every agent on the machine. One `taskctl` at a time
  replays it, and nobody waits for that: meanwhile reads go straight to the board, and writes queue
  behind the replay, which sends them too (`taskctl: another taskctl is replaying the queued writes
  ...`, exit 0). The file itself is locked only while it is read or rewritten.

## HTTP API

JSON in and out. Every write is a plain `curl` call, so any agent or script can report without
`taskctl`. The full reference for agents is served at `/api/usage`, and the exact contract, covering
shapes, validation, errors and state rules, is the module docstring of
[`tasks/server.py`](tasks/server.py). Every response carries `X-Tasks-Version` (the API version) and
`X-Tasks-Docs` (the hash of the agent-facing files) headers.

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | The dashboard in a browser. Anything else gets the usage text. |
| GET | `/r/<rid>` | A request's page |
| GET | `/api`, `/api/usage` | Instructions for agents (plain text) |
| GET | `/api/rule` | The `CLAUDE.md` rule block |
| GET | `/api/changelog` | What each API version added, newest first (plain text) |
| GET | `/api/skill/SKILL.md`, `/api/skill/taskctl`, `/api/skill/taskctl.py` | The skill and CLI files (`taskctl.py` has the board URL built in) |
| GET | `/api/health` | `{ok, service, version, docs_version, tls_port, config_path, pid, now, started_at, requests_running}` |
| POST | `/api/requests` | Create a request: `{title, origin?, tasks?: [title], id?}` (an existing `id` with the same title returns that request) |
| GET | `/api/requests` | List: running requests first, then finished ones (`?status=`, `?limit=`) |
| GET | `/api/requests/<rid>` | One request with its tasks |
| DELETE | `/api/requests/<rid>` | Delete a request and its tasks |
| POST | `/api/requests/<rid>/complete` | Close it: `{status?: done\|failed, message?}` (cascades) |
| POST, DELETE | `/api/requests/<rid>/attention` | Flag the request as waiting for input: `{message}`; clear it |
| POST | `/api/requests/<rid>/tasks` | Add tasks: `{title, start?}` or `{titles: [...]}` |
| GET | `/api/tasks/<tid>` | One task |
| POST | `/api/tasks/<tid>/progress` | `{message, percent?, eta_seconds?}` |
| POST | `/api/tasks/<tid>/complete` | `{status?: done\|failed, message?}` |
| POST, DELETE | `/api/tasks/<tid>/attention` | Flag one task as waiting for input: `{message}`; clear it |
| GET | `/api/events` | The event log since a cursor, plus what is waiting and what is stale (`?since=`, `?limit=`) |
| GET, PUT, DELETE | `/api/settings` | The board-wide notification settings: read, change (`{settings: {...}}`), reset |

Requests and tasks have an `attention` field (`{message, since}` or null); a request also has
`waiting` and `tasks_waiting`, and the list's counts have `waiting`. A write sent with the header
`X-Tasks-Replay: 1` and a body field `at` is recorded at that time (within the last 24 hours), which
is how the offline queue keeps the original times.

```sh
curl -sS --noproxy '*' -X POST http://<board-host>:8765/api/tasks/k3m9qa-2/progress \
  -H 'Content-Type: application/json' \
  -d '{"percent": 45, "message": "Compiling 812 of 2000", "eta_seconds": 300}'
```

## Networking and security

- **It listens on every interface.** By default it binds `0.0.0.0`. The socket is dual-stack, so it
  accepts IPv4 and IPv6 on one port, and falls back to IPv4 only when the host has no IPv6. Use
  `--host 127.0.0.1` to keep it on the local machine. The HTTPS listener, when on, binds the same way.
- **There is no authentication.** Anyone who can reach the port can read every request, create or
  close requests, delete them, and change the board-wide settings. Run it only on a trusted LAN.
  Never expose it to the internet. Never put secrets in titles or hints. HTTPS encrypts the traffic
  but does not add a login.
- **Browser writes are guarded.** A POST, PUT or DELETE that carries an `Origin` header is refused
  (403) unless the Origin matches the Host, and the Host is an IP address, `localhost`, the machine's
  hostname or the `--public-url` host. This blocks cross-site requests and DNS rebinding. `curl` and
  `taskctl` send no Origin, so they are unaffected. If you reach the UI under another name, pass it as
  `--public-url` so the UI's buttons keep working.
- **Agent text is shown as text.** Titles, hints, questions and messages are rendered as plain text,
  never as HTML, so a report cannot inject script. A Host header is built into the served files only
  if it is a plain `host[:port]`.
- **Agents stay on plain http.** Over the HTTPS listener, the board address built into the usage
  text and `taskctl.py` is still the plain http one (same host name, http port), because a
  self-signed or mkcert certificate would make an agent's `curl` or Python refuse the connection.
  Page links and `url` fields keep the https address.
- **Firewall**: `./tasks.sh --status` reads ufw's state and prints the rule you need, if any, for the
  http port and the https port. For example, for a `192.168.1.0/24` LAN:

  ```sh
  sudo ufw allow from 192.168.1.0/24 to any port 8765 proto tcp   # LAN over IPv4
  sudo ufw allow from fe80::/10 to any port 8765 proto tcp        # hostname URLs that resolve to IPv6
  sudo ufw allow from 192.168.1.0/24 to any port 8766 proto tcp   # the https port, if you use one
  ```

- **Link URLs**: the `url` fields and page links the board hands out start with `--public-url`. When
  the LAN can reach the board, `tasks.sh` sets `--public-url` to `http://<lan-ip>:<port>`. Otherwise
  links use the address each client connected to. Set `--public-url` yourself for a DNS name or a
  reverse proxy.

## Data and retention

State lives in SQLite at `tasks/data/tasks.db`, next to the pidfiles and the settings file. The
directory is gitignored. A database from an earlier version is upgraded in place on start.
`tasks.sh --bg` logs to `.logs/tasks_server.log`, which is also gitignored. These server flags
control aging:

| Flag | Default | Meaning |
|---|---|---|
| `--stale-after` | `600` (10 min) | Seconds without an update before running work shows as stale |
| `--expire-after` | `86400` (24 h) | Seconds without activity before a running request is closed as failed ("expired: no activity for 24h"). `0` disables it. Waiting requests never expire. |
| `--retention-days` | `30` | Days to keep finished requests before deleting them. `0` keeps them forever. |

The event log behind the notifications keeps 7 days, and at most the newest 10000 events. A
maintenance pass runs at startup and then every 10 minutes or less. `tasks.sh` does not pass these
three flags through, so to change them, run the server directly, for example
`python3 tasks/server.py --retention-days 7`. See `python3 tasks/server.py --help`.

On agent machines, `taskctl` keeps only its [offline queue](#offline-queue), and only while there is
something to replay.

## Running the tests

```sh
tests/run_all.sh
```

See [`tests/README.md`](tests/README.md) for what the suites cover and how they isolate themselves
from your real board and `~/.claude`.

## Repository layout

```
tasks.sh                      launcher: run, --bg, --stop, --status, --install-skill
tasks/server.py               HTTP server and SQLite store; its docstring is the API contract
tasks/static/index.html       the dashboard (one file, no build step)
tasks/taskctl                 POSIX sh wrapper: finds Python 3.8+ and runs taskctl.py
tasks/taskctl.py              the CLI (standard library only)
tasks/skill/SKILL.md          the Claude Code skill, served at /api/skill/SKILL.md
tasks/templates/usage.md      instructions for agents, served at /api/usage
tasks/templates/rule.md       the CLAUDE.md rule block, served at /api/rule
tasks/templates/changelog.md  what each version added, served at /api/changelog
tasks/data/                   database, pidfiles and settings file (created at run time, gitignored)
tests/                        test suites (tests/run_all.sh)
docs/                         screenshots used in this README
```
