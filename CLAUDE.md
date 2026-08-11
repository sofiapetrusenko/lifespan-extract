# lifespan-extract — rules for Claude Code

## Scope discipline
- Follow PLAN.md. Work ONLY on the phase named in the current task. Never start the next phase, even if finished early — stop and report instead.
- All work happens on a branch named `phase-N-<short-name>`. Open a PR; never push to `main`.

## Hard boundaries
- `data/gold/` is human-controlled ground truth. **Agents never write to it** — not
  with an editor tool, not with a shell command, not by invoking a script that does.
  Reading it is always fine.
  The ONLY permitted programmatic write is `scripts/check_gold.py --promote`,
  **invoked explicitly by the human**, and only after every check on the file passes:
  it strips the draft scaffolding keys, writes the record into `data/gold/`, and
  deletes the draft. An agent must not run `--promote` on the human's behalf — hand
  back the exact command and let them run it.
- Do not add dependencies beyond `requirements.txt` / `package.json` without listing each new one in the PR description with a one-line reason.
- Do not touch `.env`, credentials, or deployment config unless the task explicitly says so.

## Orchestrated loop
- Phases are executed via `/run-phase N`: implementer -> checks -> reviewer
  (fresh context), repeated until the reviewer returns zero REQUIRED items,
  capped at 5 iterations.
- A PreToolUse hook physically blocks writes to `data/gold/` and `.claude/`, including
  agent-invoked `--promote`. Reads are allowed. A BLOCKED message is a signal to stop
  and report, never to route around — including by rephrasing the command.
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

## Environment
- All Python tooling runs from the project venv: `.venv/bin/ruff`, `.venv/bin/pytest`.
  A bare `ruff` is not on PATH; a bare `pytest` resolves to Anaconda's 3.9 install
  and dies on missing deps — a real environment error that looks like a test failure.
