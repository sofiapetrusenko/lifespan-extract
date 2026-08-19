# DEBT.md — known-incomplete work

**Scope: work that needs doing.** Not decisions, not rationale, not loop history.

**One item lives in exactly one file; the others reference it.**

| file | holds |
|---|---|
| `DEBT.md` | known-incomplete work that needs action |
| `NOTES.md` | design decisions and their arguments, including the index of decisions reserved to the human |
| `LOOP_LOG.md` | what each review iteration found and what was done about it |
| `PR_DESCRIPTION.md` | what one PR changed, and a pointer here |

A **reserved decision is not debt** — it stays in NOTES.md's index. Where a
reserved decision has actionable work hanging off it, the entry below names the
work and links to the NOTES.md entry for the decision; it does not restate the
argument.

**Dates.** Each entry names the source that first wrote it down, in this order:
a dated NOTES.md entry where there is one; otherwise the LOOP_LOG.md iteration
that found it, LOOP_LOG.md not being dated per iteration; otherwise
**2026-08-13**, the day the Phase 2 review loop's findings were collected here.
The fallback is now D8 alone — every other entry is traceable to a NOTES.md
entry or to a named iteration.

**Status.** Nothing below blocks Phase 2's own contracts. The Phase 2 exit
condition was "no open finding contradicts a stated invariant in PLAN.md,
CLAUDE.md, a docstring, or the argparse help text", and every entry here was
checked against it.

---

## Correctness, bounded

**D1 · `extract/model.py::_create`** — *found iteration 9a · decides: implementer, Phase 3*
Non-SDK exceptions still escape as themselves: an unexpected kwarg, an
unserialisable body, anything that is not an `anthropic.AnthropicError`. **Risks**
one paper's failure stranding the rest of a batch with a bare `TypeError`,
which is the guarantee `call_structured`'s docstring makes. Unreachable today —
every request key is valid in anthropic 0.121.0 and the schema is derived from a
JSON file — so the guarantee holds because the request dict is fixed, not
because the catch is total.

**D2 · `extract/model.py::parse_payload`** — *found iteration 9a · decides: implementer, Phase 3*
Duplicate keys in a model object are silently last-wins; `object_pairs_hook` is
unset. **Risks** a response that says two contradictory things about one field
being read as one of them with nothing recording that the other existed.

**D3 · `extract/model.py::_FENCE_RE`** — *found iteration 6a · decides: implementer, Phase 3*
Dead against the suite: deleting the fence rule leaves every test green, because
the outermost-brace slice already recovers every fenced payload tested. It is
load-bearing only for a fenced payload containing no braces. **Risks** the rule
regressing unnoticed and that payload class then failing its paper.

**D4 · `extract/schema.py::load_schema`** — *found iteration 8a · decides: implementer, Phase 3*
Converts two read failures into `ExtractError`; `IsADirectoryError`,
`PermissionError` and `UnicodeDecodeError` escape as themselves. **Risks** a
startup misconfiguration arriving as a raw traceback rather than as the named,
actionable error every other configuration failure in this package raises.

**D5 · `extract/schema.py::build_extraction_schema`** — *found iteration 8a · decides: implementer, Phase 3*
`experiments.get("description", "")` silently substitutes an empty description
into the request schema, where every other malformed-schema case raises.
**Risks** the model being sent an unlabelled `experiments` array — prompt content
no validated field would reveal — after a schema edit drops the description.

**D6 · `extract/extract.py::_ID_PATTERN`** — *found iteration 8a · decides: implementer, Phase 3*
Duplicates the identifier regex that `schema/experiment.schema.json` also
declares, with nothing pinning the two equal. **Risks** the generator accepting
an id the schema rejects, or the reverse, after either side is loosened. Same
class of drift as D24, which names it from the other side.

**D7 · `extract/cli.py::run_extraction`** — *found iteration 4b · decides: implementer, Phase 3*
The write root is `resolve_out_root(out_root) / version`, and `schema_version`
pins no shape on the version string. The gold-set guard covers the parent, not
the full write root. **Risks** nothing today — the schema file is
human-controlled and reads `0.4.0` — but the guard's coverage is narrower than
the path it guards.

**D8 · `extract/cli.py::_report`** — *found 2026-08-13 · decides: implementer, Phase 3*
The skip counts describe only rows scanned before `--limit` was reached, but the
message reads "N **stored** paper(s) have no abstract". **Risks** an operator
reading a partial scan as a corpus-wide statistic.

**D9 · `extract/cli.py::build_parser`** — *found iteration 8b · decides: implementer, Phase 3*
`--limit`'s help text names only one of the two skips that do not consume the
limit; the no-abstract skip is documented only on `_papers`' docstring.
**Risks** an operator sizing a run against a rule the help text states
incompletely.

## Comments that misstate why the code is safe

**D10 · `extract/model.py`, the `RecursionError` arm** — *found iteration 8a · decides: implementer, Phase 3*
The in-code comment says `repair_json` "only ever removes text around the
object", which is false: the outermost-brace slice does remove nesting. Two
sibling comments are wrong the same way. The behaviour is conservative refusal —
the safe direction — so what is wrong is the stated reason, not the code.
**Risks** the next reader relying on a property the function does not have.

## Coverage holes

**D11 · `extract/extract.py` — `MAX_TOKENS` and all three system prompts** — *found iteration 9a · decided: human — measured by Phase 3's evals*
Setting `MAX_TOKENS = 100`, or replacing `classify.SYSTEM_PROMPT`,
`IDENTITY_SYSTEM_PROMPT` or `OUTCOME_SYSTEM_PROMPT` with a stub, leaves the suite
green. Deliberate: the `not_reported` / `no_effect` / median-vs-max rules PLAN.md
names as the hardest fields are prose, and a unit test asserting prompt text pins
the wording rather than the behaviour. The two-call split widened it —
`OUTCOME_SYSTEM_PROMPT` carries a prompt-only guarantee, that the second call
reports against the fixed experiment list instead of re-deriving it, and nothing
in the suite can pin it. **Risks** nothing silently wrong (the merge rejects the
paper loudly when that guarantee fails) but an *unmeasurable rate* of failed
extractions until a live run. The decision here is **made, not open**: the human
ruled that a unit test asserting prompt text pins the wording rather than the
behaviour, and that Phase 3 is where these rules get measured — LOOP_LOG.md,
`iter 9 fixes`, item 4. What is left is work (build the eval), which is why this
is debt and deliberately not in NOTES.md's index of open questions.

**D12 · `extract/model.py`, the excerpt coordinate space** — *found iteration 8a · decides: implementer, Phase 3*
The coordinate-space fix has three unpinned siblings. All correct today; none
guarded by a test. **Risks** a windowed excerpt silently pointing at the wrong
region of a payload, which is the one thing an excerpt exists to get right.

**D13 · `extract/cli.py::_check_writable`** — *found iteration 8b · decides: implementer, Phase 3*
The probe mechanism is unpinned: substituting `os.access` leaves the suite green,
though the docstring explicitly refuses that approximation and names the
full-disk case `os.access` cannot detect. **Risks** the probe regressing to the
approximation it was written to avoid, and a batch spending model calls before
discovering it cannot write.

**D14 · `extract/cli.py`, parser defaults** — *found iteration 9b · decides: implementer, Phase 3*
Both parser default *values* are unpinned; only their agreement with `--help` is.
`DEFAULT_LIMIT` carries PLAN.md's "20 unseen papers". **Risks** the Phase 2
Definition of Done's number changing with `--help` still agreeing with it.

**D15 · `extract/cli.py`** — *found iteration 7b · decides: implementer, Phase 3*
`_report`'s already-extracted branch and `record_path`'s readable-slug half are
both deletable with the suite green. **Risks** the slug half rotting into a
digest-only filename, which is the half that makes `data/extracted/` readable.

**D16 · `extract/cli.py`, `ExperimentIdCollisionError`** — *found iteration 5b · decides: implementer, Phase 3*
Counted-and-continued at the CLI by inheritance from `ExtractError`, with no
CLI-level test pinning it. **Risks** a collision becoming a batch-ending failure
if the handler is ever narrowed.

**D17 · `extract/classify.py::classify`** — *found iteration 8 fixes · decides: implementer, Phase 3*
No SDK-error test drives the `classify` entry point; all of them drive
`call_structured` directly. The shared code path is pinned; the entry point is
not. **Risks** the gate acquiring its own error path and nothing noticing.

**D18 · `extract/extract.py::_claim_value`** — *found iteration 9a · decides: implementer, Phase 3*
The `value.strip()` arm is unpinned, though it decides whether a whitespace-only
`species` falls through to the `other` organism component or raises. **Risks**
exactly the fixture failure NOTES.md's second rule keeps catching — the guard
keys on whitespace and no fixture supplies any.

## Deferred by decision, in ingest

**D19 · cross-run canonical identity for a preprint that later publishes** — *found 2026-08-10 (NOTES.md) · decides: implementer, whenever a phase needs it*
Correcting a stored row's `doi` *and* its `dedup_key` — the primary key — then
merging against a PubMed row already at the target key is a reconciliation pass
with a row-merge policy, and it contradicts the never-overwrite guarantee
idempotent re-ingest is built on. **Risks** one paper counted twice across two
runs. Phase 3's DrugAge cross-validation is the first plausible customer.
Argument: NOTES.md, *2026-08-10 — Phase 1 ingestion (`ingest/`)*, section 5.

**D20 · ingest provenance is not recorded per row** — *found 2026-08-10 (NOTES.md) · decides: implementer, when evals want to describe a corpus*
`fetched_at` is stored; the query that produced a row is not, so "which papers
came from which run" is unanswerable. **Risks** a Phase 3 corpus description
that cannot be reconstructed after the fact.

**D21 · `RecursionError` escaping `response.json()` in `ingest/{biorxiv,pubmed}.py`** — *found iteration 7 fixes · decides: implementer*
Phase 1 code already on `main`, so out of scope for the Phase 2 PR. Tracked at
[#3](https://github.com/sofiapetrusenko/lifespan-extract/issues/3). **Risks** the
same batch-ending traceback D1 describes, one layer earlier.

**D27 · a parser fix never reaches rows already stored** — *found 2026-08-17 (NOTES.md, the PubDate entry) · decides: human*
Ingest is idempotent on row *existence*, not on row *content*: `store_papers`
skips a `dedup_key` it already holds and never compares the parse behind it. So
a parser fix applies only to rows created after it. Measured 2026-08-17, after
`_year()` was fixed: of 217 rows, the 28 stored pre-fix still carry `year IS
NULL`, while 187 of 187 stored post-fix parsed a year — the split is exactly the
fix date, and re-running the same query repairs nothing. **Risks** a corpus that
silently mixes parser generations, with no way to tell a row whose field is
genuinely absent from one whose parser could not read it; downstream, extraction
fails loudly on each stale row, which is correct but unfixable by re-running
ingest. Closing it needs one of two mechanisms, and which is the human's call: a
`--refresh` mode that re-parses stored rows, or a parser version stamped on each
row so stale ones can be identified and targeted. **Not a Phase 2 blocker** —
Phase 2 needs 20 fresh papers and post-fix rows are clean. It becomes one in
Phase 3 if the corpus is ever re-parsed, or if an eval is run over rows spanning
the fix.

**D29 · four extracted records are attributable to no logged run** — *found 2026-08-19 · decides: nobody, not repairable*
`runs/2026-08-18/run.log` is 0 bytes. A first invocation created it and died;
the run that produced records was then driven by hand without `tee`, so its
output exists only on a screen that is gone. Four files in
`data/extracted/0.4.0/` — `doi-10-1007-s11357-023-00978-0-80c3dcba.json`,
`doi-10-1007-s11357-025-02078-7-e53b5c2c.json`,
`doi-10-1007-s11357-026-02201-2-e1b02af8.json`,
`doi-10-1007-s12011-025-04646-6-b5ec5ebe.json`, written 09:30–09:33 on
2026-08-18 — are named by no log. The 2026-08-17 log accounts for the other
four and for no more: it reports `4 extracted`, and its four filenames are the
other four on disk. **Risks** nothing in the records themselves, which are
schema-valid and were verified as such; what is lost is the ability to say which
command, which limit and which corpus state produced them, which is the evidence
a Phase 3 result would be read against. **Not repairable retroactively** — the
run's argv, queue size and screening decisions were never written down, and
nothing on disk can reconstruct them. This is what motivated the provenance
header now emitted before the first paper line (`extract/cli.py::run_extraction`),
which records the UTC timestamp, argv, limit, out_dir, schema version and queue
size, so a future log ties itself to the command that produced it. The header
does not close this entry; it stops the next one being opened.

## Work hanging off a decision reserved to the human

These name the *work*; the decision itself is in NOTES.md's index and is not
repeated here.

**D22 · make `species` required when `organism == "other"`** — *found 2026-08-13 (NOTES.md) · decides: human*
The work is a v0.5.0 schema change and is upstream of this package, so it is
deliberately not worked around in Phase 2 code. **Risks** Phase 3 scoring two
organisms as one. Decision and the argument for it: NOTES.md index, *`species`
required when `organism == "other"`*.

**D23 · decide the verbatim check's haystack, then implement it** — *found 2026-08-13 (NOTES.md) · decides: human*
One collapsed haystack today; the alternative is title and text as two regions.
Whichever the human picks, `extract/schema.py::check_quotes_verbatim` is where
it lands. **Risks** a fabricated quote passing the check the whole project's
trust rests on. Decision, and the measurement that sizes the trade: NOTES.md
index, *The title/abstract straddle*.

**D24 · fold the `NON_NULLABLE_QUOTE_CLAIMS` override into the schema file** — *found 2026-08-13 (NOTES.md) · decides: implementer, when v0.5.0 opens*
`extract/schema.py::_non_nullable_quote` restates a fact
`schema/experiment.schema.json` does not carry. The shape of the change is
already written down — a `provenance_quoted` `$def` and a `claim_string_quoted`
so `intervention.agent` stops sharing `claim_string` with `strain` — so this is
work, not a decision. **Risks** the same drift as D6: the derivation and the file
disagreeing about what is nullable. Argument: NOTES.md, *The live run: the
endpoint rejects the derived schema*.

**D25 · re-read the six gold files for `confidence` before Phase 3 treats it as a signal** — *found 2026-08-11 (NOTES.md, Colman 2009 entry) · decides: human (the labeller)*
9 non-`high` values out of 266 measured. **Risks** a field that almost never
varies being scored by exact category match, which makes "always answer high" a
near-perfect strategy and the resulting number meaningless. Argument: NOTES.md,
Colman 2009 entry, *Confidence, and why one wrapper differs*.

**D28 · a third of extracted experiments are organisms PLAN.md puts out of scope** — *found 2026-08-17 (the first full Phase 2 run) · decides: human*
PLAN.md's MVP scope names three organisms — *C. elegans*, *M. musculus* and
*M. mulatta*. The classifier gates on whether a paper reports a
lifespan-intervention experiment, not on which organism it used, so nothing
between ingest and extraction enforces that scope. Re-measured 2026-08-19
across `data/extracted/0.4.0/` (9 files, 25 experiments): 12 *M. musculus*, 6
*C. elegans*, 6 *D. melanogaster*, 1 *S. cerevisiae* — **7 of 25 outside the
stated scope**. (First measured 2026-08-17 at 8 files, 24 experiments, 7 of 24;
the 2026-08-19 run added one *M. musculus*. The figure moves with every run —
re-derive it, do not quote it.) Nothing is malformed: the schema handles them
exactly as designed, `organism: "other"` plus the real name in `species`, which is what
that field was added for in v0.4.0. The mismatch is between PLAN.md and
behaviour, not inside the data. **Risks** a Phase 3 in which the gold set and
the corpus span different organism distributions — gold is 19 *M. musculus*,
3 *C. elegans*, 2 *M. mulatta*, 1 each *S. cerevisiae* and *D. melanogaster*,
while the corpus is a quarter fly — so a per-organism accuracy breakdown would
rest on one or two gold records per out-of-scope organism, and an aggregate
number would be dominated by organisms the plan says are not being built for.
Two resolutions, both the human's and neither implemented: widen PLAN.md to
match what the pipeline usefully extracts, or gate on organism somewhere
(classifier prompt, a post-extraction filter, or the `organism` enum itself)
and accept discarding work already paid for. Note the choice is not free in
either direction — the gold set already contains a *D. melanogaster* and an
*S. cerevisiae* record, so gating would also strand gold data.

## Scheduled

**D26 · blind re-label of three gold papers, due 2026-08-18** — *found 2026-08-11 (NOTES.md) · decides: human (the labeller)*
`mattison2012`, `calubag2025`, `martinmontalvo2013`, selected at random to
measure single-labeller self-agreement for the README. **Risks** the README
quoting extraction accuracy with no measure of the ground truth's own
reproducibility.
