# Task status board: instructions for agents

{{BASE_URL}} is a task-status board (API version {{VERSION}}, docs {{DOCS_VERSION}}). A person
watches it in a browser to follow long-running work that AI agents do for them, instead of asking for
status. You report to it: one titled **request** per job your user gives you, one **task** per step
(or per subagent), progress updates (a percent, a hint of what is happening right now, an optional
ETA), a flag whenever you stop to wait for your user's input, and done or failed for every task and
then for the request.

- Dashboard: {{PUBLIC_URL}}/
- A request's page: {{PUBLIC_URL}}/r/<rid> (give your user this link)
- Read this board with curl, not WebFetch or another web-fetch tool: those refuse private LAN
  addresses. Every curl here uses `--connect-timeout 5` and `--noproxy '*'` (keep the quotes): the
  board is on the LAN, and a proxy from `http_proxy` would not reach it.
- If a request hangs or times out, the board host's firewall is blocking the port: tell your user, do not retry.
- If a host-name URL (such as `http://myhost.local:8765`) hangs or fails, use the board's IP address
  instead: on Windows and with mDNS (`.local`) names, a host name can resolve to an IPv6 link-local
  address that does not work, while the IP form always works.
- Always use {{BASE_URL}} (plain http). The board may also serve https on another port for browsers
  (desktop notifications need it); that is for people, not agents.

## 1. Install the skill and CLI (Claude Code)

This installs the `task-status` skill and its `taskctl` CLI (a POSIX sh wrapper plus one Python 3.8+
standard-library file, preconfigured for {{BASE_URL}}) into `~/.claude/skills/task-status/`. Show the
command to your user and get their OK before running it. It works unchanged in bash, zsh and fish:

    mkdir -p ~/.claude/skills/task-status && curl -fsS --connect-timeout 5 --noproxy '*' {{BASE_URL}}/api/skill/SKILL.md -o ~/.claude/skills/task-status/SKILL.md && curl -fsS --connect-timeout 5 --noproxy '*' {{BASE_URL}}/api/skill/taskctl -o ~/.claude/skills/task-status/taskctl && curl -fsS --connect-timeout 5 --noproxy '*' {{BASE_URL}}/api/skill/taskctl.py -o ~/.claude/skills/task-status/taskctl.py && chmod +x ~/.claude/skills/task-status/taskctl ~/.claude/skills/task-status/taskctl.py && ~/.claude/skills/task-status/taskctl ping

- If `CLAUDE_CONFIG_DIR` is set, replace every `~/.claude` in the command (and below) with its value.
- On Windows, run it from Git Bash (Claude Code's Bash tool), not PowerShell.
- The last step prints the board URL and `taskctl: ok: board at ...`. `TASKS_URL` points taskctl at
  a different board.
- It keeps itself current: whenever the board's instructions change, the next taskctl command sent
  to this board ({{BASE_URL}}, the one it was installed from) replaces the installed SKILL.md, taskctl
  and taskctl.py with the board's new ones (and refreshes the CLAUDE.md rule below, if it is
  installed), then prints what is new and asks you to re-read SKILL.md. A command sent to another
  board (`--url` or `TASKS_URL`) never changes the skill or the rule. `taskctl update` updates right
  away; it refuses another board unless given `--force`, which moves the skill to that board.
  `TASKS_NO_UPDATE=1` turns updates off. Running the install command again also updates it.

**Then the always-loaded rule.** A short block in `~/.claude/CLAUDE.md` tells every future session
when to use the skill. ASK YOUR USER FIRST, then run:

    ~/.claude/skills/task-status/taskctl install-rule

It fetches {{BASE_URL}}/api/rule and upserts the block between `<!-- task-status:begin -->` and
`<!-- task-status:end -->` in `~/.claude/CLAUDE.md` (in `$CLAUDE_CONFIG_DIR` when that is set),
creating the file if needed and leaving the rest of it untouched. Re-run it to update the block. The
raw block is at {{BASE_URL}}/api/rule if your user prefers to paste it by hand.

**Then** ask your user to run `/reload-skills` (or to start a new session). `/skills` then lists
`task-status`.

**Permissions.** Every report is a Bash call, so without permission rules your user gets a prompt for
each one, and subagents may be refused. Propose these rules for `~/.claude/settings.json` to your
user (added to any `permissions.allow` and `permissions.ask` lists already there); never add them
yourself. Replace `/home/YOU/.claude/skills/task-status/taskctl` with the absolute path of the
installed CLI, which `echo ~/.claude/skills/task-status/taskctl` prints:

    "permissions": {
      "allow": ["Bash(/home/YOU/.claude/skills/task-status/taskctl *)"],
      "ask": [
        "Bash(/home/YOU/.claude/skills/task-status/taskctl run *)",
        "Bash(/home/YOU/.claude/skills/task-status/taskctl --* run *)",
        "Bash(/home/YOU/.claude/skills/task-status/taskctl update*)",
        "Bash(/home/YOU/.claude/skills/task-status/taskctl --* update*)",
        "Bash(/home/YOU/.claude/skills/task-status/taskctl install-rule*)",
        "Bash(/home/YOU/.claude/skills/task-status/taskctl --* install-rule*)"
      ]
    }

The allow rule covers every taskctl command, including ones a later version adds. An ask rule beats
an allow rule, so `taskctl run` (runs an arbitrary command), `taskctl update` (replaces the skill's
code) and `taskctl install-rule` (rewrites the always-loaded CLAUDE.md) still ask each time, also
with a global option such as `--url` in front of them.

Once installed, follow the skill: it loads when you start multi-step, background, subagent or
workflow work. taskctl commands: `new TITLE [-t TASK]...`, `add RID TITLE... [--start]`,
`start TID [HINT]`, `progress TID PCT HINT [--eta DUR]`, `ask ID QUESTION` (you are waiting for
your user), `resume ID` (they answered), `done ID [MSG]`, `fail ID MSG`, `show ID`, `list [--all]`
(every running request; `--all` adds the 20 most recently finished), `ping` (also shows queued
reports), `flush`, `run TID [--every SEC] [--percent-regex RE] -- CMD...`, `usage` (this text),
`api METHOD PATH [JSON]` (any route below), `changelog`, `update [--force]`,
`install-rule [--file PATH]`. If the board is unreachable (the connection is refused, has no route,
the name does not resolve, a local firewall blocks it, or it is not made in time), taskctl queues
new, start, progress, ask, resume, done and fail on your machine and replays them in order, with
their original times, once it is reachable again, so carry on with the work. One taskctl at a time
replays the queue; the others never wait for it (a report made meanwhile is queued behind it). You only need the rest of this page to call the
HTTP API directly (no Python, or not Claude Code), or for something taskctl has no command for.

## 2. HTTP API

JSON in and out. Times are float Unix epoch seconds from the board's clock, and every successful JSON
response has `now`, so compute ages from it rather than from your own clock. A request id is 6
characters from `23456789abcdefghjkmnpqrstuvwxyz` (`k3m9qa`); a task id is the request id plus a
sequence number (`k3m9qa-3`). Titles are capped at 200 characters and messages at 500 (longer ones
are truncated). Errors are `{"error": "...", "field": "...", "hint": "..."}` (field and hint
optional): 400 invalid input, 404 unknown id or route, 405 wrong method, 409 state conflict, 410
a live stream that was replaced or closed, 411 chunked body (send Content-Length), 413 body over 64 KiB, 503
database busy (retry after a second) or a live stream that is not connected yet.
Browser-style writes (POST, PUT, DELETE) with a foreign `Origin` header get 403. A success body has
`"warnings": [...]` when something was truncated, clamped or ignored, such as a misspelled field.

Every response, errors included, carries two headers: `X-Tasks-Version` (the API version, now
{{VERSION}}) and `X-Tasks-Docs` (a hash of this text, the rule, the changelog, the skill and taskctl,
now {{DOCS_VERSION}}). When `X-Tasks-Docs` differs from the value you read here, these instructions
have changed: read {{BASE_URL}}/api/changelog and this page again.

    GET    /                              the dashboard in a browser, this text otherwise
    GET    /r/<rid>                       a request's page
    GET    /api, /api/usage               this text
    GET    /api/rule                      the CLAUDE.md rule block
    GET    /api/changelog                 what each API version added, newest first (plain text)
    GET    /api/skill/SKILL.md            the skill
    GET    /api/skill/taskctl             the CLI wrapper (POSIX sh)
    GET    /api/skill/taskctl.py          the CLI (Python 3.8+, this board's URL baked in)
    GET    /api/health                    {ok, service: "tasks", version, docs_version, tls_port,
                                          config_path, pid, now, started_at, requests_running}
    POST   /api/requests                  {title, origin?, tasks?: [title, ...], id?} -> 201 the request
                                          with its id, url and tasks[] (all pending); see "Request ids"
    GET    /api/requests                  ?status=running|done|failed&limit=N -> {now, stale_after, counts,
                                          requests[]}: running ones first, then finished ones; without
                                          status every running request is included and limit (default
                                          100, max 500) caps only the finished ones
    GET    /api/requests/<rid>            the request with its tasks[]
    DELETE /api/requests/<rid>            delete a request and its tasks (cleanup only)
    POST   /api/requests/<rid>/complete   {status?: done|failed, message?} -> the request, plus
                                          auto_closed[] (were running) and cancelled[] (were pending)
    POST   /api/requests/<rid>/attention  {message} -> the request with its tasks[]: it waits for its user
    DELETE /api/requests/<rid>/attention  clear the request's own flag -> the request with its tasks[]
    POST   /api/requests/<rid>/tasks      {title, start?: true} -> 201 one task, running unless start is false
                                          {titles: [title, ...]} -> 201 {tasks[]}, all pending
    GET    /api/tasks/<tid>               one task
    POST   /api/tasks/<tid>/progress      {message (required), percent?: 0-100, eta_seconds?}
    POST   /api/tasks/<tid>/complete      {status?: done|failed, message?}
    POST   /api/tasks/<tid>/attention     {message} -> the task: this step waits for its user
    DELETE /api/tasks/<tid>/attention     clear the task's flag -> the task
    GET    /api/events                    ?since=N&limit=1..1000 (500) -> the event log (below)
    GET    /api/settings                  the board-wide notification settings (below)
    PUT    /api/settings                  {settings: {...}} change some of them
    DELETE /api/settings                  back to the defaults
    GET    /api/stream                    the live stream (below): {now, stream, ffmpeg}
    POST   /api/stream                    {url} -> 201 open a stream, replacing any open one
    DELETE /api/stream                    close it
    GET    /api/stream/media?id=<id>      the stream itself as fragmented MP4, for the dashboard

Fields worth knowing. A request has `id`, `title`, `status` (running, done or failed), `percent`,
`tasks_done` and `tasks_total` (X/Y; cancelled tasks are not counted), `current` (the latest running
task), `stale`, `url`, `attention` (its own flag: `{"message", "since"}` or null), `waiting` (true
while it is running and it or one of its running tasks has a flag) and `tasks_waiting` (how many
running tasks have one). A task has `id`, `title`, `status` (pending, running, done, failed or
cancelled), `percent`, `message`, `eta_remaining`, `elapsed`, `stale`, `url` and `attention`. The
list's `counts` has `running`, `stale`, `done`, `failed` and `waiting`.

State rules:
- The first progress call on a pending task starts it (running, its timer starts). An omitted
  `percent` keeps the old value; an omitted `eta_seconds` clears the ETA.
- Progress on a closed task (done, failed or cancelled) is a 409.
- Completing twice is harmless: the same status again is a no-op, a different status overwrites.
- Closing a request cascades: its pending tasks become cancelled and its running tasks take the
  request's status. Close the tasks first, with their real outcome.
- Adding a task to a closed request reopens it.
- By default a running item with no update for 10 minutes (and not inside its ETA) shows as stale,
  and a running request silent for 24 hours is closed as failed.

Waiting for your user (attention). Before you stop to ask your user a question or wait for their
input, POST the question (`message`, required, one line) to the request's attention route, or to a
task's when only that step is blocked; DELETE it once they answer. The dashboard pulses it, shows the
question and can chime or notify.
- Posting again replaces the question and its `since` time. A pending task starts (running). A
  closed task, or a closed request, is a 409. DELETE is idempotent.
- Progress on a task clears that task's flag, and so does completing it; completing the request
  clears its own flag and every task's. Nothing else clears the request's own flag.
- Something waiting is never stale, and a waiting request never expires.

Request ids and offline replay. POST /api/requests takes an optional `id`: 6 characters of the
alphabet above, made by you, so you know the request and task ids (`<id>-1`, `<id>-2`, ... in
`tasks` order) before the board answers. Sending the same `id` and `title` again returns the
existing request (200, `"existing": true`), so a create is safe to retry; the same `id` with another
title is a 409 (make a new id). Writes you queued while the board was unreachable can be replayed
with the header `X-Tasks-Replay: 1` and a body field `at` (epoch seconds when it happened): the board
then records the write at that time (clamped to the last 24 hours and never before the item's last
update) and marks its events as replayed. Without the header, `at` is ignored with a warning.

Events. `GET /api/events` returns `{now, cursor, events[], truncated, waiting[], stale[],
settings_version, stream}` (`stream` is the open live stream, or null). Without `since`, `events` is empty and `cursor` is the newest event id (0 when
there are none): keep it and pass it as `since` next time. With `since`, `events` holds up to `limit`
events with a larger id, oldest first; `truncated` is true when more remain (then `cursor` is the
last one returned) or when events after `since` were already pruned (events are kept 7 days, at most
10000). An event is `{id, ts, type, request_id, task_id, request_title, task_title, status, percent,
message, replayed}`, with `type` one of request_created, request_reopened, request_done,
request_failed, request_deleted, task_added, task_started, task_progress, task_done, task_failed,
attention (message = the question; task_id null for a request's own flag) and attention_cleared.
`waiting` lists every open flag of a running request as `{request_id, request_title, task_id,
task_title, message, since}` (task fields null for a request's own flag); `stale` lists the running
requests that are stale now as `{request_id, request_title}`.

Settings. What the dashboard does for each kind of event, for everyone who opens it, lives in a JSON
file on the board host (`config_path` in /api/health). `GET /api/settings` returns `{now, path,
version, exists, error, settings, defaults}`: `settings` is what is in effect (the defaults merged
with the file; the defaults alone, with `error` set, when the file is not valid). The shape is
`{"events": {KEY: {"highlight": bool, "title": bool, "sound": bool, "notify": bool}}, "sound":
{"volume": 0.0-1.0}}` with KEY one of waiting, task_failed, request_failed, request_done, task_done,
task_started, task_progress, request_created and stale. `PUT /api/settings` with `{"settings":
{...}}` (any part of that shape) validates it (an unknown key or a non-boolean is a 400 naming the
field), merges it into the current settings, saves the whole result and answers like GET; `DELETE`
removes the file. `version` (also `settings_version` in /api/events) changes whenever the settings
do. Leave the settings to your user unless they ask you to change them.

Live stream. The dashboard can show one network video or audio stream beside the board on every open
page (below it on a portrait screen, to its right on a landscape one). Do this only when your user asks.
`taskctl api POST /api/stream '{"url": "http://camera.lan:8554/"}'` opens a stream (201; a newer url
replaces the open one, and the url that is already open is kept: 200 with `"existing": true`, retried
at once), `taskctl api DELETE /api/stream` closes it (idempotent) and `taskctl api GET /api/stream`
shows it as `{now, stream, ffmpeg}`, with `ffmpeg` the program the board runs for it or null when the
board host does not have it (`apt install ffmpeg` there; the reply to POST warns). The url is http,
https, rtsp, rtsps, rtmp, rtmps, srt, udp or tcp, up to 2048 characters with no spaces, else a 400 with
`field: "url"`. The board connects to it from its own host, so the name must resolve and be reachable
from there, and it is a stream source (a camera, an encoder), not a web page. `stream` is null or
`{id, url, opened_at, state, since, error, video, audio, width, height, viewers, media}`: `state` is
"connecting" until a keyframe has gone out and "live" after that; `error` says why the last attempt
failed (the board retries after 1, 2, 4, 8 and then every 10 seconds); `video` and `audio` are codec
strings such as avc1.42C028 and mp4a.40.2, null until known or when there is no such track; `url`
shows a `user:password@` as `***@`. The same object is `stream` in /api/events. The stream is saved on
the board host and reopens when the board restarts. `media` is the path of the stream itself, which
only the dashboard needs.

Examples: create a request with its tasks, report on one of the returned task ids, flag a question
and clear it, close the task, then the request, and read it back:

    curl -sS --connect-timeout 5 --noproxy '*' -X POST {{BASE_URL}}/api/requests -H 'Content-Type: application/json' -d '{"title": "Build the app and deploy it to staging", "tasks": ["Sync repos", "Build the app", "Deploy to staging"]}'

    curl -sS --connect-timeout 5 --noproxy '*' -X POST {{BASE_URL}}/api/tasks/k3m9qa-2/progress -H 'Content-Type: application/json' -d '{"percent": 45, "message": "Compiling shaders 812 of 2000", "eta_seconds": 300}'

    curl -sS --connect-timeout 5 --noproxy '*' -X POST {{BASE_URL}}/api/requests/k3m9qa/attention -H 'Content-Type: application/json' -d '{"message": "Deploy to the eu or the us staging cluster?"}'

    curl -sS --connect-timeout 5 --noproxy '*' -X DELETE {{BASE_URL}}/api/requests/k3m9qa/attention

    curl -sS --connect-timeout 5 --noproxy '*' -X POST {{BASE_URL}}/api/tasks/k3m9qa-2/complete -H 'Content-Type: application/json' -d '{"status": "done", "message": "Built in 4m"}'

    curl -sS --connect-timeout 5 --noproxy '*' -X POST {{BASE_URL}}/api/requests/k3m9qa/complete -H 'Content-Type: application/json' -d '{"status": "failed", "message": "staging upload rejected with 401"}'

    curl -fsS --connect-timeout 5 --noproxy '*' {{BASE_URL}}/api/requests/k3m9qa

Keep the JSON in single quotes, and keep apostrophes, backticks and `$` out of the text inside it.

## 3. Etiquette

- One request per user request, titled with one short sentence of what they asked. Plan its 2-8
  tasks up front so X/Y is honest.
- Start a task when you begin it. Report at milestones (roughly every 10-25% or every few minutes),
  always with a hint of what is happening now and an ETA when you can estimate one. Never after every
  tool call.
- Before you ask your user a question or stop to wait for their input, flag it (`taskctl ask`, or
  the attention route) with the question; clear it (`taskctl resume`) as soon as they answer. Waiting
  on a build or a subagent is progress, not a flag.
- Close every task, then the request, also on failure or cancel (status failed plus a one-line
  reason). Never leave a request running.
- Give your user the request's page link once.
- Open or close the board's live stream only when your user asks for it.
- Titles and hints: in single quotes, with no backticks, `$` or quote characters. No secrets: the
  board is visible on the LAN.
- Reporting is best effort. If the board is unreachable, carry on with the real work and mention it once.
- Only touch your own requests and tasks.
