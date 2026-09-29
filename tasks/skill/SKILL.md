---
name: task-status
description: Report live progress to the user's task-status web board with the bundled taskctl CLI, so the user can watch a page instead of asking you for status. Use it BEFORE starting multi-step work or anything likely to take longer than about 2 minutes (builds, deploys, test suites, migrations, data processing, long investigations or refactors), before any Bash command you run with run_in_background, before launching subagents or a Workflow, whenever your prompt contains a task-status id (a request id like k3m9qa or a task id like k3m9qa-3) or a STATUS REPORTING block, and whenever the user asks for a status report, progress, an ETA or how it is going. One titled request per user request, one task per step or agent, percent plus hint plus ETA at milestones, every task and the request closed at the end. While a request is open, flag it with taskctl ask before you stop to ask the user a question or wait for their input. Skip it for quick answers and single short commands.
allowed-tools:
  - Bash(${CLAUDE_SKILL_DIR}/taskctl new *)
  - Bash(${CLAUDE_SKILL_DIR}/taskctl add *)
  - Bash(${CLAUDE_SKILL_DIR}/taskctl start *)
  - Bash(${CLAUDE_SKILL_DIR}/taskctl progress *)
  - Bash(${CLAUDE_SKILL_DIR}/taskctl ask *)
  - Bash(${CLAUDE_SKILL_DIR}/taskctl resume *)
  - Bash(${CLAUDE_SKILL_DIR}/taskctl done *)
  - Bash(${CLAUDE_SKILL_DIR}/taskctl fail *)
  - Bash(${CLAUDE_SKILL_DIR}/taskctl show *)
  - Bash(${CLAUDE_SKILL_DIR}/taskctl list *)
  - Bash(${CLAUDE_SKILL_DIR}/taskctl ping *)
  - Bash(${CLAUDE_SKILL_DIR}/taskctl flush *)
  - Bash(${CLAUDE_SKILL_DIR}/taskctl usage *)
  - Bash(${CLAUDE_SKILL_DIR}/taskctl changelog *)
  - Bash(${CLAUDE_SKILL_DIR}/taskctl api *)
---

# Task status board

The user follows your long-running work on a live web page instead of asking you for status. Report
to it with `${CLAUDE_SKILL_DIR}/taskctl`, and always call it by exactly that path: no `cd`, no
`python3` prefix, no `~`, because the permission rules match that path. Reporting is a side channel.
It never replaces, delays or fails the real work.

Use it when the job has several steps, runs longer than a couple of minutes, runs in the background,
or fans out to subagents or a workflow. Skip it for quick answers and single short commands.

The board is separate from your own todo list. Mirroring your todo items as tasks is fine.

## 1. Open one request, with its tasks planned up front

```
${CLAUDE_SKILL_DIR}/taskctl new 'Build the app and deploy it to staging' -t 'Sync repos' -t 'Build the app' -t 'Deploy to staging'
```

stdout is the request id, then one task id per `-t`, in order:

```
k3m9qa
k3m9qa-1
k3m9qa-2
k3m9qa-3
```

stderr shows the page: `taskctl: created k3m9qa 'Build the app and deploy it to staging'  page: http://HOST:8765/r/k3m9qa`.

- One request per user request. Its title is one short sentence saying what the user asked for.
- Plan 2-8 tasks with `-t`: short imperative titles, each a step that actually finishes. Planning them
  now keeps the X/Y counter honest. Steps you discover later:
  `${CLAUDE_SKILL_DIR}/taskctl add k3m9qa 'Fix failing test'` (created pending), or
  `${CLAUDE_SKILL_DIR}/taskctl add --start k3m9qa 'Fix failing test'` (created running).
- Shell variables do not persist between your Bash calls. Read the ids from stdout and write them
  literally in every later command.
- Give the user the page link once, for example "Tracking this at http://HOST:8765/r/k3m9qa".
- If your prompt already gives you a task id (like `k3m9qa-3`), you are working for an orchestrator:
  do not create a request, report on that task only. If it gives you only a request id, register
  yourself with `${CLAUDE_SKILL_DIR}/taskctl add --start k3m9qa 'your label'` and use the id it prints.

## 2. Quote titles and hints safely

**Put every title and hint in single quotes, and never put backticks, `$` or quote characters in
them. Drop apostrophes: write dont, not don't.** A backtick or a dollar sign in double quotes, or an
apostrophe that ends a single-quoted string early, makes the shell execute part of your status text
as a command, which can re-run a build or a deploy just to print a hint.

Never put secrets, tokens or passwords in titles or hints. The board is visible on the LAN.

## 3. Report progress at milestones

```
${CLAUDE_SKILL_DIR}/taskctl start k3m9qa-1 'Fetching main and the release branch'
${CLAUDE_SKILL_DIR}/taskctl progress k3m9qa-2 40 'Compiling shaders 812 of 2000' --eta 6m
```

- `start TID 'HINT'` when you begin a task. Its running time counts from then (the first `progress`
  also starts a pending task).
- `progress TID PCT 'HINT'`: PCT is 0-100 (45, 45.5 or 45%). The hint is required and says what is
  happening right now.
- Add `--eta` (90s, 5m, 1h30m) whenever you can estimate the time left. A progress call without it
  clears the previous ETA.
- Report at real milestones only: roughly every 10-25% of the task or every few minutes. Never after
  every tool call.

## 4. Background commands report for themselves

When you run something with `run_in_background`, wrap the command with `taskctl run`:

```
${CLAUDE_SKILL_DIR}/taskctl run k3m9qa-2 -- ./scripts/deploy.sh --env staging
${CLAUDE_SKILL_DIR}/taskctl run k3m9qa-2 --percent-regex '(\d+)/(\d+)' -- ./run_tests.sh
```

`run` starts the task, passes the output through unchanged, sends the latest output line as the hint
(at most every `--every` seconds, default 10, plus a heartbeat every minute), marks the task done or
failed from the exit status, and exits with the command's own status. `--percent-regex` takes one
group (the percent, e.g. `'(\d+)%'`) or two groups (done and total, e.g. `'(\d+)/(\d+)'`). This keeps
the board live while you are idle. `run` is not pre-approved, because it runs the wrapped command, so
it asks for permission like the command itself would.

## 5. Flag it when you stop to wait for the user

**While one of your requests is open, run `ask` before you stop to ask the user a question, wait for
a decision or an approval, or need them to do something** (log in, plug in a device, check a screen).
The board then pulses the request, shows your question and can chime or notify, so the user notices
even with the page in the background. Without it the page only looks busy, then stale.

```
${CLAUDE_SKILL_DIR}/taskctl ask k3m9qa 'Deploy to the eu or the us staging cluster?'
${CLAUDE_SKILL_DIR}/taskctl ask k3m9qa-3 'Waiting for your OK to deploy to staging'
```

- Run it right before the reply that asks, before a tool that asks the user (such as
  AskUserQuestion), or before you end your turn to wait. The question is one line, quoted like a hint.
- The request id flags the whole request; a task id flags just the blocked step (a pending task
  starts). Asking again replaces the question.
- When the user answers, first run `${CLAUDE_SKILL_DIR}/taskctl resume k3m9qa` (or
  `${CLAUDE_SKILL_DIR}/taskctl resume k3m9qa-3`), then carry on. Progress on a task or closing it
  also clears that task's flag, and closing the request clears every flag, but nothing but `resume`
  clears a flag on a request that stays open.
- A waiting request is never marked stale or expired, so it can wait as long as the user needs.
- Only for the user. Waiting for a build, a download or a subagent is progress, not `ask`.

## 6. Close everything, including on failure

```
${CLAUDE_SKILL_DIR}/taskctl done k3m9qa-1 'Synced 3 repos'
${CLAUDE_SKILL_DIR}/taskctl fail k3m9qa-3 'staging upload rejected with 401'
${CLAUDE_SKILL_DIR}/taskctl fail k3m9qa 'Built fine, the staging upload was rejected'
```

- The id's shape picks what closes: `k3m9qa` closes the request, `k3m9qa-3` closes one task. `fail`
  needs a one-line reason; for `done` the message is optional.
- Close each task when it ends, with its real outcome. Close the request last: only after the work,
  background work included, has really finished, and before your final reply for that request.
- **Closing a request cascades**: its pending tasks become cancelled and its still-running tasks take
  the request's status. So close the tasks first. A planned task you deliberately skip can stay
  pending: it is cancelled and drops out of X/Y.
- On an error, when the user cancels, or when you give up: `fail` the affected task and the request
  with a one-line reason. Never leave a request running.
- Closing twice is harmless. Progress on a closed task is refused (a warning, nothing else). If the
  user asks for more work after you closed the request, `add` tasks to it: that reopens it.

## 7. Status questions

When the user asks for a status report, progress or how it is going, run
`${CLAUDE_SKILL_DIR}/taskctl show k3m9qa` (or `show k3m9qa-2` for one task), summarize it and include
the page link. If you lost the id, `${CLAUDE_SKILL_DIR}/taskctl list` shows every running request
(`list --all` adds the 20 most recently finished ones). Answer from the board, not from memory.

## 8. Staying current

The board gains features over time, and taskctl keeps this skill in step with it. When the board's
agent instructions change, the next taskctl command that talks to this skill's own board updates this
skill and taskctl in place and prints, on stderr:

```
taskctl: the board's agent instructions changed (docs OLD -> NEW); updated the skill in DIR
taskctl: what's new:
(the changelog entries for the versions you have not seen)
taskctl: re-read DIR/SKILL.md before continuing; the copy loaded in your context is out of date
```

- When you see it, read `${CLAUDE_SKILL_DIR}/SKILL.md` again with your Read tool before the next
  taskctl command, and follow the new copy from then on. The command that printed it already did its
  job: do not run it again.
- For anything the board offers that has no command here, `${CLAUDE_SKILL_DIR}/taskctl usage` prints
  its full HTTP API reference, and `api METHOD PATH ['JSON']` calls any route and prints the response
  body, for example `${CLAUDE_SKILL_DIR}/taskctl api GET /api/requests/k3m9qa`. Put a JSON body in
  single quotes, with no apostrophes, backticks or `$` inside.
- `${CLAUDE_SKILL_DIR}/taskctl changelog` lists what each board version added, and
  `${CLAUDE_SKILL_DIR}/taskctl update` checks for new instructions right away. It is not pre-approved,
  because it replaces this skill's code, so it asks for permission.
- Only the board this skill came from updates it. A command sent to another board with `--url` or
  `TASKS_URL` never changes the skill (taskctl says so in a note), and `update` refuses another board
  unless the user wants the skill moved there (`--force`).

## 9. If the board is unreachable

If taskctl prints `taskctl: warning: board unreachable at URL (REASON); queued for replay (N queued)`,
keep working. Do not retry, debug or restart the board; mention it once in your final reply.

- Reports are queued on this machine and replayed in order, with their original times, by the next
  taskctl command that reaches the board. `new`, `start`, `progress`, `ask`, `resume`, `done` and
  `fail` all queue, and `new` still prints real ids, so keep using them as usual.
- `add` cannot get ids while the board is down (nor `new` when the board may have missed it): they
  print `offline` in place of each id, and any command given the id `offline` does nothing, so keep
  using it as if it were a real id.
- `taskctl: another taskctl is replaying the queued writes for URL; this one is queued behind them`
  means another agent on this machine is sending the queue right now and sends your report with it:
  nothing to do.
- `${CLAUDE_SKILL_DIR}/taskctl ping` checks the board and says how many reports are queued;
  `${CLAUDE_SKILL_DIR}/taskctl flush` replays them now. The queue outlives your session.

## 10. Workflows and subagents

Workflow scripts have no network access, and a subagent may not have this skill loaded. So you, the
orchestrator, create the request and one task per planned agent before launching them, and each agent
reports on its own task with the commands you put in its prompt.

1. Before the Workflow or Agent call, open the request with one `-t` per planned agent, named after
   the agent's role:

   ```
   ${CLAUDE_SKILL_DIR}/taskctl new 'Build the task-status board' -t 'build:server' -t 'build:ui' -t 'build:docs' -t 'verify:contract' -t 'verify:ui'
   ```

   It prints `k3m9qa`, then `k3m9qa-1` to `k3m9qa-5` in `-t` order, and the page link on stderr.
   Give the user the link.

2. Pass the request id, the page link and each agent's task id into the Workflow args (for example
   `{"rid": "k3m9qa", "page": "http://HOST:8765/r/k3m9qa", "tids": {"build:server": "k3m9qa-1", "build:ui": "k3m9qa-2"}}`)
   or straight into the Agent prompt. Every agent's prompt gets this block, with its task id and the
   page link filled in:

   ```
   ## STATUS REPORTING (required — the user is watching this live at <PAGE_URL>)
   You own task <TID> on the user's task board. Use exactly these Bash commands. Titles and hints go in SINGLE quotes and must not contain backticks, $ or quote characters:
     ${CLAUDE_SKILL_DIR}/taskctl start <TID> 'short hint of what you are starting'
     ${CLAUDE_SKILL_DIR}/taskctl progress <TID> <0-100> 'what you are doing right now' --eta 5m     (--eta optional: 90s, 5m, 1h30m)
     ${CLAUDE_SKILL_DIR}/taskctl done <TID> 'one-line result'        (or, if you could not finish:  ${CLAUDE_SKILL_DIR}/taskctl fail <TID> 'one-line reason')
   Run start FIRST. Report at real milestones (roughly every 20-25% of your work or every few minutes) — never after every tool call. done/fail must be your LAST action before your final message. Do not create requests or touch other tasks. If taskctl warns the board is unreachable, ignore it and keep working.
   ```

   The taskctl path in this block is already absolute: this skill's directory was filled in when the
   skill loaded. Paste it exactly as you see it here. Subagents need that literal path, never a
   variable, `~` or a relative path, and it is also the path the user's permission allow rules match
   (subagents do not get this skill's pre-approved commands).

3. Dynamic fan-out: when the agents are only known at run time (one per file, per failing test, ...),
   do not pre-create their tasks. Pass the request id and a short label per agent (no quotes,
   backticks or `$` in it), and give each agent this variant, in which it registers itself:

   ```
   ## STATUS REPORTING (required — the user is watching this live at <PAGE_URL>)
   You report under request <RID> on the user's task board. Use exactly these Bash commands. Titles and hints go in SINGLE quotes and must not contain backticks, $ or quote characters. First register your task; it prints your task id, <TID> below:
     ${CLAUDE_SKILL_DIR}/taskctl add --start <RID> '<LABEL>'
     ${CLAUDE_SKILL_DIR}/taskctl progress <TID> <0-100> 'what you are doing right now' --eta 5m     (--eta optional: 90s, 5m, 1h30m)
     ${CLAUDE_SKILL_DIR}/taskctl done <TID> 'one-line result'        (or, if you could not finish:  ${CLAUDE_SKILL_DIR}/taskctl fail <TID> 'one-line reason')
   Run add FIRST and write the printed task id literally in later commands. Report at real milestones (roughly every 20-25% of your work or every few minutes) — never after every tool call. done/fail must be your LAST action before your final message. Do not create requests or touch other tasks. If taskctl warns the board is unreachable, ignore it and keep working.
   ```

4. After the workflow returns, run `${CLAUDE_SKILL_DIR}/taskctl show k3m9qa`. A task still pending or
   running belongs to an agent that crashed or never reported: `fail` it if its work did not finish.
   Then close the request with `${CLAUDE_SKILL_DIR}/taskctl done k3m9qa 'one-line summary'` (or `fail`
   with the reason). `done` on the request also closes the leftovers: still-running tasks become done
   and pending ones are cancelled.

Subagents cannot ask the user anything, so flagging a wait (section 5) stays with you: `ask` on the
request before you put a question from the workflow to the user. If the board was unreachable when
you ran `new`, pass its ids on anyway; the agents' reports queue on this machine like yours.

## Commands

| Command | What it does |
|---|---|
| `new 'TITLE' [-t 'TASK']...` | open a request; prints its id, then one task id per `-t` |
| `add RID 'TITLE'... [--start]` | add pending tasks (or one running task with `--start`); prints their ids |
| `start TID ['HINT']` | mark a task running |
| `progress TID PCT 'HINT' [--eta DUR]` | percent, what is happening now, optional ETA |
| `ask ID 'QUESTION'` | flag a request or task as waiting for the user, with the question |
| `resume ID` | clear that flag once the user has answered |
| `done ID ['MSG']` / `fail ID 'MSG'` | close a task, or a whole request (cascades) |
| `show ID` | a request with its tasks, or one task |
| `list [--all]` | every running request (`--all` adds the 20 most recently finished) |
| `ping` | check the board; prints its URL, and how many reports are queued |
| `flush` | replay the reports queued while the board was unreachable |
| `run TID [--every SEC] [--percent-regex RE] -- CMD...` | run a command and report its output |
| `usage` | the board's full HTTP API reference |
| `api METHOD PATH ['JSON']` | call any board route; prints the response body |
| `changelog` | what each board version added |
| `update [--force]` | fetch this skill's board's current skill and taskctl now (asks for permission) |

All of them are `${CLAUDE_SKILL_DIR}/taskctl COMMAND ...`. Global options: `--url URL` (or
`TASKS_URL`), `--timeout SEC`, `--strict`.
