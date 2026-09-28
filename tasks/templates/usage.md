# Task status board: instructions for agents

{{BASE_URL}} is a task-status board (API version {{VERSION}}). A person watches it in a browser to
follow long-running work that AI agents do for them, instead of asking for status. You report to it:
one titled **request** per job your user gives you, one **task** per step (or per subagent), progress
updates (a percent, a hint of what is happening right now, an optional ETA), and done or failed for
every task and then for the request.

- Dashboard: {{PUBLIC_URL}}/
- A request's page: {{PUBLIC_URL}}/r/<rid> (give your user this link)
- Read this board with curl, not WebFetch or another web-fetch tool: those refuse private LAN
  addresses. Every curl here uses `--connect-timeout 5` and `--noproxy '*'` (keep the quotes): the
  board is on the LAN, and a proxy from `http_proxy` would not reach it.
- If a request hangs or times out, the board host's firewall is blocking the port: tell your user, do not retry.
- If a host-name URL (such as `http://myhost.local:8765`) hangs or fails, use the board's IP address
  instead: on Windows and with mDNS (`.local`) names, a host name can resolve to an IPv6 link-local
  address that does not work, while the IP form always works.

## 1. Install the skill and CLI (Claude Code)

This installs the `task-status` skill and its `taskctl` CLI (a POSIX sh wrapper plus one Python 3.8+
standard-library file, preconfigured for {{BASE_URL}}) into `~/.claude/skills/task-status/`. Show the
command to your user and get their OK before running it. It works unchanged in bash, zsh and fish:

    mkdir -p ~/.claude/skills/task-status && curl -fsS --connect-timeout 5 --noproxy '*' {{BASE_URL}}/api/skill/SKILL.md -o ~/.claude/skills/task-status/SKILL.md && curl -fsS --connect-timeout 5 --noproxy '*' {{BASE_URL}}/api/skill/taskctl -o ~/.claude/skills/task-status/taskctl && curl -fsS --connect-timeout 5 --noproxy '*' {{BASE_URL}}/api/skill/taskctl.py -o ~/.claude/skills/task-status/taskctl.py && chmod +x ~/.claude/skills/task-status/taskctl ~/.claude/skills/task-status/taskctl.py && ~/.claude/skills/task-status/taskctl ping

- If `CLAUDE_CONFIG_DIR` is set, replace every `~/.claude` in the command (and below) with its value.
- On Windows, run it from Git Bash (Claude Code's Bash tool), not PowerShell.
- The last step prints the board URL and `taskctl: ok: board at ...`. To update, run the same
  command again. `TASKS_URL` points taskctl at a different board.

**Then the always-loaded rule.** A short block in `~/.claude/CLAUDE.md` tells every future session
when to use the skill. ASK YOUR USER FIRST, then run:

    ~/.claude/skills/task-status/taskctl install-rule

It fetches {{BASE_URL}}/api/rule and upserts the block between `<!-- task-status:begin -->` and
`<!-- task-status:end -->` in `~/.claude/CLAUDE.md` (in `$CLAUDE_CONFIG_DIR` when that is set),
creating the file if needed and leaving the rest of it untouched. Re-run it to update the block. The
raw block is at {{BASE_URL}}/api/rule if your user prefers to paste it by hand.

**Then** ask your user to run `/reload-skills` (or to start a new session). `/skills` then lists
`task-status`.

**Permissions.** Every report is a Bash call, so without allow rules your user gets a permission
prompt for each one, and subagents may be refused. Propose these entries for `permissions.allow` in
`~/.claude/settings.json` to your user; never add them yourself. Replace
`/home/YOU/.claude/skills/task-status/taskctl` with the absolute path of the installed CLI, which
`echo ~/.claude/skills/task-status/taskctl` prints:

    "Bash(/home/YOU/.claude/skills/task-status/taskctl new *)",
    "Bash(/home/YOU/.claude/skills/task-status/taskctl add *)",
    "Bash(/home/YOU/.claude/skills/task-status/taskctl start *)",
    "Bash(/home/YOU/.claude/skills/task-status/taskctl progress *)",
    "Bash(/home/YOU/.claude/skills/task-status/taskctl done *)",
    "Bash(/home/YOU/.claude/skills/task-status/taskctl fail *)",
    "Bash(/home/YOU/.claude/skills/task-status/taskctl show *)",
    "Bash(/home/YOU/.claude/skills/task-status/taskctl list *)",
    "Bash(/home/YOU/.claude/skills/task-status/taskctl ping *)"

Do not allow `taskctl run`: it runs arbitrary commands.

Once installed, follow the skill: it loads when you start multi-step, background, subagent or
workflow work. taskctl commands: `new TITLE [-t TASK]...`, `add RID TITLE... [--start]`,
`start TID [HINT]`, `progress TID PCT HINT [--eta DUR]`, `done ID [MSG]`, `fail ID MSG`, `show ID`,
`list [--all]` (every running request; `--all` adds the 20 most recently finished), `ping`,
`run TID [--every SEC] [--percent-regex RE] -- CMD...`, `install-rule [--file PATH]`. You only need
the rest of this page to call the HTTP API directly (no Python, or not Claude Code).

## 2. HTTP API

JSON in and out. Times are float Unix epoch seconds from the board's clock, and every successful JSON
response has `now`, so compute ages from it rather than from your own clock. A request id is 6
characters (`k3m9qa`); a task id is the request id plus a sequence number (`k3m9qa-3`). Titles are
capped at 200 characters and messages at 500 (longer ones are truncated). Errors are
`{"error": "...", "field": "...", "hint": "..."}` (field and hint optional): 400 invalid input, 404
unknown id or route, 405 wrong method, 409 state conflict, 411 chunked body (send Content-Length),
413 body over 64 KiB, 503 database busy (retry after a second). Browser-style writes with a foreign
`Origin` header get 403. A success body has `"warnings": [...]` when something was truncated, clamped
or ignored, such as a misspelled field.

    GET    /                              the dashboard in a browser, this text otherwise
    GET    /r/<rid>                       a request's page
    GET    /api, /api/usage               this text
    GET    /api/rule                      the CLAUDE.md rule block
    GET    /api/skill/SKILL.md            the skill
    GET    /api/skill/taskctl             the CLI wrapper (POSIX sh)
    GET    /api/skill/taskctl.py          the CLI (Python 3.8+, this board's URL baked in)
    GET    /api/health                    {ok, service: "tasks", version, pid, now, started_at, requests_running}
    POST   /api/requests                  {title, origin?, tasks?: [title, ...]} -> 201 the request with
                                          its id, url and tasks[] (all pending)
    GET    /api/requests                  ?status=running|done|failed&limit=N -> {now, stale_after, counts,
                                          requests[]}: running ones first, then finished ones; without
                                          status every running request is included and limit (default
                                          100, max 500) caps only the finished ones
    GET    /api/requests/<rid>            the request with its tasks[]
    DELETE /api/requests/<rid>            delete a request and its tasks (cleanup only)
    POST   /api/requests/<rid>/complete   {status?: done|failed, message?} -> the request, plus
                                          auto_closed[] (were running) and cancelled[] (were pending)
    POST   /api/requests/<rid>/tasks      {title, start?: true} -> 201 one task, running unless start is false
                                          {titles: [title, ...]} -> 201 {tasks[]}, all pending
    GET    /api/tasks/<tid>               one task
    POST   /api/tasks/<tid>/progress      {message (required), percent?: 0-100, eta_seconds?}
    POST   /api/tasks/<tid>/complete      {status?: done|failed, message?}

Fields worth knowing. A request has `id`, `title`, `status` (running, done or failed), `percent`,
`tasks_done` and `tasks_total` (X/Y; cancelled tasks are not counted), `current` (the latest running
task), `stale` and `url`. A task has `id`, `title`, `status` (pending, running, done, failed or
cancelled), `percent`, `message`, `eta_remaining`, `elapsed`, `stale` and `url`.

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

Examples: create a request with its tasks, report on one of the returned task ids, close it, then
the request, and read it back:

    curl -sS --connect-timeout 5 --noproxy '*' -X POST {{BASE_URL}}/api/requests -H 'Content-Type: application/json' -d '{"title": "Build the app and deploy it to staging", "tasks": ["Sync repos", "Build the app", "Deploy to staging"]}'

    curl -sS --connect-timeout 5 --noproxy '*' -X POST {{BASE_URL}}/api/tasks/k3m9qa-2/progress -H 'Content-Type: application/json' -d '{"percent": 45, "message": "Compiling shaders 812 of 2000", "eta_seconds": 300}'

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
- Close every task, then the request, also on failure or cancel (status failed plus a one-line
  reason). Never leave a request running.
- Give your user the request's page link once.
- Titles and hints: in single quotes, with no backticks, `$` or quote characters. No secrets: the
  board is visible on the LAN.
- Reporting is best effort. If the board is unreachable, carry on with the real work and mention it once.
- Only touch your own requests and tasks.
