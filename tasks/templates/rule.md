<!-- task-status:begin -->
## Task status board
- Before background, multi-step (over ~2 min), subagent or workflow work, use the task-status skill: create the titled request with its tasks first and give me its link ({{PUBLIC_URL}}/r/<rid>).
- Report at milestones, and close every task and then the request when done, also on failure.
- While a request is open, before you ask me a question or wait for my input, run the task-status skill's `taskctl ask <rid> '<question>'`, and `taskctl resume <rid>` after I answer.
- When I ask for status, answer from the task-status skill's `taskctl show <rid>`, not from memory.
<!-- task-status:end -->
