# Phase 2 — Classification + Extraction

Implements PLAN.md Phase 2: a cheap-model classification gate, a structured-output extractor
with JSON repair + single retry, loud failure everywhere, and a CLI. It also closes four
defects found by running the system rather than by testing it — see *Four defects, one shape*.

`ruff check .` clean. **1173 tests**, up from **485** on `main`.

**Diff size:** 29 tracked files, **+9655 / −53**, plus 7 new untracked files (414 lines) —
36 files in total. `data/gold/MANIFEST.sha256` is among the untracked files and was generated
by the human; including it is the owner's call.

## Phase 2's "Done when" is demonstrated

PLAN.md's exit criterion is *"pipeline runs end-to-end on 20 unseen papers without manual
intervention"*. Two logged runs meet it, neither with a failure:

```
runs/2026-08-17/run.log
  papers:   20 considered, 4 extracted, 16 screened out, 0 failed

runs/2026-08-19/run.log
  papers:   30 considered, 1 extracted, 29 screened out, 0 failed
  skipped:  8 already extracted against this schema version
```

Both lines are verbatim. `runs/` is gitignored, so the logs are not in this diff; they are on
the author's machine and reproduced above in full. The 08-17 run is the twenty-paper run the
criterion names. The 08-19 run re-entered the same corpus, correctly skipped the 8 records
already on disk, and extracted 1 more.

**One caveat, stated rather than buried.** `runs/2026-08-18/run.log` is **0 bytes** — an empty
file, no summary line of any kind. Four records in `data/extracted/0.4.0/` were written
between 09:30 and 09:33 that morning and are named by no log. That run was driven by hand
without `tee` and its argv, limit and screening decisions are gone. It is not repairable; it
is `DEBT.md` D29, and it is what motivated the provenance header now emitted before the first
paper line (`extract/cli.py::run_extraction` — UTC timestamp, argv, limit, out_dir, schema
version, queue size). The header does not close D29; it stops the next one being opened.
The two logs above pre-date the header and so carry only a `schema:` line.

## What was built

- **`extract/classify.py`** — cheap-model gate (`claude-haiku-4-5`) answering "does this paper
  report lifespan-intervention data?". The cost cascade is pinned by value, not by wiring: a
  test asserts the gate model is strictly cheaper than the extraction model, so setting the two
  equal fails.
- **`extract/extract.py`** — one experiment record per (organism, intervention); multi-organism
  papers yield multiple records. Every claim carries `source_quote`, `confidence` and
  `extracted_from`. **Extraction is two requests, not one** — see below.
- **`extract/model.py`** — the model boundary. JSON repair heuristic, exactly one retry, then
  raise with a windowed excerpt centred on the failure. Every SDK failure and every malformed
  payload is converted to an `ExtractError` subclass, so a batch can handle one paper's failure
  by catching one type.
- **`extract/schema.py`** — derives **both** request schemas from
  `schema/experiment.schema.json` rather than restating either; validates returned records
  against the real schema; verifies every `source_quote` is verbatim in the exact prompt string
  the model was shown. Carries the one deliberate exception to "derive, never restate":
  `NON_NULLABLE_QUOTE_CLAIMS` asserts of four claims that their quote is never null, which the
  schema file does not say. Because it restates a fact the file does not carry, the fact is
  pinned against the data rather than asserted in a comment — a test fails if any of those four
  quotes is ever null anywhere in `data/gold/`. Measured now: **0 nulls across all 26 gold
  records.** Folding it into the file is `DEBT.md` D24.
- **`extract/cli.py`** — `python -m extract [--limit N] [--out DIR]`, following Phase 1's
  pattern. Atomic writes, per-paper failure isolation, a writability probe before any spend,
  a run-provenance header, and a hard refusal to write anywhere inside `data/gold/`.
- **`extract/errors.py`** — seven typed errors, all re-exported from the package, with a test
  deriving the expectation from the module so the surface cannot drift again.

## The two-call split (A + D), and its live results

The first live run returned **HTTP 400**: the structured-output endpoint compiles at most **16**
union-typed parameters in a request schema, and the schema derived for the whole experiment
object carried **24**. Nine review iterations could not have caught it — every test stubs
`messages.create` and validates the request schema against `jsonschema` rather than against the
service. The human chose two changes together, over three rejected alternatives (NOTES.md,
*2026-08-13 — The live run: the endpoint rejects the derived schema*):

- **A — drop nullability on four `source_quote`s** whose value can never be absent
  (`organism`, `intervention.type`, `intervention.agent`, `lifespan_effect.direction`), and
  which are non-null in all 26 gold records. Implemented as an override on the *derived* schema
  only (`extract/schema.py::NON_NULLABLE_QUOTE_CLAIMS`), not as a v0.5.0 schema change, because
  opening v0.5.0 mid-phase would move the gold-set contract mid-labeling.
- **D — split extraction into two requests**, partitioning the experiment's properties:
  `IDENTITY_PROPERTIES` (the experiment as designed) and `OUTCOME_PROPERTIES` (what it found).
  Call 1 fixes the list of experiments and their order; call 2 sees the same paper prompt plus a
  numbered echo of call 1's output and returns one outcome per listed experiment, each carrying
  its `experiment_index`. `_merge_outcomes` rejoins them, refusing anything that is not a
  one-to-one join.

The arithmetic, measured off the schemas the extractor actually emits rather than restated:
**24 → 20 (A) → 10 + 10 (D)**, against an endpoint limit of 16 and a self-imposed
`UNION_BUDGET` of 12.

Two things the split had to get right and does. The **quote haystack stays the paper prompt
alone** — the echoed list is the model's own output, so a "quote" lifted from it verifies while
appearing nowhere in the paper, and a test drives exactly that fabrication. And a paper whose
first call succeeds and whose second fails **produces no record and no file**: `extract_record`
has no early-return path, and nothing writes until it has returned.

### It has now been exercised live

**9 records on disk under `data/extracted/0.4.0/`, carrying 25 experiments.** Five are
attributable to a run log (4 to 08-17, 1 to 08-19); four are the unlogged 08-18 set. No run
reported a failure.

Corroboration that these are A+D products rather than pre-split leftovers: the pre-split schema
cannot produce a record at all — the endpoint refuses it — and `extract/extract.py` was last
written before the earliest record. All 25 experiments carry a non-null `source_quote` on all
four `NON_NULLABLE_QUOTE_CLAIMS` (0 exceptions), and 0 records leak `experiment_index`, which
`_merge_outcomes` consumes and never writes.

**Organism and species across the 25 experiments:**

| organism | n | | species | n |
|---|---|---|---|---|
| `M. musculus` | 12 | | *(null)* | 18 |
| `other` | 7 | | `Drosophila melanogaster` | 6 |
| `C. elegans` | 6 | | `budding yeast (Saccharomyces cerevisiae)` | 1 |

**7 of 25 experiments are organisms PLAN.md's MVP scope does not name** (*C. elegans*,
*M. musculus*, *M. mulatta*). Nothing is malformed — that is exactly what `organism: "other"`
plus `species` was added for in v0.4.0 — but it is a mismatch between PLAN.md and behaviour,
and it is the human's to resolve. `DEBT.md` D28.

## The rest of the diff

The extractor is not the whole of this branch. Roughly 40% of it is elsewhere, and all of it is
in scope for review:

- **`ingest/pubmed.py`** — `_YEAR_RE` and `_year` rewritten. See *Four defects* below.
- **`.claude/hooks/protect_paths.py`** — the PreToolUse guard. `HUMAN_ONLY_SCRIPTS` widened
  from `check_gold` alone to `("check_gold", "validate_gold")`, and `--write-manifest` added to
  `HUMAN_ONLY_FLAGS`. Adding the flag alone did nothing: `check_promote` returned before the
  flag loop ran, leaving the manifest writer reachable by an agent. The widening costs one
  conservative false positive — a command naming one of those scripts beside one of those flags
  is refused whether or not it writes. Accepted at that price and argued in NOTES.md
  (*2026-08-19*); it fired three times while this description was being assembled.
- **`.claude/settings.json`** — the PreToolUse matcher gains `NotebookEdit` (see below), and
  both hook commands are absolutised to `"$CLAUDE_PROJECT_DIR/.claude/hooks/protect_paths.py"`
  so the guard resolves regardless of the harness's working directory.
- **`.github/workflows/ci.yml`** — two changes. The `pytest || [ $? -eq 5 ]` escape hatch is
  removed; it existed because no tests were collected before Phase 2 and now only masks a
  zero-collection run. And a second job, **`hook-guard-py39`**, runs
  `pytest tests/test_protect_paths.py` under Python 3.9. It exists because the behavioural
  3.9 test skips wherever no 3.9 interpreter is present — including the 3.11 job above — so
  without this job the regression that made the guard inert is unpinned exactly where it would
  go unnoticed longest.
- **`scripts/validate_gold.py`** — gains integrity checking against
  `data/gold/MANIFEST.sha256`: fails on any gold file whose digest changed, any file absent
  from the manifest, and any manifest entry whose file is gone. The format is plain
  `sha256sum`, so the human can verify without trusting this script.
- **`data/gold/MANIFEST.sha256`** *(untracked; human-generated)* — 10 digests for 10 gold
  files. `shasum -a 256 -c` run from inside `data/gold/` returns **10 × OK, exit 0**. What it
  does **not** attest is written down in NOTES.md (*2026-08-18*): it establishes "unchanged
  since the manifest was generated", and it was generated weeks after the window in which the
  guard was inert. It is a tripwire forward, not a chain of custody backwards.
- **`.gitignore`** — `runs/` added. A run log is evidence of a run, not an artefact of the
  repo, and is regenerable by re-running the extractor.
- **Three new test files** — `tests/test_pubmed_pubdate.py` (150 lines, over three verbatim
  efetch fixtures under `tests/fixtures/pubmed_pubdate/`), `tests/test_validate_gold.py` (110)
  and `tests/test_validate_gold_manifest.py` (135).

## Four defects, one shape

Four defects were closed this phase. **Every one of them is the same failure: a component was
tested against a shape the live system does not produce.** All four suites were green while the
component was wrong or inert in production, and in each case the fixtures had been written from
the same assumption as the code. That is the finding of this phase, more than any single fix.

**1. The request schema the endpoint refuses.**
*Artefact:* HTTP 400 on the first live run — 24 union-typed parameters against a limit of 16.
*Shape not produced by tests:* every test stubs `messages.create` and validates the request
against `jsonschema`, which has no opinion about the service's own limits.
*Fix:* A + D, above. *Pinned by:*
`tests/test_extract_schema.py::test_each_request_schema_stays_well_under_the_union_limit`,
which counts unions in the schemas the extractor actually sends and holds them at ≤ 12. It
cannot prove the endpoint accepts them; it turns a creep back over the limit into a number
rather than an HTTP 400 on the first paper of the next run.

**2. The guard sat inert under Python 3.9.**
*Artefact:* commit `f9fb7a6`. `list[str] | None` in an annotation is evaluated at import under
3.9, so the hook raised `TypeError` and exited 1 — and PreToolUse reads any non-2 exit as
"hook errored, proceed". With `skipDangerousModePermissionPrompt` on, `data/gold/` had no
enforced protection at all. (`git log -- data/gold/` was checked: every commit is human;
nothing was written programmatically while the guard was down.)
*Shape not produced by tests:* every test drove the hook under the venv's 3.11; the harness ran
it as a shell command string under `python3`, which on this machine resolves to 3.9.
*Fix:* `from __future__ import annotations`, and fail closed — an unexpected exception now
exits 2, not 1. *Pinned by:* `test_the_hook_parses_under_an_older_python` (runs the hook under
a real 3.9) and `test_annotations_are_safe_for_the_oldest_supported_python` (a static
`ast.parse(..., feature_version=(3, 9))` plus a PEP 604 scan, which is the only one of the two
that fires on a 3.11-only runner — hence the `hook-guard-py39` CI job).
*Measured blast radius:* removing the `__future__` import from a candidate hook turns **2**
tests red.

**3. `NotebookEdit` was inspected by the guard and routed to it by nothing.**
*Artefact:* `git show main:.claude/settings.json` — matcher `"Edit|Write|MultiEdit"` — against
`main`'s hook, which already read `notebook_path` for `NotebookEdit`. The shipped harness reads
an alphanumeric matcher as an **exact-name list**, not a regex, so `"Edit"` does not cover
`"NotebookEdit"` by substring. The predicate is ported into `tests/test_protect_paths.py`
rather than guessed.
*Shape not produced by tests:* every hook test invokes the hook directly as an argv and so
bypasses routing entirely — none of them could see that the guard was never consulted.
*Fix:* matcher widened to `"Edit|Write|MultiEdit|NotebookEdit"`.
*Pinned by:* `test_notebook_edit_is_routed_to_the_guard`, which reads `.claude/settings.json`
rather than the hook. *Measured blast radius:* reverting the matcher turns **1** test red —
and `test_every_guarded_tool_is_routed_to_the_guard` is not it, because `GUARDED_TOOLS` still
excludes NotebookEdit by design.

**4. `_year` was tested against a PubDate shape PubMed does not usually send.**
*Artefact:* a Phase 2 live run failed loudly on **28 of 30** ingested papers with
"paper.year is missing". `_year` searched the *flattened* PubDate, and `itertext()`
concatenates children with no separator, so `<Year>2025</Year><Month>Sep</Month><Day>23</Day>`
arrived as `2025Sep23` — where the old pattern's trailing `\b` cannot match, a digit followed
by a letter not being a word boundary. Only a Year-only PubDate ever parsed.
*Shape not produced by tests:* every existing ingest fixture happened to carry a PubDate that
flattened cleanly.
*Fix:* read `PubDate/Year` directly, with `MedlineDate` free text as the fallback it was always
meant to be; `_YEAR_RE` moves from `\b…\b` to `(?<!\d)…(?!\d)` so it still rejects `12025` and
`20255`. The alternative — joining `itertext()` with spaces — was rejected because `_text` also
flattens `ArticleTitle`, where inline markup is intra-word: measured against live XML it turns
`H2O2` into `H 2 O 2`.
*Pinned by:* `tests/test_pubmed_pubdate.py`, over three **verbatim** efetch responses, one per
PubDate shape the live run produced. *Measured blast radius:* reverting the fix turns **4**
tests red, all of them in that new file — **no pre-existing ingest test catches any of it**,
which is the point.

A parser fix does not reach rows already stored: ingest is idempotent on row *existence*, not
row *content*. That is `DEBT.md` D27, and it is the human's call.

## Invariants enforced (and tested)

- **Absent data is `not_reported`, never guessed.** Enforced in both directions: an absent
  value may not carry a quote, and a present value may not lack one.
- **Model output is untrusted input.** Repair → one retry → raise. Non-JSON float constants
  (`NaN`, `Infinity`), floats that overflow to infinity (`1e400`), integer literals Python
  refuses (>4300 digits), and payloads nested past the decoder's stack limit are all refused
  with a windowed excerpt rather than reaching a record.
- **`source_quote` is verified verbatim** against the exact prompt the model saw, title
  included. A fabricated quote invalidates the whole record.
- **`extracted_from` is asserted on a record read back off disk**, not just on a return value.
- **Nothing writes to `data/gold/`.** `resolve_out_root` resolves symlinks and `..`, compares
  case-folded path *components* (the dev filesystem is case-insensitive; a prefix match would
  also swallow `data/goldilocks`), and refuses. It is the chokepoint every write passes through.
- **Idempotent per (paper, schema_version).** Records are written via `mkstemp` + `os.replace`,
  so an interrupted run never leaves a truncated file that a later run reads as
  "already extracted" — the file's existence is the only idempotence marker. The 08-19 run's
  `skipped: 8` is that invariant working on live data.
- **A run with failures cannot report success.** `python -m extract` carries `main`'s status
  out to the shell; the shim is exercised through `runpy`, so reducing `sys.exit(main())` to a
  bare `main()` fails a test rather than silently exiting 0 on a failed batch.
- **Two distinct agents can never share an `experiment_id`.** A collision raises rather than
  taking a numeric tail; the same (organism, agent) pair repeated in one paper still gets `-2`.

## New dependencies

- **`anthropic>=0.77`** — the model client. The floor is `0.77` specifically because
  `output_config` (structured output) first shipped there; the `0.69` floor originally proposed
  predates it and would have installed a version without the parameter the code depends on.
- **`jsonschema>=4.18`** — promoted from `requirements-dev.txt` to `requirements.txt`, because
  `extract/schema.py` validates records at runtime, not only in tests. CI installs both files,
  so nothing under `scripts/` is orphaned by the move.

The `hook-guard-py39` CI job installs `pytest` and `httpx` into a bare 3.9 interpreter. Those
are not new project dependencies — the job runs one test file against an interpreter the
project does not otherwise use.

## Deviations from PLAN.md

- **`data/extracted/<schema_version>/` is a new directory**, now documented in PLAN.md's
  architecture block and in a dated NOTES.md entry. Extracted records are written there as
  JSON, one file per paper. Whether they ultimately belong there or in Postgres is open — see
  open questions.
- **Extraction costs two Opus calls per extracted paper — roughly double.** PLAN.md Phase 2
  describes one structured-output call per paper; the endpoint's 16-union limit made that
  impossible without changing how absent data is represented, which is a gold-set and eval
  decision rather than an implementation one. **The classifier gate is unchanged**, so a
  screened-out paper costs exactly what it did before, and the cost cascade PLAN.md asks for is
  intact — the doubling falls only on papers that pass the gate. The 08-19 run is the shape of
  that in practice: 29 of 30 papers never reached an Opus call. Reversing the split means
  reopening the representation question (NOTES.md index, *How absence is represented in the
  request schema*).
- **The MVP organism scope is not enforced anywhere.** 7 of 25 extracted experiments are
  outside it. `DEBT.md` D28; the resolution is the human's.

## Known items — accepted, not fixed

**They live in [`DEBT.md`](DEBT.md), not here.** One item, one file: this section used to
restate them, which meant two places to update and two places to go stale.

**Characterisation, re-derived from the file rather than remembered.** The entries run
**D1–D29** — 29 entries, no gaps — under six headings:

| n | heading |
|---|---|
| 9 | Correctness, bounded |
| 1 | Comments that misstate why the code is safe |
| 8 | Coverage holes |
| 5 | Deferred by decision, in ingest |
| 5 | Work hanging off a decision reserved to the human |
| 1 | Scheduled |

The phase's agreed exit condition was **no open finding contradicts an invariant stated in
PLAN.md, CLAUDE.md, a docstring, or the argparse help text**, and every entry was checked
against it. One needs the owner's ruling rather than mine: **D28** records a mismatch between
PLAN.md's stated MVP organism scope and what the pipeline extracts. Whether a scope statement
is an invariant in the sense above is the owner's call, not the implementer's, so it is flagged
here rather than silently cleared.

**The three that bear on merging** — read them in `DEBT.md`:

- **D22** — `organism: "other"` with no species defeats the id collision guard. Needs a v0.5.0
  schema change, so it cannot be closed in this PR.
- **D11** — `MAX_TOKENS` and all three system prompts are unpinned. Deliberate, and the human
  has ruled that evals rather than unit tests are the instrument.
- **D29** — four extracted records are attributable to no logged run. Not repairable; see the
  caveat at the top.

### Filed separately

A `RecursionError` escaping `response.json()` in `ingest/{biorxiv,pubmed}.py` — Phase 1 code
already on `main`, so out of scope for this PR — is tracked in
[#3](https://github.com/sofiapetrusenko/lifespan-extract/issues/3) and as `DEBT.md` D21.

## Open questions for the human

The authoritative list is NOTES.md's *Open questions reserved to the human* index, which
carries **eleven** as of this writing. The five below are the ones this PR is the occasion for,
summarised; the index is where they are kept.

1. **The `experiment_id` convention.** The generator follows the convention the schema
   documents and reproduces **11 of the gold set's 26** ids. The other **15**, across 7 papers,
   differ systematically: the labeller disambiguates semantically (`-male`/`-female`,
   `-low-dose`/`-high-dose`) where the generator has only the schema's numeric `-2`, and the
   labeller hand-normalises agent names. This is a schema/gold-set inconsistency, not only a
   code bug — the code does what the schema says. Left unchanged and pinned in both directions
   by `test_generated_ids_match_gold_except_where_pinned`.
2. **The eval alignment key.** NOTES.md previously recommended aligning gold to extracted
   output on `(organism, agent)` instead of `experiment_id`. That recommendation has been
   **withdrawn**: the pair is ambiguous for **12 of 26** gold records across **6** within-paper
   groups — including `eisenberg2009`, whose yeast and fly records are separated only by
   `species`, which the pair does not read. A 1:1 matcher on it would collapse them and report
   the collapse as agreement. No replacement is proposed; the key is a Phase 3 design decision.
3. **Where extracted records belong** — `data/extracted/<schema_version>/` as JSON, or Postgres.
   The filesystem gives a free crash-safe idempotence marker and diffable output during prompt
   iteration; a table is what PLAN.md's stack names and what Phase 4's `GET /experiments`
   filter set implies. Documented both ways in NOTES.md, undecided.
4. **Where the classifier's operating point actually lands.** The gate prompt previously
   instructed the model to answer true on genuinely ambiguous text, on the rationale that
   "the extraction step is the second gate". That rationale was wrong — `extract_record`
   raises when `experiments` comes back empty, so a false positive costs an Opus call and
   ends the paper as a batch error rather than being filtered cheaply. Worse, the bias
   targeted precisely the shapes `data/classifier_set/negatives.json` is built from. **The
   instruction has been removed** and replaced with a neutral one — judge the three criteria as
   written, and when the text does not settle one, say which and set confidence to low. No
   compensating bias toward false was added. The two live runs screened out 16 of 20 and 29 of
   30 with their reasons recorded per paper; whether those calls are *correct* is a Phase 3
   measurement, not something these logs establish.
5. **Does the verbatim check run against one haystack or per-region?** See `DEBT.md` D23. One
   haystack accepts a quote assembled across the title/abstract join; per-region rejects a
   quote that legitimately spans two sentences. The figures, the method and its caveats are in
   NOTES.md's quote-shape table.

## What remains unverified

The live round trip has happened, repeatedly, and the schemas at 10 unions each have been
accepted by the endpoint across three runs. What is still open:

- **Prompt quality on real papers**, including `OUTCOME_SYSTEM_PROMPT`'s prompt-only guarantee
  that the second call reports against the fixed experiment list instead of re-deriving it. Its
  failure is loud (the merge refuses the paper) and no run has reported one, but 9 papers is
  not a rate. `DEBT.md` D11.
- **Classifier precision and recall.** Unmeasured. Phase 3's job.
- **Every test still stubs `messages.create`**, so the suite cannot catch a request-shape
  regression. The union-budget test narrows that gap to a number but does not close it; only a
  live run can, and a live run is not part of CI.

## Review

**Every round is logged in LOOP_LOG.md, which is the count.** Rounds after the first ran as two
scoped passes (extraction core; interface and wiring), after a single full-diff dispatch failed
twice at the tool-use layer; iteration 1 was a single full-diff review. The loop was declared
closed at nine numbered iterations and **reopened for the A + D round**, because the live 400
arrived after it closed. The rounds since returned four REQUIRED, then five, then three, then
one, each fixed in turn.

### The review loop has not seen the last five days of this branch

`LOOP_LOG.md` was last written **2026-08-14**. Its final entry, *iter hygiene fixes, review*,
records the tree at **1096 tests**. The suite now stands at **1173**.

**Those 77 tests, and the code they cover, have had no logged review round.** That includes
every change described in *The rest of the diff* — `ingest/pubmed.py`, the guard, the matcher,
the CI job, `scripts/validate_gold.py`, the manifest, the `cli.py` provenance header — and
three of the four defects in *Four defects, one shape*. It also includes the live runs
themselves, which produced findings (D27, D28, D29) that no reviewer has checked.

This is stated rather than smoothed over because it is the single largest caveat on the branch.
A reviewer should treat roughly 40% of this diff as first-pass material.

### The cap and the bar were changed, by the human, mid-phase

CLAUDE.md still says what it always said: a cap of **5** iterations, and a definition of done
requiring the reviewer to return **zero** required changes. Neither was edited. Both were
overridden for this phase only, **by the repository owner**, and the overrides are recorded in
LOOP_LOG.md so that no agent could be said to have relaxed its own standard:

- **2026-08-12 — cap 5 → 7**, Phase 2 only. Rationale, in the owner's words: iterations 3 and 4
  were successive patches to one function against one class of defect, not independent review
  rounds.
- **2026-08-12 — cap 7 → 8, and the exit condition replaced.** "Zero REQUIRED" gave way to **no
  open finding contradicts a stated invariant**, where a stated invariant is one written in
  PLAN.md, CLAUDE.md, a docstring, or the argparse help text. Findings that contradict nothing
  written down are recorded rather than fixed — which is what `DEBT.md` is.

The loop then ran past 8 as well, on the same authority and for the same reason — the live
HTTP 400 arrived after the loop had closed. **Whether CLAUDE.md should be amended to match, or
the overrides should stay scoped to this phase, is a decision for the owner and is not taken
here.**

Decorative tests caught by mutation, and the systematic contract sweep with its declared blind
spots, are documented in NOTES.md's "Test honesty" section — counts and members there, not
repeated here.

The A + D round added the sharpest of them: **the two-call join by `experiment_index` was
indistinguishable from a positional join**, because `conftest.experiment_payload()`'s outcome
half was byte-identical across every experiment any test built. A reordered response is legal,
and under a positional join every outcome attaches to the wrong experiment while the record
still validates and every quote still verifies. Fixed in the fixture rather than in one test,
and pinned by mutation in all three forms — positional, reversed, rotated one place. The first
fix was itself not injective — it derived the outcome half from three arguments, merging the
male/female and low/high-dose pairs the gold set actually uses — so the derivation now reads the
whole identity half, and `conftest.outcome_payload` **refuses** any multi-experiment fixture
whose outcome halves coincide. A vacuous join fixture is now unwritable rather than merely
unwritten.
