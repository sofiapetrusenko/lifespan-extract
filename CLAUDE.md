# lifespan-extract — rules for Claude Code

## Scope discipline
- Follow PLAN.md. Work ONLY on the phase named in the current task. Never start the next phase, even if finished early — stop and report instead.
- All work happens on a branch named `phase-N-<short-name>`. Open a PR; never push to `main`.

## Hard boundaries
- NEVER write to `data/gold/` — it is human-labeled ground truth. Read-only, always.
- Do not add dependencies beyond `requirements.txt` / `package.json` without listing each new one in the PR description with a one-line reason.
- Do not touch `.env`, credentials, or deployment config unless the task explicitly says so.

## Orchestrated loop
- Phases are executed via `/run-phase N`: implementer -> checks -> reviewer
  (fresh context), repeated until the reviewer returns zero REQUIRED items,
  capped at 5 iterations.
- A PreToolUse hook physically blocks writes to `data/gold/` and `.claude/`.
  A BLOCKED message is a signal to stop and report, never to route around.
- Never modify reviewer.md, hooks, settings, or ci.yml to make a review or
  check pass. If a check seems wrong, say so in the report — the human decides.

## Engineering rules
- Prefer loud failure over silent fallback. A missing API key raises; a malformed model response raises with a windowed excerpt. No placeholder defaults.
- Model output is untrusted input: JSON repair heuristic + one retry, then raise.
- Absent data is `not_reported`, never guessed.
- Every module runnable standalone from the CLI where PLAN.md defines a CLI contract.

## Definition of done (per task)
1. `ruff check .` passes.
2. `pytest` passes (write tests for new logic, not just happy path).
3. The reviewer agent (`.claude/agents/reviewer.md`) has been run on the full diff in a fresh context and returned ZERO required changes.
4. PR is open with: what was built, deviations from PLAN.md (if any), new dependencies (if any), and open questions for the human.

## Communication
- When PLAN.md is ambiguous, ask the human rather than deciding silently. List questions at the end of the session output.
- Commit messages are written by the human. Stage changes; do not commit unless explicitly told to.
