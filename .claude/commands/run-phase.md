---
description: Run one PLAN.md phase through the implementer -> reviewer loop until the review is clean, then prepare the PR.
---

Orchestrate Phase $ARGUMENTS of PLAN.md. You are the ORCHESTRATOR: you do not
write feature code yourself; you dispatch subagents and enforce the loop.

## Protocol
1. Read PLAN.md and CLAUDE.md. Create branch `phase-$ARGUMENTS-<short-name>`
   if it does not exist.
2. LOOP (max 5 iterations):
   a. Dispatch the **implementer** subagent with: the phase number, the full
      text of that phase from PLAN.md, and (from iteration 2 onward) the
      reviewer's complete REQUIRED list verbatim. The implementer fixes ONLY
      what is listed.
   b. Run `ruff check .` and `pytest` yourself. If either fails, send the
      failures back to the implementer (same iteration).
   c. Dispatch the **reviewer** subagent in a FRESH context with the full
      diff (`git diff main...HEAD`) and PLAN.md. Never summarize the diff
      for it — pass it whole.
   d. If verdict is APPROVE with zero REQUIRED items -> exit loop.
3. If 5 iterations pass without a clean review: STOP. Do not weaken tests,
   do not touch reviewer.md, hooks, or ci.yml to make the review pass.
   Produce a report: remaining REQUIRED items + open questions for the human.
4. On clean review: stage all changes (do NOT commit), write the PR
   description per CLAUDE.md's Definition of Done, and print the exact
   `git commit` + `gh pr create` commands for the human to run.

## Hard rules
- A hook BLOCKED message means: stop, report to the human. Never work around it.
- Reviewer feedback flows one way: fix it or escalate. Never argue with the
  reviewer or re-litigate a REQUIRED item.
- Append one line per iteration to `LOOP_LOG.md`:
  `iter N | REQUIRED: K | <one-line summary>`. This is the PR's audit trail.
