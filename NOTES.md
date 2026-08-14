# Schema & design log

## Open questions reserved to the human

An index, not a discussion. Every decision this log has deliberately left open
is listed here with a pointer to the entry that argues it; the arguments are
not repeated, and nothing is resolved here. Added because the ten are scattered
across five months of entries in the order they came up rather than in any
order they would be read in.

Two things are deliberately *not* here, so the completeness claim above means
something. Work that is known-incomplete but whose decision is already made —
a deferral, a migration whose shape is written down, a coverage hole — is
tracked in `DEBT.md`, one item to one file. And a limitation that was weighed
and *accepted* is not an open decision; it stays in the entry that accepted it.

- **The `experiment_id` convention.** Semantic disambiguation (`-male`,
  `-low-dose`) as the gold set was labelled, or the schema's numeric `-2`,
  `-3` as the generator produces — and whether agent names are normalised
  before slugging. → *2026-08-12 — `experiment_id` generation does not
  reproduce the gold ids (open question for Phase 3)*
- **The eval alignment key.** What Phase 3 matches a gold record against an
  extracted one by, now that the `(organism, agent)` fallback has been
  withdrawn as not unique within a paper. → same entry, *What Phase 3 must do*
- **The title/abstract straddle.** Whether the verbatim quote check runs
  against one haystack or against the title and the text as two regions —
  a false-accept shape against a false-reject one. → *2026-08-13 — Two gaps
  the extractor cannot close on its own*, section *A quote straddling the
  title/abstract join verifies*
- **`species` required when `organism == "other"`.** A v0.5.0 schema change:
  without it, two genuinely different organisms in one paper can share an
  `experiment_id` and the collision guard does not fire. → same entry, section
  *`organism: "other"` with no species defeats the id collision guard*
- **Where extracted records live.** `data/extracted/` as JSON files, which is
  what the code does today, or Postgres, which the stack and Phase 4's
  filtered query assume. → *2026-08-13 — `data/extracted/`: where Phase 2's
  output goes*, section *Open question, reserved to the human: JSON files or
  Postgres*
- **How absence is represented in the request schema.** The null convention
  the gold set is labelled under is what puts the request near the endpoint's
  union limit; changing it is a schema and eval question before it is an
  implementation one. → *2026-08-13 — The live run: the endpoint rejects the
  derived schema*, section *Not decided here* (the A + D decision below it
  bought headroom without touching the representation, so the question stands)
- **How free-text fields are scored.** `age_at_start`, `strain`, `dose` and
  `mechanism` are open vocabulary, so exact match reports failures that are not
  failures — and the answer has to be a rule per field, not one global
  tolerance. → *2026-08-11 — labeling Mattison 2012: the splitting rule, the
  conflicting pair, and an eval question*, section *(c) Open question for Phase
  3 — free-text fields cannot be scored by exact string match*
- **Whether a null recorded because the paper is ambiguous scores as a null
  recorded because the paper is silent.** Distinguishing them needs the record
  to mark the difference, which it currently cannot. → *2026-08-11 — schema
  limitation found while labeling Calubag 2025: the unqualified percentage*,
  section *Phase 3 consequence — this one needs an eval decision, not just a
  schema decision*
- **The schema-shape candidates held for v0.5.0.** Per-statistic direction,
  survival-at-timepoint, multi-source provenance, and the statistic-qualifier
  redesign that would subsume the three `_change_pct` fields — four gaps found
  while labelling, none designed and none fixed unilaterally mid-labeling. →
  *2026-08-11 — schema limitation found while labeling Miller 2011*; *…while
  labeling Colman 2009*; *…while verifying strong2016 quotes: multi-source
  provenance*, section *v0.4.0 candidates now stand at three*; and *…while
  labeling Calubag 2025*, section *v0.5.0 direction*
- **Whether Phase 3 gets a held-out split.** Without one the classifier prompt
  is iterated against the set it is scored on, and the extraction gold set is
  the same ten papers, so both headline numbers are optimistic by an unknown
  amount. Closing it needs papers this project has not labelled. → *2026-08-11
  — eval design for the classifier set: ratio, headline metric, and a shared
  sample*, section *Known limitation: the positives and the extraction gold set
  are the same papers*

---

## 2026-08-08 — schema v0.1.0 (`schema/experiment.schema.json`, draft 2020-12)

1. **Provenance wrapper on claim fields only** — every extracted claim is `{value, source_quote, confidence, extracted_from}`; `paper.*` stays flat because bibliographic data comes from the ingest API, not the prose, so a `source_quote` for a DOI would be fiction.
2. **`confidence` is categorical `high|medium|low`, not a float** — the gold set is labeled by a human and humans cannot produce calibrated 0.88s; eval comparison is exact-match on a category instead of an arbitrary numeric tolerance.
3. **One file = one paper, with an `experiments[]` array** — multi-organism papers (Eisenberg 2009 spermidine) yield multiple entries rather than multiple files, keeping paper identity in one place.
4. **Required: `paper.doi/title/year/source`; per experiment `organism`, `intervention.type`, `intervention.agent`, `lifespan_effect.direction`** — the minimum that makes a record mean anything; everything else is optional and nullable.
5. **`not_reported` for closed enums, `null` for numerics** — `sex` and strain-like categoricals carry an explicit `not_reported` member; `median_change_pct`, `max_change_pct`, `sample_size` are nullable. No sentinel numbers, ever: `-1` is a legitimate percentage change.
6. **Closed enums** — `organism: C. elegans|M. musculus|other` (`other` lets the macaque gold papers be labeled truthfully; MVP filters won't surface them), `sex: male|female|mixed|hermaphrodite|not_reported`, `direction: increase|decrease|no_effect`, `intervention.type: pharmacological|genetic|dietary|other`, `extracted_from: abstract|full_text`.
7. **`p_value` is a nullable pattern-constrained string** — papers report `"< 0.001"`, which loses its inequality if coerced to a float.
8. **`schema_version` at file root, starting `0.1.0`** — extraction is idempotent per (paper, schema_version), so the version has to live in the record itself.
9. **Required `experiment_id` per experiment, flat string** — convention `<first-author-year>-<organism-slug>-<agent-slug>[-<n>]`, lowercase ASCII, hyphen-separated, e.g. `harrison2009-mmusculus-rapamycin`; the trailing `-<n>` disambiguates repeat (organism, agent) pairs in one paper. Assigned by hand in `data/gold/`, generated deterministically from the same convention in Phase 2. Flat rather than a claim wrapper: it is derived identity, not a quotable claim. Array position is not stable across re-extraction, so `/experiments` needed a real key.

### Consequences worth remembering

- `experiment` requires the three *parent* objects (`organism`, `intervention`, `lifespan_effect`) because JSON Schema cannot require `intervention.type` without first requiring `intervention`. Decision 4 is unchanged in effect.
- `median_change_pct` and `max_change_pct` are separate fields by design — PLAN.md Phase 3 names median/max confusion as an expected failure mode, and keeping them structurally distinct makes that error visible in per-field eval rather than silently averaged away.
- Objects are closed (`additionalProperties: false`, `unevaluatedProperties: false` on wrappers). An invented field is a loud failure, matching the "model output is untrusted input" rule.
- `strain` uses the literal string `"not_reported"` rather than `null` — it is open-vocabulary but strain-like, so it follows the categorical convention from decision 5.
- Open-vocabulary claim fields (`mechanism`, `dose`, `age_at_start`) use `null` rather than `"not_reported"`, so the sentinel can never collide with a real extracted value.
- `paper.source` is a closed enum `pubmed|biorxiv`. PMC open-access full text is **not** a third source: it is `source: "pubmed"` with `extracted_from: "full_text"` on the claims. Phase 1 ingest clients conform to the schema, not the reverse.
- `experiment_id` carries a `pattern` (`^[a-z0-9]+(-[a-z0-9]+)+$`), so the convention is enforced rather than merely documented. It accepts the awkward real cases — `strong2016-mmusculus-17-alpha-estradiol` and the `-<n>` suffix form — and rejects uppercase and whitespace. Loosen the pattern, not the convention, if a gold paper ever needs to break it.
- Version not bumped past `0.1.0` for these three changes (new required field, narrowed enum — both breaking). No records exist yet, so there is nothing to migrate. The first bump is owed the moment `data/gold/` has a file in it.

### Carried forward — Phase 2 requirement

- **Error reporting must surface ALL validator errors, never `errors[0]`.** One bad record routinely produces many independent errors, and `unevaluatedProperties` adds noise on top that can sort ahead of the real diagnosis. Reporting only the first would make the "loud failure with a windowed excerpt" rule emit confident nonsense. `scripts/validate_gold.py` already does this correctly — every error, sorted deepest-path-first so the specific message leads. Phase 2's extraction error path must match. Nothing to implement before Phase 2.
- **The `unevaluatedProperties` noise is version- and location-dependent** — measured, not assumed:
  - A failed `value` (declared in the wrapper's own `properties`, sibling to the `allOf`): jsonschema 4.4.0 emitted a spurious `Unevaluated properties are not allowed ('value' was unexpected)` alongside the real enum error; 4.26.0 does not. This is why `requirements-dev.txt` floors jsonschema at 4.18.
  - A failed `confidence` or `extracted_from` (declared inside the shared `#/$defs/provenance` subschema): **both versions** emit the noise. The whole `allOf` branch fails, so it contributes no annotations and all three provenance keys count as unevaluated. Expect roughly 2× error inflation on wrappers with a bad `confidence`.
  - Neither is a schema bug. Do not "fix" it by dropping `unevaluatedProperties` — that would let invented fields through, which is the opposite of the untrusted-input rule.

### Known limitations — accepted for MVP

- **Per-source full-text coverage is not answerable from records.** PMC open-access full text is encoded as `source: "pubmed"` + `extracted_from: "full_text"`, so a record cannot distinguish "PubMed abstract only" from "retrieved via PMC". Any Phase 3 statistic of the form "N% of bioRxiv records used full text" is computable, but "N% of records came from PMC" is not. Accepted: PMC is a retrieval detail, and `extracted_from` already captures the part that affects extraction quality. Revisit only if full-text provenance becomes a reported eval dimension.

### Tooling

- `scripts/validate_gold.py` validates every `data/gold/*.json` against the schema, prints all errors per file with dotted paths (`experiments[0].lifespan_effect.direction`), and exits 1 on any failure. Read-only with respect to `data/gold/`. An empty gold directory prints "nothing to validate" and exits 0 — legitimate during Phase 0, and stated out loud so an empty glob never resembles a clean run.
- `.github/workflows/ci.yml` calls `python scripts/validate_gold.py` directly. The inline heredoc it used to carry is gone: local and CI validation are now literally the same code and cannot drift.
- `schema/gold_template.json` is the copy-ready skeleton for hand-labelling: full required structure, two stubbed entries in `experiments[]` for multi-organism papers, every value `"TODO"` or `null`. **It fails validation by design** (91 errors when untouched) — a template that validated would let a half-filled record pass as done. Copy it into `data/gold/<paper>.json`, fill it, run `python scripts/validate_gold.py` until clean. Filling `confidence` and `extracted_from` first collapses most of the error count, since those two fields are what trigger the `unevaluatedProperties` inflation above.
- Dev environment is a project venv at `.venv` (Python 3.11, matching CI and PLAN.md), never the anaconda base env — whose Python is 3.9.12 and whose jsonschema is 4.4.0, old enough to change validator behaviour. `python3.11 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`. `.venv/` is gitignored.
- `ruff check .` runs clean from the venv with no config file, so CI's bare `ruff check .` sees identical settings. It caught `EXE001` (shebang without the executable bit) on `scripts/validate_gold.py`, fixed with `chmod +x` — the file is committed mode 100755.

### Resolved — do not reopen (v0.1.0)

- **Preprints and DOIs**: bioRxiv assigns DOIs under the `10.1101/...` prefix, so a preprint is never DOI-less. `paper.doi` stays required. Covered by a validation case.
- **Per-experiment identity**: settled by decision 9 above.

### Superseded — added 2026-08-11

- **Decision 6's `organism` enum is out of date. Do not act on it as written.** It records `C. elegans|M. musculus|other`, with `other` carrying the macaque papers. `M. mulatta` became a first-class member in v0.3.0, so the enum is now `C. elegans|M. musculus|M. mulatta|other`, and v0.4.0 added `experiments[].species` beside it for organisms still outside the enum. Decision 6 is left as written because it records what was decided on 2026-08-08 and why; the current state is in the v0.3.0 entry (*rhesus macaque in scope*) and the v0.4.0 entry (*`species`, and why `organism` was not extended*). The rest of decision 6 — `sex`, `direction`, `intervention.type`, `extracted_from` — is unchanged.

---

## 2026-08-08 — schema v0.2.0

**Change:** added `lifespan_effect.mean_change_pct`, same claim-wrapper shape as `median_change_pct`, nullable value. Ratified by the human.

**Rationale:** mouse papers report a mean / life-expectancy change with no pooled median percentage — Harrison 2009 is the motivating case. Under v0.1.0 that number had nowhere to go, forcing a choice between dropping it and misfiling it as a median. Additive and backward compatible: a v0.1.0 record still validates against v0.2.0, since the new field is optional.

### Labeling guidelines

- **Mean vs median are never substituted for one another.** If a paper reports only a mean, `median_change_pct` is `null` and the number goes in `mean_change_pct`. The reverse likewise. Median/mean/max confusion is already flagged in PLAN.md Phase 3 as an expected failure mode; the schema keeps all three structurally distinct so the error is visible in per-field eval rather than averaged away.
- **Age at 90% mortality / 90th-percentile survival maps to `max_change_pct`**, not to mean or median. This is the standard proxy for maximum lifespan and is how Harrison 2009 reports its headline numbers.
- **Abstract `source_quote`s are copied from the PubMed abstract version; full-text quotes from PMC.** Phase 1 ingests PubMed abstracts, so eval quote-matching must target that exact wording. A quote transcribed from the publisher's PDF abstract can differ in punctuation or hyphenation from the PubMed record and will fail string comparison.
- **Cohorts without a reported effect size are not separate records.** Interim analyses and unreported arms go in `notes`, not into `experiments[]`. A record whose `lifespan_effect` cannot be filled is noise in the eval denominator.
- **`proposed_mechanism` carries the stated mechanism of action only.** Speculative mechanisms raised in the discussion go in `notes`. The field records what the authors claim to have shown, not what they hypothesise.
- **Provenance — Harrison 2009:** labels drafted with AI assistance from the full text; values human-verified against the PDF and the PubMed record.

### Blocked at v0.2.0 — both resolved in v0.2.1 below

- **`notes` does not exist.** Two guidelines above route content to it (unreported cohorts, speculative mechanisms). There is no `notes` field at either paper or experiment level, and every object is `additionalProperties: false`, so adding the key to a gold file makes it fail validation. Needs ratification: per-experiment `notes` (nullable string, flat — it is commentary, not an extracted claim) is the smaller change; paper-level would not fit the "unreported cohort" case, which is experiment-scoped.
- **`proposed_mechanism` does not exist; the field is named `mechanism`.** Its description already says "Proposed mechanism as stated by the authors, not inferred", so the guideline matches the existing field's intent exactly. Either rename `mechanism` → `proposed_mechanism` (breaking, but no gold file uses it yet) or keep `mechanism` and treat the guideline as describing it. Not renamed unilaterally.

### Version drift to watch

- `scripts/validate_gold.py` hardcodes the version in its success message (`All N file(s) valid against schema v0.2.0`). The schema has no machine-readable version of its own — only the `schema_version` *instance* field — so this string must be bumped by hand on every schema release. Worth adding a root-level version keyword to the schema file if this drifts even once.
- `data/gold/harrison2009.json` declares `schema_version: "0.1.0"`. Not updated here: `data/gold/` is human-only per CLAUDE.md. It still validates against v0.2.0 (the change is additive), but the human should bump it when filling the file.

---

## 2026-08-08 — schema v0.2.1

Resolves both items blocked at v0.2.0, plus the version-drift note. Additive only: a v0.1.0 or v0.2.0 record still validates.

1. **Added per-experiment `notes`: nullable flat string.** Ratified. Flat rather than a claim wrapper, because it is labeller commentary rather than something extracted and quoted — it carries no `source_quote`/`confidence` and is never scored in evals. Destination for the two guidelines that previously had nowhere to write: cohorts with no reported effect size (interim analyses), and speculative mechanisms from the discussion. Paper-level `notes` was considered and rejected — the unreported-cohort case is experiment-scoped.
2. **`mechanism` keeps its name; no rename to `proposed_mechanism`.** Ratified. The v0.2.0 labeling guideline describes labeling policy — record the stated mechanism of action, send speculation to `notes` — not a field name. The field's own description already says "Proposed mechanism as stated by the authors, not inferred", so policy and schema agree.
3. **Schema declares its own version at the root: `"x-schema-version": "0.2.1"`.** `scripts/validate_gold.py` reads it instead of hardcoding a literal, and exits loudly if the key is missing. `x-` prefixed so it can never collide with a future JSON Schema keyword; validators ignore unknown keywords, so it has no effect on validation. This is now the single source of truth for the schema's version — bump it and the tooling follows.

### Two versions, deliberately

- `x-schema-version` (schema root) is what the *schema file* is. One place, bumped per release.
- `schema_version` (record root) is what an individual *record* was written against, and stays a `pattern` rather than a `const`/`enum`. Making it a const would pin every record to the current release and break the backward compatibility that additive bumps are supposed to buy — a v0.1.0 gold file would start failing on a bump that changed nothing relevant to it. The cost is that nothing enforces agreement between the two; per-field eval in Phase 3 is where a genuinely stale record should surface.

### Still open

- `data/gold/harrison2009.json` remains an unfilled copy of the template (all `TODO`/`null`) and declares `schema_version: "0.1.0"`. Untouched here — `data/gold/` is human-only. The v0.2.0 provenance guideline stating its values were human-verified does not yet describe the file on disk.
- `schema/example.json`'s `source_quote` was drafted, not transcribed from the PubMed record. Flagged for verification; left as is by decision.

---

## 2026-08-08 — labeling guidelines, addendum

- **Effect magnitude with an unspecified statistic goes to `notes`, not to a `_change_pct` field.** Wording like "lived twice as long" or "extends lifespan by 30%" without naming median, mean, or maximum leaves all three of `median_change_pct`, `mean_change_pct`, `max_change_pct` as `null`; the magnitude and the paper's exact wording go in `notes`. Guessing which statistic was meant is precisely the median/mean/max confusion PLAN.md Phase 3 sets out to measure — a guessed value would contaminate the ground truth the eval is scored against. A number in `notes` is recoverable later; a number in the wrong field is not.
- **Epistasis and genetic requirements are not `mechanism`.** "Requires *daf-16*", "abolished in *daf-2* mutants", and similar dependency findings state what the effect needs in order to occur, not the authors' stated mechanism of action. They go in `notes`. `mechanism` stays reserved for a mechanism the authors assert, keeping the `?mechanism=` API filter a claim about mode of action rather than a mixed bag of modes and dependencies.

---

## 2026-08-08 — environment note: the arm64/x86_64 venv hazard

The pre-commit hook once blocked a commit with "jsonschema is not installed" while the same script passed from an activated `.venv`. The package was installed the whole time. `.venv/bin/python3.11` is a **universal2** binary inherited from the python.org 3.11 framework build, and a universal binary starts in its *parent process's* architecture; `rpds`, a native dependency of `jsonschema`, ships **arm64-only** wheels (no universal2 wheel is published). So any x86_64 parent — this machine's anaconda toolchain is x86_64, as are Rosetta shells and some GUI git clients — launched the venv interpreter as x86_64, `dlopen` refused the arm64 `.so`, and the resulting `ImportError` was swallowed by a bare `except ImportError` that printed "not installed". Three fixes, in order of what actually matters: `.githooks/pre-commit` pins execution to the hardware architecture for both `python` and `ruff`, detecting it via `sysctl -n hw.optional.arm64` rather than `uname -m` (which reports the *process* arch and returns `x86_64` under Rosetta, pinning to precisely the wrong one); `scripts/validate_gold.py` now prints `sys.executable` and the verbatim `ImportError` instead of guessing at "not installed"; and the hook distinguishes "no venv" from "venv cannot provide X". **Rebuilding the venv under `arch -arm64` does not help and was verified not to** — the base interpreter is still universal2 and pip still installs arm64 wheels, so `arch -x86_64 .venv/bin/python -c 'import jsonschema'` fails exactly as before. The hazard is therefore permanent for manual invocation from an x86_64 shell; the hook is immune, and the error message now names the cause in one line. Related trap: bare `python` on this machine resolves to anaconda 3.9.12 carrying jsonschema 4.4.0, which is below the `>=4.18` floor in `requirements-dev.txt` and validates with the older `unevaluatedProperties` behaviour documented under v0.2.0 — always invoke `.venv/bin/python` explicitly.

---

## 2026-08-10 — Phase 1 ingestion (`ingest/`)

### 1. `RawPaper` is one flat row, and both sources project onto it

`ingest/models.py` defines a single SQLModel table, `raw_paper`. Its columns are exactly what Phase 2 needs to build a `schema/experiment.schema.json` record — `doi`, `title`, `year`, `source`, `pmid` map one-to-one onto the schema's `paper` object — plus the `abstract` itself and the three identity columns dedup runs on. Nothing else was added on spec.

The one column that is not strictly forced by the Phase 1 contract is `first_author`. It is there because the `experiment_id` convention ratified in decision 9 above is `<first-author-year>-<organism>-<agent>`, and ingest is the only stage where the author list is in hand; without it Phase 2 would have to re-fetch every paper to generate an id. Both clients normalise it to a bare surname (PubMed's `<LastName>`, bioRxiv's text before the first comma) so Phase 2 sees one shape, not two. Collective authors yield `None` rather than a guess.

### 2. The dedup key, and what happens when there is no DOI

`dedup_key` is the primary key: `doi:<canonical doi>` when a DOI is known, `<source>:<source_id>` when one is not. PubMed records genuinely arrive without a DOI (older citations, and some records where the publisher registered it later), and the fallback keeps two such papers distinct instead of collapsing them onto a shared sentinel. DOIs are lowercased and stripped on the way in, because they are case-insensitive and PubMed and bioRxiv do not agree on casing — `10.1038/NATURE08221` and `10.1038/nature08221` must not become two rows.

**The preprint/publication bridge is the non-obvious part.** A bioRxiv preprint DOI (`10.1101/...`) is *never* equal to its publication's journal DOI, so a naive "dedup by DOI" would never once fire on the pair PLAN.md actually names. bioRxiv's API reports the journal DOI in its `published` field, and that is what becomes `RawPaper.doi` for a published preprint; the `10.1101/...` DOI stays as `source_id`. That single substitution is what makes the preprint collide with its PubMed twin. Tested directly, both at the client level and end-to-end through the CLI.

This works **when both records arrive in the same run**. When the preprint was ingested *before* it was published, it does not — see the first known limitation below, which is the case PLAN.md's wording also covers.

`published` is the only field in either client whose *shape* is validated (`^10\.\d{4,9}/\S+$`) rather than merely normalised, and the asymmetry is deliberate. Every other malformed value produces a visibly wrong row; this one produces a *plausible* row that quietly stops matching its twin, and a dedup that silently fails looks exactly like a paper with no twin. A URL form (`https://doi.org/10...`), a `doi:`-prefixed string, a PMID or a truncated prefix therefore raises rather than becoming the record's primary key.

### 3. Dedup is enforced by the database, and only *chosen* in Python

Two separate jobs, deliberately kept apart:

- **The database enforces uniqueness.** `store_papers` issues `INSERT ... ON CONFLICT DO NOTHING ... RETURNING dedup_key` and counts the returned rows, so "already present" is what the database actually did, not what a prior `SELECT` predicted. There is no read-then-write window for a second writer to slip through, and an existing row is never overwritten — re-ingesting cannot silently rewrite a record an extraction run is already keyed against. **That property has a price, and it is not free:** `DO NOTHING` is also what makes a stored row's identity *uncorrectable*, which is precisely why the cross-run preprint case below cannot be fixed without giving the guarantee up. Both cannot be had at once, and the never-overwrite side was chosen.
- **Python only chooses between candidates.** `ingest/dedup.py` decides *which* of several records for one paper survives a single run — PubMed before bioRxiv, because the journal record carries the peer-reviewed wording that gold `source_quote` values are transcribed from (see the v0.2.0 labeling guideline). A unique constraint can reject a duplicate but cannot express a preference.

There are **two** constraints, not one, and the second is not redundant. `UniqueConstraint(source, source_id)` catches the case the DOI key cannot: a PubMed record first ingested without a DOI lands under `pubmed:<pmid>`, and once the publisher registers the DOI the same paper would compute `doi:<doi>` and insert a second time. With both in place the second insert conflicts and is skipped. A `CHECK (source IN ('pubmed','biorxiv'))` keeps the column honest against the schema's closed `paper.source` enum.

Dialect support is checked at engine construction, before the DBAPI is imported: a database without `ON CONFLICT DO NOTHING` cannot give the idempotence guarantee, so its URL is refused with a message rather than accepted and quietly degraded.

### 4. Retry policy: 5 attempts, 1/2/4/8s, no jitter

`ingest/http.py` retries 429, 500, 502, 503, 504 and `httpx.TransportError`. Five attempts with a ×2 schedule from 1s, capped at 32s per wait — about 15s of total delay, long enough to ride out NCBI's per-second throttle and short enough that a genuinely dead upstream fails the command promptly. Non-retryable 4xx raise immediately with a body excerpt.

**No jitter, on purpose.** Jitter desynchronises a herd of concurrent clients; this is one sequential CLI process, so there is no herd, and determinism makes the exact delay schedule assertable in tests. `sleep` is a parameter for the same reason — the suite asserts `[1.0, 2.0, 4.0]` without spending eleven seconds.

`Retry-After` is honoured when it is a plain number of seconds, but only up to 60s; a server asking for an hour raises instead of holding the process open, and an HTTP-date form falls back to the exponential schedule rather than pulling in a date parser.

The PubMed client additionally paces itself to 0.34s (or 0.11s with a key) **before every E-utilities request after the first**, esearch included, so it does not manufacture the 429s it would then have to retry. "Before every request" is load-bearing and was got wrong first time round: pacing only *between efetch batches* left the documented invocation (`--limit 100` — one esearch, one efetch, one batch) completely unthrottled, which made the tier constants dead code and this paragraph false for every run the CLI realistically makes. Three tests now pin it: the one-batch run, the keyed interval, and one gap per request across multiple batches.

### 5. bioRxiv has no keyword search — this is the awkward one

`api.biorxiv.org/details/biorxiv/<from>/<to>/<cursor>` returns every preprint posted in a date interval, 100 at a time, and offers no query parameter at all. `fetch_abstracts(query, limit)` keeps PubMed's contract by scanning a date window and matching whole words against title and abstract locally. Three consequences, each made visible rather than hidden:

- The search scope is a **date window** (`--biorxiv-window-days`, default 30 ≈ 3000 preprints ≈ 30 requests), not relevance. Widening it costs one request per extra 100 preprints.
- **PubMed's query language cannot be honoured.** Treating `AND` as a required keyword would return zero results for a query that looks perfectly valid, so a query containing boolean operators, field tags or quoted phrases raises rather than being mis-run. `--query` goes to PubMed verbatim; bioRxiv rejects what it cannot mean.
- Hitting the 200-page cap before the window is exhausted raises `ScanLimitError`. A partial scan returned silently would be indistinguishable from a thorough scan that found little.

Matching is whole-word, not substring, so `rat` cannot match `strategy`. Multiple versions of one preprint collapse to the highest version number.

### 6. Loud failures, and the two places that deliberately are not failures

Malformed payloads raise `ResponseFormatError` carrying a windowed excerpt of what arrived — non-JSON from esearch, a missing `idlist`, an esearch `ERROR` field, unparseable efetch XML, an efetch batch whose PMIDs are not the PMIDs requested, an article with no PMID or an empty title, a bioRxiv entry with no DOI or a `published` field that is not a DOI. The excerpt is capped so an error can never dump a multi-megabyte body. This matches the Phase 2 requirement carried forward above: never report only the first problem, and never guess at a cause.

Four things are explicitly *not* errors, and all four are stated on the terminal rather than swallowed:

- **A record with no abstract is stored, not dropped.** PubMed has plenty of legitimately abstract-less entries; discarding them would make the ingest count disagree with PubMed's own. The CLI prints how many arrived that way, because Phase 2 cannot extract from them.
- **A Bookshelf record is skipped, counted, and announced.** `<PubmedBookArticle>` is Bookshelf-indexed material (StatPearls, GeneReviews), whose PMIDs esearch returns like any other. Book chapters report no primary experiment, so they are skipped — but the count goes to stderr, and a batch of *nothing but* book records returns empty rather than aborting the run, because that is a correct upstream answer and not a malformed payload.
- **A deleted citation is skipped, named, and announced.** The `PubmedArticleSet` DTD is `((PubmedArticle | PubmedBookArticle)*, DeleteCitation?)`: a PMID deleted or merged between esearch building its index and efetch answering comes back as `<DeleteCitation><PMID>…</PMID></DeleteCitation>` and is neither record element. That is the same class of upstream answer as a book chapter, so it is treated the same way — the PMIDs go to stderr by name, and the rest of the batch is kept. Aborting on it would throw away up to 199 good papers over an event PubMed handles routinely.
- **The complement is enforced by comparing PMID *sets*, not counts.** The PMIDs the response accounts for (articles + books + deleted) must equal the PMIDs requested, so nothing can go missing between esearch and efetch without the run saying which PMID. A count check was tried first and was wrong twice over: it treated a legitimate `DeleteCitation` as a fatal short batch — the exact mistake the book-only case above already documents — and it could not see a response carrying the right number of records for the *wrong* PMIDs. The set comparison also names the offending PMIDs instead of guessing at a cause.
- **`NCBI_API_KEY` is optional.** This is a considered exception to the "a missing API key raises" rule, not an oversight: NCBI's anonymous tier is a documented, supported mode of the API (3 req/s instead of 10), not a degraded stand-in for a credential. The justification depends entirely on the client actually pacing itself to whichever tier applies, which is why §4 above now says "before every request" and why that is tested rather than asserted in prose. `DATABASE_URL`, which has no such anonymous mode, does raise.

### 7. `--limit` is per source

`--limit 100` requests up to 100 records **from each source**, so a two-source run fetches up to 200 before dedup and satisfies "100+ raw papers from one command" without either client having to know about the other. Said in `--help` because the alternative reading is at least as natural.

### 8. Configuration and environment

`DATABASE_URL` is required and raises when unset or blank — a default would write a hundred papers into a throwaway SQLite file and look like success. `requirements.txt` pins psycopg 3, so the URL must name the driver (`postgresql+psycopg://`); the bare `postgresql://` scheme resolves to psycopg2 and fails deep inside SQLAlchemy with "No module named 'psycopg2'", which reads like a broken install rather than a URL missing one word. That case is caught at engine construction with the corrected form in the message. The CLI prints the URL with `hide_password=True`, and a test asserts the password never reaches stdout or stderr. That test drives `run_ingest` with both sources empty rather than letting a run die on connect: `create_engine` is lazy and `store_papers` returns before opening a session, so the assertion runs against the line that actually prints the URL, with no socket and no PostgreSQL server involved. Dying on connect would have asserted the property against a path that never reaches the print at all — and would have passed just as happily with `hide_password=False`.

Tests use in-memory SQLite through the same `make_engine`, which applies `StaticPool` for `sqlite://` — without it every connection gets its own empty database and the schema created at startup is gone by insert time. No test touches the network: `httpx.MockTransport` stubs at the transport layer, so request building, retry and parsing all run for real and only the socket is replaced. That is why `httpx` was chosen over `requests`.

### Known limitations — accepted for Phase 1

- **A preprint ingested *before* it is published stays a second row forever.** Dedup collapses a preprint and its publication only when both arrive in the *same* run. Across runs it does not, and the failure is worth spelling out because §2 above reads as though the substitution settles the whole case. Day one the preprint is unpublished, so `published` is `"NA"` and the row is keyed `doi:10.1101/...`. Once bioRxiv starts reporting the journal DOI, the client rebuilds the record under `doi:10.1038/...` — and **that rebuilt record is discarded by one of two different mechanisms, depending on the shape of the later run**:

  - *bioRxiv answers alone* (no PubMed hit that run — an off-cycle run, a narrower query, a PubMed outage): the rebuilt record reaches `store_papers` and collides with the day-one row on `(source, source_id)`, so `ON CONFLICT DO NOTHING` drops it in the database.
  - *both sources answer in the same run*: `ingest/dedup.py` collapses the pair on the shared journal DOI in Python and PubMed wins on source priority, so the bioRxiv record never reaches `store_papers` at all and the `(source, source_id)` conflict never fires.

  Either way the PubMed twin inserts under the journal DOI and the result is the same: two rows for one work, the bioRxiv one still carrying a DOI that is no longer the work's canonical DOI, which Phase 2 would read as `paper.doi` in good faith. Both paths are listed because a fix has to address each — the Python path throws the rebuilt record away *before* any storage layer could reconcile it.

  **Not fixed, and the reason is a real trade, not effort.** Correcting it means mutating a stored row's `doi` *and* its `dedup_key` — the primary key — and then merging the result against a PubMed row that already occupies the target key. That is a reconciliation pass with a row-merge policy, and it directly contradicts the never-overwrite guarantee in §3 that idempotent re-ingest is built on. Buying it would cost the property that makes re-running the command safe. Phase 1's Definition of Done is "100+ raw papers from one command; re-running does not duplicate", and re-running does not duplicate; this is a *different* run with genuinely different upstream data. Deferred to whenever a phase actually needs canonical cross-run identity — Phase 3's DrugAge cross-validation is the first plausible customer.

  One test per path, each driven so that only its own mechanism can fire. `tests/test_cli.py::test_a_preprint_ingested_before_publication_leaves_two_rows` runs bioRxiv alone on day two and asserts the run reports `0 new, 1 already present` — proof the record was offered to the database and refused there. `::test_a_later_combined_run_drops_the_published_preprint_in_python` runs both sources and asserts `2 fetched -> 1 unique` followed by `1 new, 0 already present` — proof nothing was offered that the database had to refuse. A reconciliation path added inside `store_papers` breaks the first; one added in `ingest/dedup.py` breaks the second. Verified by removing `UniqueConstraint(source, source_id)` and watching only the first test fail.

- **No migrations.** `init_db` is `SQLModel.metadata.create_all`, which creates tables but never alters them. Any change to `raw_paper` after this point needs a real migration; rerunning `init_db` will not apply it and will not complain.
- **Ingest provenance is not recorded per row.** `fetched_at` is stored, but the query that produced a row is not, so "which papers came from which run" is not answerable. Deferred rather than guessed at: it is not needed until evals want to describe a corpus.
- **PubMed abstracts only.** PMC open-access full text is Phase 2's problem; per the v0.1.0 note, it is `source: "pubmed"` with `extracted_from: "full_text"`, not a third source, so nothing here needs to change to accommodate it.

### Tooling drift found while doing this

- **`ruff check .` no longer runs with the small default rule set.** The v0.1.0 tooling note above records that ruff runs clean with no config file. The venv now has ruff 0.16.2, whose default is ~415 rules (verified with `ruff check --isolated --show-settings`: the isolated and configured rule counts are identical, so this is the installed ruff's default, not a stray config file). CI installs `ruff` unpinned and will see the same. Nothing here needs a config file — `ruff check ingest tests` is clean under the expanded set — but the earlier note is now stale, and the wider rule set is worth pinning if a future ruff release moves the goalposts again mid-phase.

---

## 2026-08-11 — schema v0.3.0: rhesus macaque in scope

**Change:** `organism` gains `M. mulatta`, so the enum is now `C. elegans|M. musculus|M. mulatta|other`. Scope decision made by the human, not derived from the data. Member name follows the existing abbreviated-binomial convention; placed before `other`, which stays last as the catch-all. Additive and backward compatible — a v0.1.0/v0.2.0/v0.2.1 record still validates, and all five files in `data/gold/` pass unchanged against v0.3.0.

**Supersedes v0.1.0 decision 6**, which described the macaque papers as deliberately out of scope and labelled `other`. That rationale for `other` is gone; `other` survives on its own merits, as the truthful bucket for any organism outside the three the MVP filters cover.

### Why this matters for Phase 2 — the classifier must accept macaque papers

The CR-in-macaques pair (Colman 2009 / Mattison 2012) is the strongest conflicting-evidence case the gold set is designed around: two long-running caloric-restriction studies in *Macaca mulatta* reaching opposite conclusions on survival. Nothing else in the PLAN.md Phase 0 design puts two papers, same intervention, same organism, on opposite sides of `lifespan_effect.direction` — the rapamycin pair (Harrison 2009 / Miller 2011) is a *consistency* pair, which tests agreement rather than disagreement. It is also the motivating case for the Phase 5 stretch goal, contradiction detection, which groups by (intervention, organism) and flags opposite directions.

**Consequence for `extract/classify.py`:** the cheap-model gate must return true for macaque papers. A classifier prompt that names only worms and mice — the wording the v0.2.1 scope line invited — would drop both papers before extraction ever sees them, and the failure would be silent: the eval would simply have no record to score, not a wrong one. The conflicting-evidence case would then be absent from the metrics that exist to measure exactly this kind of hard case. Phase 2's classifier prompt and its labelled positive/negative set both need a macaque positive.

### State on disk, so this is not read as more than it is

Neither Colman 2009 nor Mattison 2012 is in `data/gold/` yet. The gold set currently holds five files — `harrison2009`, `kenyon1993`, `lakowski1998`, `martinmontalvo2013`, `strong2016` — against the ten PLAN.md Phase 0 calls for; the macaque pair is part of that design, not of the labelled corpus. So no record uses `M. mulatta` today, and nothing was relabelled: the enum was widened ahead of the papers that need it, which is the right order given `data/gold/` is human-only. Whoever labels the pair writes `M. mulatta` directly and never passes through `other`.

`schema_version` `examples` in the schema still reads `["0.2.1"]`, deliberately: it illustrates the record-level pattern, and `0.2.1` is what every gold file on disk actually declares. Per the "two versions, deliberately" note under v0.2.1, the root `x-schema-version` is the single source of truth for the schema's own version, and that is what was bumped.

---

## 2026-08-11 — schema limitation found while labeling Miller 2011

**`lifespan_effect` carries one `direction` for the whole claim.** There is no per-statistic direction, so a paper that reports a *numeric* change in one statistic and a *qualitative* change in another cannot express the second one as a row of its own.

Miller 2011 is the case that surfaced it. Its abstract gives median survival as a number — "extended by an average of 10% in males and 18% in females" — and asserts a maximum-lifespan increase with no number attached: "produced significant increases in life span, including maximum life span, at each of three test sites." The median claim fits: `median_change_pct` 10 / 18, `direction: increase`. The max claim has nowhere to go. `max_change_pct` is `claim_number_nullable`, so it cannot hold `"not_reported"` — number or null, nothing else — and `direction` is already spent on the record as a whole.

**Workaround in the draft, so the claim is not simply lost:** `max_change_pct` is `null` with a `null` `source_quote`, and `lifespan_effect.direction` takes as its `source_quote` the sentence containing "including maximum life span". The increase in maximum lifespan is therefore recorded only in the provenance of a different field.

**What that costs.** `max_change_pct: null` now means two different things and nothing distinguishes them: "the paper says nothing about maximum lifespan" and "the paper says maximum lifespan increased but gives no number". Phase 3 per-field eval scores `max_change_pct` on the value alone, so both read as an unremarkable null and a model that correctly extracts the qualitative max claim gets no credit for it — the same blindness the median/mean/max split under v0.2.0 was created to avoid, one level down. It is also a `not_reported`-honesty problem in the direction PLAN.md Phase 3 already flags: absence and unquantified-presence are not the same answer.

**Candidate for v0.4.0.** Not designed here, and deliberately not fixed unilaterally mid-labeling — it changes the shape of every `lifespan_effect`. The two obvious shapes are a per-statistic direction (each `_change_pct` gains a sibling direction, verbose but symmetric) or a nullable qualitative member on the numeric wrappers themselves. Both are additive and neither invalidates an existing record. Whichever is chosen, the Miller 2011 draft is the migration test case.

---

## 2026-08-11 — the `data/gold/` write boundary moved, and what still holds

**What changed.** `scripts/check_gold.py --promote` is now the one piece of code permitted to write into `data/gold/`. Until today the rule in CLAUDE.md and PLAN.md was absolute — "NEVER write to `data/gold/`", "never modified by automation" — and it is no longer true, so both were amended rather than left to rot into folklore that the tooling openly contradicts.

**Why it moved.** Promotion was already happening; it was just happening by hand. A labeller finishing a draft had to strip `_abstract` and `_journal`, copy the file across, and delete the draft, in three steps, from memory, after the checks passed. That is exactly the kind of manual transcription step that the verbatim-quote checker exists to distrust everywhere else in this pipeline. It also had a failure mode with teeth: the strip is what the *checker* did in memory, not what the labeller did on disk, so a file could pass `check_gold.py` and then be rejected by the pre-commit hook — see the parity note below.

**What still holds, and is now stated in both files:**

- `data/gold/` is *human-controlled*, not merely *not-automated*. An agent never writes there — no editor tool, no shell command, and no invoking a script that would. Reading is unrestricted and always was.
- `--promote` is a **human-invoked** command. An agent handing back the exact command for the human to run is the intended flow; an agent running it is not, and the PreToolUse hook blocks it explicitly rather than relying on the rule being read.
- It refuses on any failing check, on a quote that was *unverified* rather than verified (PubMed unreachable, `--no-quotes`), on a target that already exists, and on a cross-file failure anywhere in the run. Refusing on an unverified quote is the load-bearing one: exiting zero means "nothing is known to be wrong", promoting means "this is the answer key now", and only one of those claims needs the abstract to have actually been read.
- It never overwrites. A draft beside a finished gold file is a mistake to report, not a merge to attempt.

### The parity bug that prompted it

`scaffold_gold.py` parks `_abstract` and `_journal` at the top level so a labeller can read the paper beside the fields they are filling in. `check_gold.py` stripped those keys before validating; `validate_gold.py` — which the pre-commit hook runs — does not. So a scaffolded file sitting in `data/gold/` passed `check_gold.py` cleanly and was then refused by the hook on `additionalProperties`, with an error naming a key the labeller had been told was fine. Two tools, one schema, opposite answers about the same file.

Fixed at the source rather than by teaching `validate_gold.py` to strip too, which would have propagated the leniency into the pre-commit hook and made the scaffolding keys committable. `check_gold.py` now keys off location: under `data/gold/` the keys are a `FAIL` **and the document is validated unstripped**, so the schema errors it prints are character-for-character the ones the hook will print. Under `data/drafts/` — or any other path — the old strip-then-validate behaviour stands, with the strip stated as an `INFO` line rather than done silently. A test asserts the two tools agree on the same file rather than asserting a hardcoded message, so the parity cannot drift back apart.

### Hook precision

The PreToolUse hook (`.claude/hooks/protect_paths.py`) blocked any Bash command that *mentioned* a protected path and contained a write-ish token anywhere in the string. `head -c 300 data/gold/miller2011.json 2>/dev/null` was refused: the `>` it matched belonged to `2>/dev/null`. Rewritten to ask whether a protected path is the **target** of a write — redirection targets, destructive commands, in-place edits — rather than whether a write character appears somewhere in the line. Reads pass. Anything it cannot parse well enough to be sure about is still blocked, and an agent-invoked `--promote` is blocked outright, matching the amended rule above.

---

## 2026-08-11 — schema limitation found while labeling Colman 2009

**A survival proportion at a reported time point has nowhere to go.** `lifespan_effect` holds three percentage-change fields (`median_change_pct`, `mean_change_pct`, `max_change_pct`) and a `p_value`. All four describe a *change in a lifespan statistic*. A paper whose only survival datum is a *proportion alive at a stated time point* fits none of them, and there is no fifth field for it.

Colman 2009 is the case. Its abstract gives no lifespan statistic at all: "At the time point reported, 50% of control fed animals survived as compared with 80% of the CR animals." That is a survival proportion at a censoring point, reported in the context of aging-related deaths — not median, not mean, not maximum, and not a change in any of them.

**Consequence for the record as written.** `lifespan_effect.direction` is `increase`, backed by the sentence about CR lowering the incidence of aging-related deaths, and every numeric field under it is `null`. The record therefore carries a direction with no numeric support whatsoever. Nothing in the schema marks that as different from a paper that reported a direction and simply omitted its numbers. The 80/50 figures survive only in `notes`, which is never scored in evals.

**Forcing 80/50 into `median_change_pct` was considered and rejected.** Some arithmetic on the two proportions would produce a number, and the number would look like an effect size. It would be wrong twice over: the quantity is not a median and not a change, and the derivation is the labeller's, not the paper's. The decisive argument is what it would do to Phase 3 — a gold record carrying a fabricated median would penalise a model that correctly extracts `null`, converting the eval from a measure of extraction accuracy into a measure of willingness to invent. The whole point of keeping median/mean/max structurally distinct since v0.2.0 is that a mislabeled statistic is worse than a missing one.

**Candidate for v0.4.0**, alongside the max-lifespan direction limitation from the Miller 2011 entry above. The two are the same shape of gap seen from different sides: a qualitative claim with no number (Miller), and a quantitative datum that is not one of the three named statistics (Colman). Whatever shape v0.4.0 takes — per-statistic direction, a qualitative member on the numeric wrappers, or a separate survival-proportion field carrying its time point — both records are the migration test cases. Not designed here and not fixed unilaterally mid-labeling.

### Confidence, and why one wrapper differs

Every wrapper in this record is `high` except `lifespan_effect.direction`, which is `medium`. The distinction is between reading and reasoning. Organism, agent, intervention type, and every `not_reported`/null are read verbatim off the abstract — the text either says rhesus macaques or it does not. `direction: increase` is not read off anything: the abstract states no lifespan statistic, so the value is an inference from a claim about aging-related mortality to a claim about lifespan direction. That is a defensible inference and still the right label, but it is a different epistemic act from transcription, and the confidence field is where that difference is supposed to show up.

**The existing gold files barely exercise this field, and are worth a review pass.** Measured across all six: `harrison2009` 28/28 `high`, `kenyon1993` 14/14, `martinmontalvo2013` 28/28, `miller2011` 56/56 — four files with no variation at all. `lakowski1998` is 13 `high` / 1 `medium` and `strong2016` is 118 `high` / 8 `medium`. So the field is not literally constant, but 9 non-`high` values out of 266 is close enough that the concern stands: a confidence field that almost never varies is not measuring anything, and Phase 3 scores it by exact category match, so a gold set that is ~97% `high` makes "always answer high" a near-perfect strategy. Either the labelling really was that uniform — plausible for abstracts that state their numbers outright — or `high` has been the default keystroke rather than a judgement. Worth re-reading the six files with the reading-versus-reasoning line above in hand before Phase 3 treats the field as a signal.

---

## 2026-08-11 — labeling Mattison 2012: the splitting rule, the conflicting pair, and an eval question

### (a) When a paper becomes more than one record

Stated explicitly because it has been applied consistently for seven files without ever being written down, and the Mattison abstract is the first case where the tempting answer is the wrong one.

**Split into separate records only when the source reports separate results.** The unit is a reported result, not a cohort, an arm, or a demographic label. If the paper resolves a number or a direction for each group, each group earns a record; if one statement covers several groups, they share one.

Applied to the set as it stands:

- **Split.** `harrison2009` reports rapamycin per sex — mean 9% / max 9% in males, 13% / 14% in females — so it is two records. `miller2011` likewise, median 10% male and 18% female. `strong2016` splits acarbose the same way. `martinmontalvo2013` splits metformin by dose, two doses with two results.
- **Not split.** `mattison2012` states one survival result for the young and the older monkeys together, so it is one record and `age_at_start` spans both cohorts. `miller2011`'s resveratrol and simvastatin arms each carry a single statement covering males and females, recorded once with `sex: mixed`. Several `strong2016` records are `sex: mixed` for the same reason.

The failure mode this prevents is inventing rows. Splitting Mattison into a young record and an older record would produce two records whose `lifespan_effect` is a copy of one sentence, doubling the paper's weight in any aggregate and in the Phase 3 eval denominator while adding no information. That is the same objection as the v0.2.0 guideline against giving unreported cohorts their own records, seen from the other direction: there, a record with no result; here, two records sharing one.

Note the rule keys on the *reported result*, not on `sex` or dose being knowable. `sex: mixed` and a cohort-spanning `age_at_start` are the honest encodings of a statement that genuinely did not separate them.

### (b) Colman 2009 / Mattison 2012 is the strongest conflicting-evidence case in the set

Same intervention (`caloric restriction`), same organism (`M. mulatta`), opposite `lifespan_effect.direction` — `increase` against `no_effect`. Nothing else in the gold set puts two papers on opposite sides of the direction field. The rapamycin pair (`harrison2009` / `miller2011`) is a *consistency* pair: it tests that the same finding is extracted the same way twice, which is a different property.

What makes this pair unusually strong is that the disagreement is not an artefact of two labellers reading two papers — **Mattison's own abstract names the contrast**, citing the WNPRC result and setting its own against it. The conflict is asserted by the source, so a Phase 5 contradiction detector grouping by (intervention, organism) and flagging opposite directions has a case where the ground truth is not a judgement call. The v0.3.0 entry above anticipated this pair as the reason `M. mulatta` entered the enum; both records now exist and the case is real rather than planned.

Worth stating for whoever reads the two files side by side: the paired records are *deliberately* asymmetric in provenance. Colman's `direction` is `medium` confidence, inferred from an aging-related-mortality claim; Mattison's is `high`, read off a sentence that states the survival result outright. Neither record carries a single lifespan number. The pair disagrees on direction with no effect sizes on either side.

### (c) Open question for Phase 3 — free-text fields cannot be scored by exact string match

`age_at_start`, `strain`, `dose` and `mechanism` are open vocabulary. `mattison2012.age_at_start` is `"young and older age"`, sliced verbatim from the abstract. A model that returns `"young and old"`, `"young and older"`, or `"young and older age rhesus monkeys"` has extracted the same fact and would score zero under exact match. `strain` has the same problem in the other direction, where `"genetically heterogeneous"` and `"UM-HET3"` denote the same animals in the ITP papers.

This is not the `confidence` problem from the Colman entry, where the enum is right and the labels barely vary. Here the field is genuinely unbounded and exact match is the wrong metric — it would report extraction failures that are not failures, and the resulting number would be uninterpretable rather than merely harsh.

**Not decided here.** It is an eval-design decision, due when Phase 3 defines per-field scoring, and it needs a rule per field rather than one global tolerance: normalized-substring containment might suit `age_at_start`, a synonym table is closer to what `strain` needs, and `mechanism` may want neither. Recording it now so the question is on the table before the metric is written, rather than discovered when the first eval reports a suspiciously low free-text score. The v0.1.0 note that closed enums are compared by exact match still stands and is unaffected.

---

## 2026-08-11 — schema limitation found while verifying strong2016 quotes: multi-source provenance

### The gap

**A value derived from two or more places in the source has no provenance model.** `source_quote` is a single string, and full-text verification (added this phase) enforces what was previously only a convention: it must be one *contiguous* slice of the source. A value that is read off one sentence, or one table row, is expressible. A value that is the sum of two table rows is not — there is no second quote to point at, and no way to say "these two places, together".

Found by the full-text check rather than by reading. Three `strong2016` records — `metformin-rapamycin`, `metformin`, `udca` — carry a `sex: mixed` `sample_size` that is the sum of a male arm and a female arm:

| record | value | male + female |
|---|---|---|
| `strong2016-mmusculus-metformin-rapamycin` | 300 | 158 + 142 |
| `strong2016-mmusculus-metformin` | 288 | 148 + 140 |
| `strong2016-mmusculus-udca` | 282 | 149 + 133 |

Each quote had been written by concatenating the two table rows with a semicolon and appending a parenthetical naming the table. None of those strings is in the paper. The check reports them at 39–53% similarity to the nearest real slice, which is the correct answer: they are not mistranscriptions, they are constructions.

**The paper states no combined total.** Searched for it before deciding: no "a total of N mice", no enrolment or assignment sentence carrying a cohort size, and the literals 288 and 282 appear nowhere in the full text. 300 appears once, in the rotarod methods (`accelerating to a maximum 40 rpm within 300 s`). The nearest prose n is `the ITP protocol used 148 Met mice and 294 controls, distributed among the three test sites` — a single-sex number in the discussion of the Martin-Montalvo disagreement, not a total. The sums are the labeller's arithmetic, correct arithmetic, and unattributable.

**A contiguous slice covering both arms exists and is worse than nothing.** For each of the three, the span from the male row to the female row is 259–266 characters and sweeps in every intervening agent's row — Control, 17aE2, Prot, and for metformin also Met/Rapa and UDCA. It is verbatim, it is unique, and it would pass the checker while pointing at six other drugs. A quote that verifies without evidencing anything is a worse failure than a quote that fails, because nothing downstream will ever flag it.

### Interim rule

**`sample_size` carries the n as stated for the record's scope.** Where the source states a total for that scope, quote it. Where only per-arm n's are stated and the record is `sex: mixed`, the field is **null** and the arms go in `notes`.

This is the treatment Colman 2009's 80/50 survival proportions received, and for the same reason: a value the paper does not state, reachable only by the labeller's arithmetic, does not belong in a field that Phase 3 will score. There the argument was that a fabricated median penalises a model correctly extracting `null`, converting the eval from a measure of accuracy into a measure of willingness to invent. A summed `sample_size` is the milder version of the same error — the arithmetic is unarguable where Colman's was not — but the eval consequence is identical, and the provenance is no better.

Note what the rule does *not* do: it does not split the records. Splitting `metformin` per sex would fix the provenance and destroy the record — its `notes` designate it the flagship contradiction case against Martin-Montalvo 2013, and `metformin-rapamycin`'s notes state that one mixed record was chosen deliberately because median +23% was identical in both sexes. The splitting rule from the Mattison entry keys on whether the source *reports separate results*; for these three the survival result is stated jointly. Provenance of one field is not a reason to re-cut a record.

Applied to the three records: `sample_size.value` and `sample_size.source_quote` both to null, per-arm n's and their sum into `notes`, everything else untouched. One side effect worth recording: the `udca` quote had also dropped a minus sign, reading `UDCA 133 865 1 0.762` where PMC's table reads `−1` for the female median change. **That never reached a value** — `median_change_pct` and every other numeric in the record are null and `direction` is `no_effect`, which the table supports for both sexes. It was damage confined to a string that was carrying five columns the field never used, and it disappears with the string.

### v0.4.0 candidates now stand at three

1. **Per-statistic direction** — Miller 2011: a qualitative claim about one statistic when another carries a number, with one `direction` for the whole `lifespan_effect`.
2. **Survival-at-timepoint** — Colman 2009: a quantitative datum that is not median, mean, or max.
3. **Multi-source provenance** — this entry: a value attributable to two or more disjoint places in the source, with one contiguous `source_quote`.

The first two are the same gap seen from two sides — a qualitative claim with no number, and a number with no field. The third is a different axis: not which fields exist, but how many places one field is allowed to cite. It is the only one of the three that changes the claim wrapper itself rather than the `lifespan_effect` shape, and so the only one that touches every field in the schema. Whichever shape v0.4.0 takes, all three records are the migration test cases. Not designed here, and not fixed unilaterally mid-labeling.

---

## 2026-08-11 — `--refresh-quotes` could have written a mid-word slice into `data/gold/`

**The defect.** `best_slice` finds the passage of PMC text closest to a quote by aligning the two with `SequenceMatcher` and then walking outward from the first and last shared runs by however many characters of the quote sit outside them. That walk counts characters and knows nothing about words. Given a quote whose *leading* characters were themselves mistranscribed — `chi2 = 5.46 …` against the paper's `χ2 = 5.46 …` — the alignment finds no shared run until several characters in, and the walk-back overshoots into the middle of the preceding word. The candidate it returned for Martin-Montalvo 2013 began:

```
nsion of mean lifespan (Fig. 1a), χ2 = 5.46 and p= 0.02 in Gehan-Breslow survival test
```

Verbatim, unique in the source, and gibberish — the front of `extension` had been severed.

**Why it never fired.** `--refresh-quotes` only substitutes a candidate scoring at or above `REFRESH_SIMILARITY` (0.90). Both affected quotes are transliterations of a Greek letter plus a spacing difference in a short string, which scores 71% and 74%, so `plan_rewrite` refused them on the threshold and the mid-word candidate was never a write. Nothing in `data/gold/` was ever damaged. That is luck, not design: the threshold was chosen to separate mistranscriptions from reconstructions, and it caught this by coincidence of the same quotes being short. A longer sentence with the same leading-character problem would have cleared 0.90 and been written.

**The failure mode being prevented** is the one that has no second chance. `--refresh-quotes --write` edits ground truth in place, and it writes a string that by construction verifies — the whole point is that the candidate is a real slice of the paper. A gibberish quote committed that way passes `check_gold.py` on that run and on every run afterwards. Nothing downstream distinguishes `of mean lifespan …` from `nsion of mean lifespan …`; both are contiguous and both are in the source. It would have to be caught by a human reading the diff, which is exactly the review this tool exists to make unnecessary.

**The fix, in two independent places.** `best_slice` now snaps both boundaries to whitespace edges, scoring the contracted and expanded form of each and keeping the best-matching combination (ties to the shorter slice). Separately, `plan_rewrite` refuses any candidate that begins or ends adjacent to a word character, and `apply_rewrites` re-checks the same condition against the source and **raises** rather than skipping. The second and third checks are unreachable while the first is correct, and that is the point: this is the only code path in the repo that edits `data/gold/`, and one guard on it is one more than zero but fewer than it deserves.

Recorded because the general lesson outlives this bug: a string-similarity search is a *search*, and its output is a candidate, not a quote. Anything that promotes a search result into ground truth needs a check that the result is well-formed on its own terms, not merely that it scored well.

---

## 2026-08-11 — elink returned a fault and the checker read it as "not in PMC"

### The bug

`parse_pmcid` walked `LinkSet/LinkSetDb` looking for the `pubmed_pmc` link, and returned `None` if it found none. `None` means "this paper has no PMC article", and `fetch_full_text` caches it. An elink response carrying a server fault has no `LinkSet` at all, so it fell through the same path and was cached as a settled fact about the paper.

Found while checking whether Eisenberg 2009 (PMID 19801973) is in PMC. It is not — NCBI's ID converter says `Identifier not found in PMC`, and that is the right answer. But the answer arrived through the broken path, and testing the same call on PMID 27312235, which is certainly `PMC5013015`, returned "no PMC record" too. elink was serving this to every query:

```
<eLinkResult><ERROR>NCBI C++ Exception: ... Read failed: EOF (the other side
has unexpectedly closed connection), peer: 130.14.18.86:8064</ERROR></eLinkResult>
```

Three consecutive attempts, every PMID. A service outage, presented by our code as a property of the literature.

### Blast radius

The gold set read clean throughout, because `--all` uses the cache and the cache was warm from before the outage. The exposure was `--refresh`, and it was total: re-resolving during the outage would have written `pmcid: null, text: null` for all four PMC papers and cached it, flipping **all 128 full-text quotes to `unverifiable`** — `harrison2009` 8, `martinmontalvo2013` 15, `strong2016` 95, `lakowski1998` 7 — with the run still exiting 0, because unverifiable is a warning by design. Simulated against a throwaway cache directory to confirm rather than assume: `strong2016` re-resolved to `pmcid=None`.

The failure would have looked exactly like a correct result. Nothing distinguishes "PMC does not have this paper" from "PMC did not answer" once the answer is on disk, and the next `--refresh` would have been months later.

### The distinction that failed

`PMCFullText` was designed around exactly this: a *fact about the paper* (not in the open-access subset — a warning, unverifiable, blocks promotion) against a *fact about the request* (we could not find out — a skip). `make_full_text_lookup` keeps them apart by protocol, returning `None` for the first and raising for the second, and `check_file` maps them to different statuses. That design is sound and it held. It was simply built on a lower layer that had already collapsed the two, one function down, where `None` was doing double duty.

The lesson is about where a distinction has to be enforced, not whether it is documented. Three layers agreed on the difference between "no" and "don't know"; the one that produced the value did not, and the agreement above it was worth nothing.

### The fix

`parse_pmcid` now raises `PubMedLookupError` on any `<ERROR>` element, and on a response carrying no `LinkSet` at all — refusing to read either as "no PMC record". `parse_full_text` gets the same guard, with the case distinction that matters there: upper-case `<ERROR>` is an E-utilities fault and raises, lower-case `<error>` inside `<pmc-articleset>` is PMC declining to serve the article and is a real answer. Because the raise propagates before `write_fulltext_cache`, a negative that was not positively established is never written — that is now stated in `fetch_full_text`'s contract and pinned by a test asserting the cache file does not exist after a fault.

One cache entry was written by the broken path during the outage — `data/.abstract_cache/19801973.fulltext.json`. Its content is correct, confirmed independently against the ID converter, but it was not positively established and is being deleted so it is re-derived once elink recovers.

---

## 2026-08-11 — schema v0.4.0: `species`, and why `organism` was not extended

Additive, so every existing record still validates: all 8 gold files pass unchanged, and omitting the field entirely is legal.

### The gap this closes

`organism` is a closed enum — `C. elegans | M. musculus | M. mulatta | other` — and `other` exists so an out-of-scope paper can be labelled truthfully rather than forced into a wrong bucket. It does that, and loses the species on the way. Found while scoping Eisenberg 2009, which reports spermidine in yeast, flies, worms and human immune cells. Building its yeast and fly records and diffing them field by field, under v0.3.0:

```
DIFFERS  experiment_id      an id slug, pattern-matched but never checked against organism
same     organism           {"value": "other"} — identical
DIFFERS  strain             "BY4741" / "w1118"
same     sex, sample_size, intervention, mechanism, lifespan_effect, notes
```

Two records, indistinguishable in every field that means anything. `strain` is not a fix: it names a strain *within* a species, it is legitimately `not_reported` in plenty of papers, and `BY4741` identifies the organism only to a reader who already knows. `experiment_id` is not a fix either: the convention puts an organism slug in it, but the pattern is `^[a-z0-9]+(-[a-z0-9]+)+$` and nothing validates the slug against `organism.value` or aggregates on it.

### Why not extend the enum

Tempting and wrong. `organism` is the **filter vocabulary** — `GET /experiments?organism=` and the Phase 3 eval both key on it, and PLAN.md scopes the MVP to three organisms. Adding `S. cerevisiae` and `D. melanogaster` to the enum would put out-of-scope organisms into the API's aggregates and the eval's denominator, changing what the MVP claims to cover in order to fix a labelling problem. `species` keeps the two concerns apart: `organism` stays the coarse bucket the product filters on, `species` carries what the paper actually studied.

Populate `species` whenever `organism` is `other`. Elsewhere it is optional and null — the enum already says it, and duplicating `C. elegans` into a free-text field would create a second spelling to keep consistent.

### Fourth of four gaps, and the first one closed

1. Per-statistic direction (Miller 2011) — **open**
2. Survival-at-timepoint (Colman 2009) — **open**
3. Multi-source provenance (Strong 2016 summed `sample_size`) — **open**
4. Species below the organism enum (Eisenberg 2009) — **closed by this release**

The fourth was separable from the other three, which is why it went first. The first three all change the shape of `lifespan_effect` or of the claim wrapper itself, they interact, and none of them has an obvious design yet; this one adds a field beside `strain` and touches nothing else. Closing it now does not prejudge them, and Eisenberg 2009 can be labelled without waiting.

---

## 2026-08-11 — bioRxiv preprints: scaffolding by DOI, and verifying against bioRxiv

### The schema needed no change

Checked before building anything, because it would have gated the rest. `paper.required` is `doi`, `title`, `year`, `source` — **not `pmid`** — and `pmid` is typed `["string", "null"]` with the description already reading *"null for preprints not yet indexed"*. `source`'s enum has held `"biorxiv"` since v0.1.0. A DOI-only record validates today, with `pmid` null or omitted entirely; both were confirmed against v0.4.0 rather than reasoned about. So this was not a v0.5.0 question. The schema anticipated the case a year of labelling before it arrived, which is the first time that has happened in this project.

### What was built

`scripts/biorxiv_lookup.py` is the bioRxiv twin of `pubmed_lookup.py`: DOI in, `BioRxivRecord` out, cached in `data/.abstract_cache/` under `biorxiv-<doi with slashes replaced>.json`. It is **not** a second client — the HTTP call, retry policy, status vocabulary and JSON shape stay in `ingest/biorxiv.py`, which gained a public `fetch_detail(doi)` beside `fetch_abstracts`. The two are the same API from opposite ends: `fetch_abstracts` scans a date window because bioRxiv has no keyword search, `fetch_detail` resolves a DOI the human already chose.

`scaffold_gold.py` now dispatches on the identifier's shape — digits are a PMID, `10.NNNN/...` is a DOI — rather than on a flag. The two forms are disjoint, so there is nothing to disambiguate, and an identifier that is neither raises at the CLI instead of becoming a confusing "PMID not found" from the wrong client. `build_skeleton` reads `record.source`, `.pmid`, `.journal` and the rest off either record type; `PubMedRecord` gained a `source` property (a property, not a field, so cached entries still load) and `BioRxivRecord` carries the same attribute surface. The PubMed path is unchanged and pinned by a regression test.

`check_gold.py` keys on `paper.source`. A `biorxiv` record's abstract quotes verify against the bioRxiv abstract, fetched by DOI and cached the same way; everything else about the comparison is identical.

### Full-text quotes on a preprint are unverifiable, deliberately

bioRxiv full text exists — every preprint carries a JATS XML link — but it is not in PMC, and PMC is what this checker reads. So a preprint's `full_text` quotes are reported `unverifiable`, the same status a paper outside the PMC open-access subset gets, with the same consequences: a warning on a run, a refusal on `--promote`. The alternative was to add a second full-text fetcher and a second flattener for bioRxiv's JATS. Not now: the preprint slot is one record, the quotes that matter for it are in the abstract, and a second flattener is a second thing to be wrong about whitespace.

A preprint labelled entirely from its abstract emits no PMC warning at all, because nothing is fetched when no claim depends on it. That is the intended shape for this slot — verified end to end: 6/6 abstract quotes, `FULLTEXT` reads `-`, promotable.

### Gcgr 2025 was considered for this slot and rejected

`10.1101/2025.05.13.653849` (Bruner et al., glucagon receptor knockout, median lifespan **decreased** 35% in lean and 54% in diet-induced obese male mice) was the first choice and is not usable as a preprint record: it has since been published as GeroScience `10.1007/s11357-025-01899-w`, **PMID 40993467**, and is in PMC as **PMC12972411**. The whole PubMed path handles it, full text included, so it exercises none of what the preprint slot exists to exercise.

Caught late — the `published` field was in the bioRxiv API payload from the first query and went unread. `scaffold_gold.py` now prints a loud `PUBLISHED` warning naming the journal DOI whenever `fetch_detail` reports one, because once a draft is written the distinction is invisible.

**Worth keeping as a candidate if the gold set is extended.** The set holds 17 `increase`, 6 `no_effect`, 1 `decrease`. Per-field accuracy on `decrease` is uninformative at n=1 — a Phase 3 model that never predicts `decrease` loses almost nothing — and this paper would bring two effect sizes in that direction, in mice, with a quantitative abstract. It would enter as an ordinary `source: pubmed` record via PMID 40993467.

### Live instance of the known preprint/publication dedup gap

This is the case the bioRxiv module's docstring and the "Known limitations" entry describe in the abstract: preprint ingested first, publication appearing later, the two not collapsing across runs. Here the preprint DOI `10.1101/2025.05.13.653849` and the journal DOI `10.1007/s11357-025-01899-w` are the same work, and only the bioRxiv `published` field connects them. `fetch_detail` surfaces that field, so a future dedup pass has something to key on; nothing consumes it yet.

---

## 2026-08-11 — schema limitation found while labeling Calubag 2025: the unqualified percentage

### The gap

**A lifespan percentage stated without naming its statistic has no field.** `lifespan_effect` carries `median_change_pct`, `mean_change_pct` and `max_change_pct`, and every one of them names a statistic. A paper that reports a size without saying which — "extends the lifespan of male, but not female, mice by 23%" — cannot be recorded quantitatively at all, because there is no field that means "23%, statistic unspecified".

The fifth gap, and the first found in a preprint. Abstracts are where it will keep appearing: a journal abstract that reports a lifespan effect usually names the statistic, and a preprint abstract compressing a full paper often does not.

### Interim treatment

All four numeric fields null, the figure in `notes`, stated as an unqualified percentage. Same shape as Colman 2009's 80/50 and Strong 2016's summed `sample_size`, and for the same reason each time: a value the paper does not state in the form the field means does not go in the field.

Forcing 23 into `median_change_pct` was considered and rejected on the argument already made for Colman: the decisive objection is not that the guess is probably wrong, it is what a wrong guess does to Phase 3. A gold record asserting a median the paper never named would penalise a model that correctly declines to guess, and turn the eval from a measure of extraction accuracy into a measure of willingness to invent. Median is the *likely* reading — most mouse lifespan papers lead with median — but likely is not stated, and this project's whole position is that the difference matters.

### v0.5.0 direction

Not a new field per case. The three named `_change_pct` fields already encode the statistic in the field name, which is what makes an unnamed statistic unrepresentable; adding `unqualified_change_pct` beside them repeats the mistake one column wider and leaves the next unnamed quantity homeless again.

The direction worth designing is a **value plus a statistic qualifier** — one change-percentage member carrying its own `statistic` alongside it, with `median`, `mean`, `max` and **`unqualified`** in the qualifier's vocabulary. That subsumes the existing three fields rather than sitting beside them, so it is a breaking change and belongs in a major-ish bump with a migration, not in an additive release. It also interacts with gap 1 (per-statistic direction, Miller 2011): both want the statistic to become a value rather than a field name, and designing either without the other would mean doing the same migration twice.

### Phase 3 consequence — this one needs an eval decision, not just a schema decision

Worth stating separately, because it is the first gap whose cost lands in the eval rather than in the record. **A model that puts 23 in `median_change_pct` for this paper is not clearly wrong.** It has made a defensible reading of an ambiguous abstract, and the gold record says null. Under exact match that is a miss, scored identically to a model that invented a number from nothing — and those are not the same failure.

So `lifespan_effect.median_change_pct` on `calubag2025` is a record where plain exact match reports something misleading in both directions: crediting null-vs-null tells you the model was cautious, not that it was right, and penalising 23-vs-null tells you it was wrong when it was merely decisive. This joins the free-text scoring question from the Mattison entry on the list of things Phase 3 must settle before the metric is written. A plausible answer is that fields null *because of a recorded ambiguity* are scored separately from fields null because the paper is silent — which needs the record to distinguish those two, and it currently cannot.

### The other thing this paper needed: two records, opposite directions

Not a gap, but the splitting rule's cleanest case so far. Val-R extends male lifespan and does not extend female lifespan, both stated in one sentence, so the Mattison rule — split when the source reports separate results — gives two records with `direction: increase` and `direction: no_effect` from a single quote. The female record is a *reported null*, not missing data, which is exactly the distinction `no_effect` exists to carry.

Two of the abstract's claims are deliberately absent from the file: the isoleucine result is the authors' own prior paper ("we recently found..."), and the statement that BCAA restriction extends healthspan and lifespan is background literature. Neither is a finding of this study. They are recorded in `notes` as excluded, and a check in the labelling script asserted that neither sentence appears as a `value` or a `source_quote` anywhere in the record — the failure mode being a background sentence quoted as though the paper had demonstrated it.

---

## 2026-08-11 — the classifier negative set, and why the negatives have to be hard

PLAN.md Phase 3 scores the classifier on precision and recall over a labeled positive/negative set. The 10 gold papers are the positives. `data/classifier_set/negatives.json` is the other half: 15 candidates, three in each of five categories.

### Why hard negatives

**A classifier scored only on obvious negatives reports a precision that means nothing.** Draw a negative set at random from PubMed and it is mostly cardiology, oncology and epidemiology; a classifier keying on nothing but the token "lifespan" separates that set almost perfectly, and the resulting 0.98 is a fact about the sampling, not about the classifier. Precision is only informative when the negatives are drawn from the region where the decision is actually difficult — papers the classifier is at genuine risk of calling positive.

So every entry shares surface features with the positives: the vocabulary, usually the organism, and in several cases a real intervention. Each fails on exactly one thing, and the entry names which. The hardest are the ones where a human has to read twice — a fasting-mimicking diet trial that opens by citing lifespan extension in mice and then measures biological-age markers, or a meta-analysis whose title contains "rapamycin", "metformin", "dietary restriction" and "lifespan extension" and whose every number belongs to someone else.

### What each category stresses

The five are not a taxonomy of papers, they are a set of **boundaries the classifier has to hold**, one per category:

| category | the boundary it probes |
|---|---|
| `aging-no-lifespan` | Does it require a *lifespan outcome*, or is aging-as-a-topic enough? |
| `lifespan-no-intervention` | Does it require an *intervention*, or is a measured lifespan enough? |
| `lifespan-adjacent-outcome` | Does it distinguish the lifespan of an *organism* from the lifespan of a cell? |
| `review-or-meta-analysis` | Does it distinguish *doing* an experiment from *describing* one? |
| `wrong-organism` | Does it read "lifespan" *in context*? |

Three entries per category rather than a flat 15 on purpose. A per-category breakdown is what turns a bad score into a diagnosis: 12/15 tells you the classifier is imperfect, while 3/3 on four categories and 0/3 on `review-or-meta-analysis` tells you it cannot tell a review from an experiment, which is one prompt change away from fixed.

Two shapes were deliberately **not** included. A *Drosophila* lifespan-intervention study is not a negative — the schema's `organism: other` exists so out-of-scope organisms can be labelled truthfully, and Eisenberg 2009 is a gold positive covering flies, yeast and worms. And a C. elegans recombinant-inbred mapping study that then validates its hits by RNAi is genuinely ambiguous: mapping is not an intervention, but the validation is. Both were read and dropped rather than filed under a category they do not fit.

### The human reviews every entry before it counts

Each entry carries `"reviewed": false`. The validator prints the unreviewed count on every run and says those entries do not count toward the eval. The candidates were assembled by searching PubMed through the existing ingest client and reading every abstract — no entry was written from a title — but "an agent read it and thought so" is not the standard for the set that decides whether the pipeline can be trusted. The same rule as `data/gold/`, arrived at for the same reason, and the flag makes the distinction machine-visible instead of relying on someone remembering.

### Structural notes

`scripts/validate_classifier_set.py` checks structure, closed vocabulary and uniqueness, and runs in CI beside `validate_gold.py`. It is **not** JSON Schema: a gold record is a deep versioned tree a model will one day have to produce, while this is a flat list under a closed vocabulary, and a second schema file would need its own version for no gain.

`--resolve` checks every PMID against PubMed and confirms the stored title still matches. It is opt-in and stays out of CI: an identifier valid when added does not stop being valid, and CI that fails because NCBI is having an outage teaches people to ignore CI — the elink outage earlier today being the local precedent.

One known limit, pinned by a test so it is a decision rather than a surprise: duplicate detection keys on whichever identifier an entry uses, so the same paper listed once by PMID and once by DOI would pass. Nothing in the file records the mapping between the two. It matters if preprint negatives are ever added alongside their published versions — the same gap as the ingest dedup limitation, in a smaller place.

---

## 2026-08-11 — eval design for the classifier set: ratio, headline metric, and a shared sample

Decided when the negative set was approved at 15 entries against the gold set's 10 positives. Recorded because each of these is a choice that would otherwise look like an accident of how many candidates a search happened to return.

### The 15:10 ratio stands

Not rebalanced to 1:1, and not padded in either direction. **Precision and recall are properties of a threshold and its errors, not of set composition.** The negatives are what the search found under the criterion "hard enough that a keyword classifier would plausibly call it positive"; adjusting the count afterwards to make the resulting number rounder would be tuning the measuring instrument rather than the thing being measured. If the ratio is wrong for some downstream purpose, the honest fix is to say what the purpose is and compute a different statistic from the same set, not to reshape the set until one statistic behaves.

### Aggregate is the headline; per-category is diagnostics

The README quotes **aggregate precision and recall over all 25 papers**. The per-category breakdown is reported alongside and is explicitly *not* quoted as a metric.

The reason is n=3. A per-category rate moves 33 points when one entry changes, so "67% on reviews" would read as a measurement and carry the resolution of a coin flip. What three-per-category is actually good for is *localisation*: a run that misses all three reviews and nothing else is a different failure from one that misses three scattered entries, and the aggregate cannot tell those apart while the breakdown can. That is why the categories exist and why they are equally sized — one boundary each, enough entries to notice a pattern, not enough to quote a rate.

### Known limitation: the positives and the extraction gold set are the same papers

The 10 papers in `data/gold/` are simultaneously the classifier's positive class and the ground truth for per-field extraction accuracy. So the two headline Phase 3 numbers — classifier precision/recall, and per-field extraction accuracy — are computed over a **shared sample** and are not independent evidence about the pipeline. A gold set that happens to be easy to classify inflates the first while telling you nothing new about the second.

Worse, and the part worth stating plainly: **there is no held-out split, so the classifier prompt will be iterated against the set it is scored on.** Every revision is chosen partly by how it scores here. The reported precision and recall are therefore optimistic by an unknown amount. This is the same exposure the Phase 3 extraction prompt loop already carries, appearing a second time in a second place, and it is not fixable by editing this file — closing it needs papers this project has not labelled, which is a Phase 3 scoping decision.

Recorded in the `Limitations` section of `data/classifier_set/README.md` as well as here, because a limitation that lives only in a design log is one nobody reads at the moment they are quoting the number.

---

## 2026-08-11 — Blind re-label targets selected

Three gold papers were selected at random for a blind re-label on 2026-08-18,
to measure single-labeler self-agreement for the README:
mattison2012, calubag2025, martinmontalvo2013.

Selection method: `ls data/gold/*.json | sort -R | head -3`, run once on
2026-08-11, first result taken. Recorded before the re-label so the targets
cannot be chosen after the fact.

Procedure: re-answer the labeling questions for each paper from the same
source the original label used (per its extracted_from field), without
consulting the existing JSON. Compare field-by-field afterwards. Disagreements
are reported as-is; the original label is not silently corrected to match.

---

## 2026-08-11 — Eval design: like-for-like source matching

At eval time the model receives the same source the human labeler used for
each gold file, determined per-file by that file's extracted_from values:
abstract-only labels are scored against a model given the abstract alone,
full_text labels against the model given PMC full text, preprints against
the bioRxiv text.

Rationale: the eval measures extraction quality, not source-availability
mismatch. A model given full text where the label was made from the
abstract alone would be penalized for correctly extracting fields the
labeler honestly marked not_reported, and the reverse would credit it for
absences it had no chance to fill.

---

## 2026-08-12 — `experiment_id` generation does not reproduce the gold ids (open question for Phase 3)

Found in review of Phase 2. `extract/extract.py::_experiment_id` follows the convention `schema/experiment.schema.json` writes down — `<first-author-year>-<organism-slug>-<agent-slug>[-<n>]` — and **reproduces 11 of the gold set's 26 `experiment_id`s.** The other 15 differ, from two systematic causes:

1. **Disambiguation is semantic by hand, numeric by code.** Where one paper reports the same (organism, agent) pair more than once, the labeller wrote `-male` / `-female` (harrison2009, miller2011, strong2016 acarbose, calubag2025) or `-low-dose` / `-high-dose` (martinmontalvo2013). The generator has only the schema's `-2`, `-3`, and nothing in the payload tells it which axis split the arms. **No gold record uses a numeric disambiguation suffix** — the `-2` in `kenyon1993-celegans-daf-2` and `lakowski1998-celegans-eat-2` is part of the gene name, not a tail distinguishing two arms of one paper.
2. **Agent names are hand-normalised by the labeller.** `nordihydroguaiaretic acid (NDGA)` is `ndga` in gold and `nordihydroguaiaretic-acid-ndga` generated; `ursodeoxycholic acid (UDCA)` likewise; `eat-2 mutation (reference allele ad465)` is `eat-2`; `metformin plus rapamycin` is `metformin-rapamycin` by hand and `metformin-plus-rapamycin` by slug.

The schema calls `experiment_id` a stable identity "for eval alignment" and says Phase 2 generates it "from the same convention". Both halves of that sentence cannot currently be true, and **this is a schema/gold-set inconsistency, not only a code bug** — the code does what the schema documents.

**Not decided here.** Choosing between semantic and numeric disambiguation, and whether agent names get normalised before slugging, changes what Phase 3 can align on and is the human's call. Nothing was changed in the generator.

Pinned instead of drifting: `tests/test_extract.py::KNOWN_ID_DIVERGENCES` lists all 15 divergent pairs, and `test_generated_ids_match_gold_except_where_pinned` parametrizes over the real `data/gold/*.json` and asserts the set exactly. A new divergence fails; so does one that quietly disappears.

### What Phase 3 must do

Aligning on `experiment_id` as it stands would score 15 of 26 gold records as unmatched — a measurement of a naming mismatch, reported as an extraction failure. That half of the problem is real and unchanged.

**The fallback this entry originally recommended — align on `(organism, agent)` instead of `experiment_id` — does not survive the paper's own facts, and is withdrawn.** Cause 1 above says outright that some papers report the same (organism, agent) pair more than once; the recommendation that followed it did not carry that forward. The pair is not unique within a paper, so it cannot key a 1:1 matcher.

Counted against `data/gold/` rather than off the list above, because the list is incomplete: **12 of the 26 records, in 6 within-paper groups, share an (organism, agent) pair with a sibling.** Five groups are the ones cause 1 names — harrison2009, miller2011, martinmontalvo2013, strong2016 (acarbose) and calubag2025. The sixth is eisenberg2009, and it is missing from that list because it is not two arms of one intervention. That paper has three records — worm, yeast and fly, all spermidine — and the worm one is distinct because `C. elegans` is in the MVP enum. The other two are not: `organism` is a closed enum, so both the yeast and the fly record read `other`, and both agents read `spermidine`. `species` is the only field that separates them, and `(organism, agent)` does not read it. A Phase 3 matcher built on the withdrawn sentence would collapse those 12 records into 6. That is the mirror image of the id problem, and the worse half of it: aligning on a divergent id reports a naming mismatch as a missed extraction, which is at least visible in the numbers; collapsing two experiments into one scores them as a match and reports nothing amiss.

**No replacement key is proposed here. The eval alignment key is an open Phase 3 question and is reserved to the human**, alongside the `experiment_id` convention itself. The two are one decision seen twice: choosing what aligns gold to extracted output is choosing what identifies an experiment.

Two things to decide when the convention is settled: whether the split axis (`sex`, `dose`) can be read off the extracted record reliably enough to name an id from it, and whether normalising agent names for the id also means normalising `intervention.agent.value`, which `GET /interventions/{agent}` aggregates on.

---

## 2026-08-12 — Test honesty

**Seven cases in this repo have now been found decorative** — green, plausible, and unable to fail for the reason their name gives. Written down before Phase 3 rather than after it, because the eval harness will be guarded by tests of exactly this kind and its numbers are the ones the README quotes.

The seven are numbered below and the numbering runs 1 to 7 unbroken: 1–3 were found one per round in iterations 3a, 4a and 5b, and 4–7 all in iteration 6a. They are not all the same thing, and the difference matters when reading the list. **Four are tests that exist, name the right function, and still cannot go red for the reason their name gives** — 1, 3, 4 and 5. Two of those four assert the wrong thing (1 pins the defective output as expected; 5 pins the wiring and says nothing about the constant) and two have fine assertions over a fixture that cannot reach the case (3, 4) — which is the second rule below, and why the two groups are not worth separating further. **Three are coverage that is simply absent** — 2, 6 and 7, where a sixty-four-entry table or a whole branch could be deleted with the suite green and no assertion anywhere to notice. From outside the two shapes are indistinguishable: the function is named, the path is walked, and nothing is watching.

This section was first written at three, in iteration 5, and the lead sentence went on saying three after iteration 6a added four more — a count contradicted four paragraphs down by this document's own "Cases 4 to 7" heading. Corrected here rather than quietly re-worded, because that is the failure this section is about, one level up: a number that was true when written and was never re-derived.

**1. `test_non_ascii_agents_still_produce_a_valid_identifier` (found iteration 3a).** It asserted `strong2016-mmusculus-17-estradiol` as the expected value — the *defective* output, with the Greek letter deleted. So it read as coverage of the non-ASCII path while pinning the bug in place. Found when a reviewer asked why the defect had survived three passes and noticed that every `intervention.agent.value` in `data/gold/` is already hand-normalised to ASCII: the gold-parametrized id test never feeds the generator a non-ASCII string at all, so nothing else was watching that path either.

**2. Thirty-nine of the transliteration table's sixty-four entries (found iteration 4a).** Not a wrong assertion — absent coverage. A reviewer deleted all 8 Latin entries and the entire 31-entry uppercase-Greek derivation, and the full 656-test suite still passed. Same family, same round: the iteration-4 implementer's own first draft of the replacement test checked only `_ID_PATTERN`, which the *broken* output also satisfies, and caught that itself by mutation before shipping.

**3. `test_the_limit_counts_attempts_not_rows` (found iteration 5b).** Its fixture stored three papers that all had abstracts and none of which were already extracted, so the two things in the test's name were the same number. Inverting the `--limit` contract outright — making both skip branches consume the limit — left all 917 tests green.

And the near-miss behind the fourth of these rounds: the boundary rule that stops a character with no ASCII reading being trimmed off the end of a slug was pinned on `_slug` and not on `_binomial_slug`, its other call site. Reinstating the iteration-4 full strip there left all 917 tests passing — no test referenced `_binomial_slug`, `_organism_identity` or `_split` at all.

**The rule: a test asserting the current output of a function is not coverage of that function's contract.** It is a change detector. That is worth having, but it reports only that today's behaviour is today's behaviour, and it reports it just as confidently when today's behaviour is wrong.

What cases 1 to 3 have in common is how they were written — by reading the implementation and writing down what it does, rather than reading the contract and writing down what it must never do. That is also why each looked like coverage: they name the right function and exercise the right path.

Two things actually caught them, and neither is review by reading:

- **Mutation.** Break the thing on purpose and watch the test go red, before shipping the test rather than after someone asks. Every case above was found this way, and case 2's near-miss was found this way by the person writing it.
- **Deriving a test's universe from something other than the thing under test.** The transliteration coverage tests parametrize from Unicode, not from the table; the id test parametrizes over the real `data/gold/*.json`; the package export test derives its expectation from `extract.errors` rather than from a list, because a hand-written list is the same failure one level up — it can be incomplete in exactly the way the code is.

One corollary, because case 1 and case 3 both turned on it: check what a fixture actually contains before believing a test covers a case. Gold agent names are hand-normalised, so the gold-parametrized test never saw a non-ASCII character; the limit fixture had no skipped papers, so it could not tell attempts from rows. The assertion was fine in both. The inputs could not reach the failure.

### Cases 4 to 7, and the sweep that followed them (iteration 6)

Four more, all found in one round. **4.** The windowed-excerpt tests in `tests/test_model.py` used payloads under 40 characters, and `ingest.errors.excerpt` has a 200-character radius — so `text[0:400]` and `text[pos-200:pos+200]` were the same string and both call sites' `position=` arguments could be set to `None` with all 960 tests green. The token assertion did not help: the reason string interpolates the token itself, so it passed whether or not the excerpt reached the offending region. **5.** `tests/test_classify.py` asserted `request["model"] == CLASSIFIER_MODEL`, which pins the wiring and says nothing about the constant — `CLASSIFIER_MODEL = "claude-opus-5"` left everything green and deleted PLAN.md Phase 2's cost cascade outright. **6 and 7.** Both branches of the schema's description rule — `_flatten`'s `source is not node` skip and `_inherited_description`'s body — carry comments naming the bug they prevent, and either could be deleted with the suite green. Between them they decide what the model is told each field means, which is prompt content that no validated field would ever reveal.

Finding these one per round is the process this section exists to stop, so the rest of the package was swept in one pass rather than waiting for the next reviewer.

**Method.** For every function in `extract/`, read the contract it claims — docstring, PLAN.md, or CLAUDE.md — then write a mutation that makes that specific claim false. Not random mutation: `_ellipsis` gets its length bound raised, `check_quotes_verbatim` gets both sides case-folded, `repair_json` gets a rule that rewrites a body it promised to leave alone. Run the full suite, record whether anything turns red. Everything ran in a scratch copy, one mutation at a time, with every source file restored from a snapshot in a `finally` between runs — a mutation left in place would quietly falsify every result after it.

**Scale.** 71 function-level entries in `extract/`, counting a method-less class as one and each of the two modules that define no functions as one. 62 of them got at least one deliberate mutation, and 9 could not be given one — see "Where the sweep does not reach" below. **107 mutations in all: 93 in the sweep proper, plus 14 follow-ups run to correct or confirm one of the 93.**

**16 of the 107 left all 967 tests passing**, and the two survivor counts belong to different halves of the run. 79 of the 93 sweep mutations turned red, so 14 survived the sweep proper — and those 14 are the real findings listed below. The other two survivors are follow-ups, and they were the sweep's own fault rather than findings: `pass` inserted before a `raise` is a no-op, and `except UnicodeEncodeError` widened to `except UnicodeError` still catches the same exception. Neither inverted the contract it was aimed at, so neither could have gone red. Both were re-run as proper inversions and both contracts turned out to be pinned. Worth writing down: a mutation harness earns the same suspicion as a test, and the way to audit one is a second mutation that breaks the same contract differently.

**Fourteen real ones.** Twelve state their contract in a docstring, in PLAN.md, or in CLAUDE.md — written promises with nothing enforcing them — and are now covered:

- `repair_json` — "a body it cannot fix comes back unchanged", checked only on `{"a": 1}`: the one input none of the three repair rules can reach, having no fence, no prose and no comma.
- `schema_document` — "loading it once per process" had no test at all. The cache keys `validate_record`'s compiled validator by object identity, so an equal-but-fresh dict recompiles the schema once per paper.
- `extract_record` — the quote haystack was pinned against being *narrower* than the prompt and not against being wider. A wider one verifies a quote against text the model was never shown, which is the fabrication check running backwards.
- `load_schema` — annotated `-> dict[str, Any]`; a schema file containing a JSON array was accepted and returned.
- `build_extraction_schema` — the "no items schema" guard. Without it a missing `items` is a bare `KeyError`, and a non-dict one is sent to the model as the request schema.
- `check_quotes_verbatim` — "case, punctuation and unicode differences are real differences". Lowercasing both sides of the comparison passed every verbatim test in the file.
- `_closest_window` — "anchored on the longest block the two share". Returning the top of the text, or the empty string, changed nothing.
- `_ellipsis` — "one line, and short enough to read in a terminal". The limit could be raised to a million.
- `_resolve_composition` — "in application order", reversible, because every wrapper in the real schema composes exactly one definition. Now pinned with a synthetic document.
- `_is_code_assigned` — the path condition scoping `experiment_id` and `notes` to the experiment level could be deleted.
- `_error_path` — "renders as `experiments[0].organism.value`". The bracketed index could go.
- `build_parser` — `--limit`'s default could be changed while `--help` went on advertising the constant.

**Two were left, deliberately, and are recorded rather than fixed:**

- `Classification` is `frozen=True`, and nothing anywhere says a gate decision is immutable; unfreezing it changes no observable behaviour. A design choice, not a contract.
- `extraction_schema` caches its derived schema, and unlike `schema_document`'s its docstring does not say so. Rebuilding per call is a cost regression that violates nothing written down.

**Where the sweep does not reach.** Nine of the 71 entries got no mutation, and they are holes rather than clean results. `MessagesResource.create` and `ModelClient.messages` are `Protocol` stubs with `...` for a body — no behaviour to invert. `_RefusedNumber.__init__` and the four error classes that declare no methods (`ExtractError`, `OutputPathError`, `ExperimentIdCollisionError`, `RecordValidationError`) hold their contract in a base class or a message rather than in code that can be made wrong; `ConfigurationError` was mutated as the representative of "every error here is an `ExtractError`" and the rest were not. `ModelCallError` and `ModelResponseError` are not in that group and are not holes: each declares methods of its own — `from_api_error`, and `from_payload`, which builds every windowed excerpt this package emits — so both are ordinary function-level entries, mutated like any other. Making `from_payload` ignore the `position` it is handed turns tests red, and dropping the excerpt from its message turns fourteen red. **That keeps the hole count at nine, not ten.** `RunSummary` declares six counters and no methods, so what is invertible is the code that increments them. And `extract/__main__.py` is a two-line shim with no function to mutate — which surfaces something worse than a sweep gap: **no test runs `python -m extract` at all.** The entry point the whole CLI contract is written for is the one thing nothing exercises.

**What the shape of the result says.** 79 of the 93 sweep mutations turned red — 91 of all 107, counting the follow-ups — and they are the ones that matter most: every honesty invariant, both loud-failure paths, the identity generator, the transliteration table, the gold-set write guard. The suite is not decorative. But the twelve real gaps have a shape. **Four** are in the reporting layer — how a window is chosen (`_closest_window`), how a path is rendered (`_error_path`), what `--help` advertises (`build_parser`), what an excerpt is built from (`_ellipsis`). Two are guards that raise on a malformed schema (`load_schema`, `build_extraction_schema`). Two are comparison semantics (`check_quotes_verbatim`, and `extract_record`'s haystack). Two are unobservable against the real schema file and needed a synthetic document to reach (`_resolve_composition`, `_is_code_assigned`). One is a cache (`schema_document`).

**That is eleven, and the twelfth fits none of the five buckets: `repair_json`.** It is not reporting, not a guard, not a comparison, not a cache, and it is entirely observable against real input. It is the untrusted-input heuristic itself, and its gap is the fixture rule below rather than a category of function — the only body the test fed it was `{"a": 1}`, which is precisely the one input none of the three repair rules can reach. The bucket list is left at five with the remainder named, rather than stretched to absorb it: an accounting that balances by widening a category until everything fits describes nothing. The taxonomy previously read "Five are in the reporting layer" while naming four things, and summed to twelve only through that one-item overcount. Every member is now named so the count can be checked instead of trusted.

Almost none of that is the happy path, and that is the point: a function whose job is to describe a failure only ever runs on the sad one, and the sad ones are what the tests were not written from. `_closest_window` and `_ellipsis` exist for no reason except to make a fabricated quote legible to a human, and neither had a single assertion.

**The second rule, from the fixtures again.** Cases 4, 6 and 7, and three of the twelve above, failed for exactly the reason cases 1 and 3 did: the input could not reach the failure. A 40-character payload is shorter than the excerpt window. `{"a": 1}` has no comma. A claim wrapper composing one definition cannot exhibit an ordering bug. So: before believing a test covers a rule, ask what feature of the input that rule keys on, and check that the fixture has it. That is a different question from "does this test exercise the function", and it is the one that keeps coming back.

### What the sweep did not reach: two more doors, one round apart (iterations 7 and 8)

The sweep ran over every function in `extract/` and declared `extract/model.py` covered. **Two more ways out of that module's central guarantee — that a failure originating in model output leaves as an `ExtractError` and nothing else — were found in the two rounds after it.** Neither is a regression from the sweep's twelve fixes. Both were there while it ran, and it could not see either. That is what this section exists to record about a sweep: its reach, not its success.

**Door one, payload-side: `RecursionError` (iteration 7a).** Roughly two kilobytes of `[` exhausts the decoder's stack. `RecursionError` is a `RuntimeError`, so neither of `parse_payload`'s two handlers took it — not `json.JSONDecodeError`, not `_RefusedNumber` — and it went past the retry in `call_structured`, past the per-paper `except ExtractError` in `extract/cli.py`, and ended a batch run on a raw traceback with every later paper unattempted. Depth 200 parses; 1000 and 2000 escaped. It is the same defect `_bounded_int` had been added one round earlier to close, arriving through a door nobody had tried.

**Door two, SDK-side: `anthropic.AnthropicError` (iteration 8a).** `_create` caught `anthropic.APIError` and its docstring promised it converted *any* SDK failure. `APIError` is not the SDK's exception root — it is a child of `anthropic.AnthropicError`, and three exported types sit outside its subtree: `AnthropicError` itself, `RetryableError`, and `WorkloadIdentityError`. All three escaped `_create` and `classify` as themselves. `RetryableError` is the one with consequences: the SDK's transport middleware propagates exceptions as-is unless they opt into the retry policy by type, so one that outlasts the retry budget arrives here intact. This contradicted two docstrings in the file at once — `_create`'s "converting any SDK failure into a `ModelCallError`", and `call_structured`'s "never lets an SDK exception escape — a caller processing a batch must be able to handle one paper's failure by catching one type." The fix is one identifier.

**It was invisible for the fixture reason, again — and this is the sharpest instance of it yet.** Three tests covered the SDK error path: an `APIStatusError` carrying 529, an `APIConnectionError`, an `APITimeoutError`. Every one of them builds its input from an `APIError` subclass. The rule the bug keys on is *the exception's position in the SDK's class hierarchy*, and all three fixtures sat on the same side of it, so no assertion in any of them could have reached the failure however it were written. Restated as an experiment rather than an argument: reverting the catch to `APIError` turns the new test red in all three of its cases and leaves those three older tests green. The three green ones are the finding.

The replacement test derives its universe from the installed `anthropic` package — every exported exception that is an `AnthropicError` and not an `APIError` — rather than from a list written here. That is the second of the two things that actually catch these, applied deliberately: a hand-written list of SDK error types can be missing exactly the type the `except` clause is missing, which is the same failure one level up. A companion test asserts the premise, that the remainder is non-empty, because a parametrization over nothing is a green test that ran nothing.

**And the negative result, which is worth as much as the two positives.** Iteration 8a went looking specifically for a fifth payload-side door and did not find one. It enumerated what `json.loads` can raise under all three number hooks; established that `TypeError` and `UnicodeDecodeError` are unreachable because `response_text` always returns a `str`; measured that `repair_json` cannot raise, against a 2 MB body, at 0.7 ms and with no backtracking blowup; and fuzzed about thirty adversarial payloads and seven adversarial records. Every failure came out as an `ExtractError`. The door it found was on the SDK side, where it had not been looking. A deliberate search that comes back empty is a result, and recording only the doors that were found would make this section a list of defects instead of a map of where the risk is and is not.

**Running tally on the second rule.** It has now failed **four** more times since it was written down, in four consecutive rounds, and none of the four is on the numbered list above: iteration 7a's coordinate-space window, where the excerpt test written that very round had no fence and no prose, so `repair_json` returned the body unchanged and the two-candidate branch never ran; iteration 7b's whitespace-only abstract, where the guard keys on `.strip()` and the fixture only ever supplied `None`; iteration 8a's SDK hierarchy above; and iteration A+D's identical outcome halves, below. They are not numbered as decorative cases because they are not that shape — each test asserts the right thing about the case it does reach, and iteration 7a's reviewer looked for a decorative test in this scope and reported none. The shape is the other one: the assertion is fine, the fixture cannot get there. Seven decorative cases were found in four rounds and none since; the fixture rule has failed in every round since it was written. It is the one to check first. Every instance of it is listed by name at the end of this section, under *Every instance of the fixture rule, named* — a running tally is exactly the kind of number this section exists to distrust.

### The fixture rule again, iteration A+D: an outcome half that was the same bytes every time

The **eleventh identified** instance of the fixture rule in this repo, and the **fourth consecutive round** in which the rule has failed. The review that found it called it the eighth and this section's own arithmetic made it the ninth; both numbers were recorded side by side for a while, which was the wrong answer twice over — the population had never been enumerated, so neither number was re-derivable and the disagreement was about a set nobody had written down. It is enumerated at the end of this section now.

**What was not covered.** The A+D split made extraction two calls, and `_merge_outcomes` joins the second call's outcomes onto the first call's experiments by `experiment_index`. That join is the entire reason `experiment_index` exists: a reordered response is a *legal* response, and `OUTCOME_SYSTEM_PROMPT` promises the code will honour whatever order it comes back in. **Nothing in the suite distinguished it from a positional join.** Replacing `zip(indices, outcomes)` with `enumerate(outcomes)` left all 1077 tests green; so did reversing the correspondence, and so did rotating it one place. Under any of the three, every outcome attaches to the wrong experiment and the record still validates, still satisfies `check_provenance`, and still passes `check_quotes_verbatim` — the quotes are all real sentences of the same paper, so they verify wherever they land. Rapamycin's effect filed under metformin, with a genuine quote behind it. This is the exact silent misattribution the index mechanism was added to prevent, and it was the one member of that family with no test: the dropped outcome, the duplicated index, the extra outcome and the out-of-range index each had one.

**Why no assertion could have caught it.** `conftest.experiment_payload()`'s *outcome half* — `mechanism` and `lifespan_effect` — was byte-identical for every experiment any test in the suite built. The multi-experiment fixtures vary `organism`, `species` and `agent`, and all three are identity-half fields. So the three outcomes being joined were the same three key-value pairs in every test that had more than one experiment, and a permutation of them is the identity function. No assertion, however written, can distinguish objects that are equal.

**Fixed in the fixture, not in a test — at the second attempt.** `experiment_payload` derives its outcome half from its identity half: the *whole* half, serialised with sorted keys, read after any `**overrides` have been applied, with `mechanism` embedding that string verbatim. So the derivation is injective in the identity half, and two experiments whose identity halves differ anywhere carry different results — no per-axis maintenance when a property joins `IDENTITY_PROPERTIES`. The first attempt derived from three *arguments* instead, and was not injective at all: of eleven experiments a test could plausibly build, nine produced byte-identical outcome halves — `sex=male` against `sex=female`, a low dose against a high one, `organism="M. musculus"` against `organism="other"` with that species, which are precisely the axes `data/gold/` uses to separate experiments within one paper. It was dormant, because the fixtures that build several experiments happen to vary `agent`; see the near-miss below. **And the premise is now an instrument rather than an assertion:** `outcome_payload` refuses any fixture of more than one experiment whose outcome halves are not pairwise distinct, so a vacuous join fixture cannot be written rather than merely having not been written, and it guards every future multi-experiment fixture instead of one function in one file. Three fixtures were rewritten as fallout, all of them repeated-pair fixtures that had used three byte-identical experiments; they now differ by `sex`, which is how `harrison2009` actually splits its rapamycin arms and which the id generator does not read, so the `-2`/`-3` behaviour they pin is unchanged. The join itself is pinned twice — the in-order response asserted outcome by outcome rather than merely "has a `lifespan_effect`", and a parametrized out-of-order response covering both rotations and the reversal. Measured after: the positional join turns 3 red, the reversal 4, the rotation 4. One further test had moved as fallout of the first attempt — the `not_reported` honesty test now sets `mechanism` to null itself instead of inheriting a null it never asked for, which is what that test should have been doing anyway.

**What is new in this instance, and worth adding to the rule.** Every earlier one turned on a missing *property of one input*: a payload shorter than the excerpt window, an abstract that was `None` rather than whitespace, an exception on the wrong side of a class hierarchy. This one turns on a missing *relation between two inputs* — that two experiments differ. The fixtures here were varied, and thoroughly; they were varied only in the half the code under test does not read. So the fixture question has a second form: not just "does the input have the feature the rule keys on", but "when the rule is about two things being matched up, can the fixture tell them apart at the point where the matching happens".

**The near-miss, and it is the same rule one level down.** The first fix above claimed injectivity in two docstrings and did not have it. What the derivation actually read was three arguments, so it separated experiments on the axis the existing fixtures happen to use (`agent`) and merged them on the axes the gold set uses (`sex`, dose, the `organism`/`species` collapse). Nothing was vacuous *yet*: no test built two experiments differing only on one of those axes. The cost would have been paid by the next author to write a join test, who would have reached for the project's own natural axes, landed in the collision set, and got a green test that could not fail — with a docstring in front of them promising that could not happen. Recorded as a near-miss rather than as a member of the list below, on the same footing as the `_binomial_slug` near-miss above: it was caught in review before any test rested on it. The lesson it adds is narrow and worth having — **a fixture derivation that is claimed injective should be derived from the whole of what it claims to be injective over**, not from an argument list that has to be maintained by hand as the thing it summarises grows.

### Every instance of the fixture rule, named

The count of these was recorded twice and reconciled neither time. Neither number was defensible, and the reason was not that the count was in dispute: **the population had never been enumerated.** The sentence that defines it is above — "Cases 4, 6 and 7, and three of the twelve above, failed for exactly the reason cases 1 and 3 did" — and *three of the twelve above* names nothing. So the remedy this section already prescribes for itself is the one that applies: every member is named, and what cannot be named is counted apart rather than folded into a total.

Membership below is exactly what the sentence above asserts; it is not re-adjudicated here. Where a member's shape is really "no test at all" rather than "the fixture could not reach it", that is said in its own line.

**Eleven identified**, oldest first:

1. **Case 1**, iteration 3a — the gold-parametrized id test. Every `intervention.agent.value` in `data/gold/` is hand-normalised to ASCII, so the one test that feeds the generator real values never fed it a non-ASCII character.
2. **Case 3**, iteration 5b — `test_the_limit_counts_attempts_not_rows`. Three papers, all with abstracts and none already extracted: no skipped paper anywhere in the fixture, so "attempts" and "rows" were the same number by construction.
3. **Case 4**, iteration 6a — the windowed-excerpt tests in `tests/test_model.py`. Payloads under 40 characters against `excerpt`'s 200-character radius, so the centred window and the first 400 characters were the same string.
4. **Case 6**, iteration 6a — `_flatten`'s `source is not node` skip. Absent coverage rather than an unreachable fixture: nothing in the suite read a description out of the derived schema at all.
5. **Case 7**, iteration 6a — `_inherited_description`, the opposite half of the same rule, and absent in the same way.
6. **`repair_json`**, iteration 6's contract sweep — "a body it cannot fix comes back unchanged", checked only against `{"a": 1}`: no fence, no prose, no trailing comma, which is the one input none of the three repair rules can reach.
7. **`_resolve_composition`**, iteration 6's contract sweep — "in application order", unobservable because every wrapper in the real schema composes exactly one definition, and a one-element sequence has no order to get wrong.
8. **Iteration 7a**, the coordinate-space excerpt. The excerpt test written that very round used `long_payload()`, which has no fence and no prose, so `repair_json` returned it unchanged, `candidates` had length one, and the two-candidate branch the bug lives in never ran.
9. **Iteration 7b**, the whitespace-only abstract. `_papers` guards `not paper.abstract or not paper.abstract.strip()`; every fixture supplied `abstract=None`, so the `.strip()` arm had no input.
10. **Iteration 8a**, the SDK exception hierarchy. All three SDK-error tests built their input from an `APIError` subclass, and the rule the bug keys on is the exception's position in that hierarchy — so no assertion in any of them could have reached it however written.
11. **Iteration A+D**, the identical outcome halves, described immediately above.

**And one described but never named.** The defining sentence claims *three* of the sweep's twelve. Two are identifiable from what the text says about them — `repair_json`'s `{"a": 1}` and `_resolve_composition`'s single-definition wrapper, numbers 6 and 7 above. The third is asserted and never identified. `_is_code_assigned` is the natural guess, being the other of the two the sweep calls "unobservable against the real schema file", but a guess entered on a list like this is the failure the list exists to prevent. It is left unnamed and counted separately.

**So: eleven identified, plus one described but never named.** Twelve described in total, which is not a number to quote — the honest form is the pair. The A+D instance is the eleventh identified, and neither "eighth" nor "ninth" was ever recoverable, because both were computed over a set that had not been written down.

---

## 2026-08-13 — `data/extracted/`: where Phase 2's output goes

Recorded because it was nowhere. The directory existed only in `.gitignore` and in `extract/cli.py`; PLAN.md's architecture block enumerated `data/gold/` and `data/drafts/` and stopped, and this file never mentioned it. Phase 3 reads both documents for the layout and would not have found the directory its eval inputs live in. PLAN.md's architecture block now lists it too.

**What it holds.** One JSON file per extracted paper, under a directory named for the schema version the record was written against: `data/extracted/<schema_version>/<slug>-<digest>.json`. The record is shape-identical to a `data/gold/` file and validates against the same `schema/experiment.schema.json` — that is the whole point, since Phase 3 compares the two field by field. The filename is a readable slug of the paper's `dedup_key` plus an eight-character SHA-256 digest of the same key; the digest is load-bearing, because two dedup keys can slugify to one string and a collision would present as "already extracted" rather than as an error.

**Who writes it.** `python -m extract`, and nothing else. `extract/cli.py::DEFAULT_OUT_ROOT` is `<repo>/data/extracted`, overridable with `--out DIR`, which writes to `DIR/<schema_version>/` on the same rules. Every write goes through `_write_record` — `mkstemp` in the target directory then `os.replace` — because the *existence of the file is the only "already extracted" marker there is*. There is no database column recording that a paper was extracted, so a truncated file is worse than no file: every later run would read it as done, the paper would never be re-extracted, and the truncated JSON would flow into Phase 3. Idempotence is per (paper, schema_version), exactly as the key invariants require, and it is the versioned subdirectory that delivers the second half of that — bumping the schema forces a fresh extraction instead of silently reusing a record written against an older shape.

**It is gitignored, deliberately.** It is machine output, reproducible from the database and the prompt, and it must never be mistaken for ground truth. `resolve_out_root` refuses any `--out` that resolves inside `data/gold/` for the same reason: an extracted record is indistinguishable from a gold one on disk, so a run pointed at the gold set would leave Phase 3 scoring the model against its own output and reporting the result as accuracy.

**Phase 3 reads this directory.** The eval harness compares records here against `data/gold/`. Two things it will need that are settled elsewhere in this file and not here: the alignment key between a gold record and an extracted one is an open question reserved to the human (see the `experiment_id` entry of 2026-08-12 — the `(organism, agent)` fallback was withdrawn and no replacement is proposed), and quotes in gold records are transcribed from the PubMed abstract text, which is what `extracted_from: "abstract"` records are generated from too.

### Open question, reserved to the human: JSON files or Postgres

**Whether extracted records ultimately belong in `data/extracted/` as JSON at all is not settled, and is not settled here.** What is written above is what the code does today, and it was chosen by the implementation rather than ratified.

The case for the filesystem is what Phase 2 needed: the file's existence is a free, crash-safe idempotence marker; the records are diffable and readable during prompt iteration; and no database round trip stands between an extraction run and someone reading its output. The case against it is that two other parts of PLAN.md assume a table — the stack names PostgreSQL, and Phase 4's `GET /experiments?organism=&intervention_type=&mechanism=&direction=` is a filtered query over every record, which is a table scan of a directory tree if the records stay as files. Phase 1 already stores raw papers in Postgres, so a run currently reads from a database and writes to a directory.

The decision is the human's. It has a deadline of sorts: Phase 3 reads whatever exists, and Phase 4 has to query it, so the longer both hold the answer implicitly the more code assumes the current one. Noted rather than resolved, and the current behaviour is documented above so that whichever way it goes, what is being migrated *from* is written down.

---

## 2026-08-13 — Two gaps the extractor cannot close on its own

Both surfaced in Phase 2 review. Neither is a Phase 2 fix: one needs a schema
change, the other an eval-design decision. Recorded here so Phase 3 meets them
as known shapes rather than as anomalies in its numbers.

### `organism: "other"` with no species defeats the id collision guard

`_organism_identity` returns `(slug, source_value)`, and `_experiment_id`
compares the *source values* of two ids to decide whether a numeric tail is
honest disambiguation or a silent merge. When `organism` is `"other"` and
`species` is absent, it returns `("other", "other")` — the same pair for every
such record. Two genuinely different organisms in one paper therefore present
as one intervention reported twice: the collision guard does not fire, and the
records are separated by `-2` alone.

`eisenberg2009` is exactly this shape. Its three records are *C. elegans*,
*S. cerevisiae* and *D. melanogaster*, all spermidine, and the latter two both
carry `organism: "other"` — the enum is the MVP filter vocabulary and neither
yeast nor fly is in it. The gold labels populate `species` on both, which is
the only reason they get distinct ids. An extracted record that omits `species`
on a paper of this shape produces the merge instead, and nothing downstream can
tell it happened.

**The fix is upstream, not here.** The schema already says to populate `species`
whenever `organism` is `other`, but says it as guidance rather than as a
constraint, so a record without it is valid. Making it required when
`organism == "other"` is a **v0.5.0 schema change** — it changes what validates,
which is a decision about the gold set and the API surface, not about the
extractor. `extract/extract.py` already enforces two invariants the schema
cannot state; this would be a third, except that here the honest place is the
schema itself. Deliberately not worked around in Phase 2 code.

### A quote straddling the title/abstract join verifies

`check_quotes_verbatim` matches against the exact prompt the model was shown,
which is `f"Title: {title}\n\n{text}"`, and the whitespace-collapsed comparison
turns that join into a single space. So a "quote" whose first half is the tail
of the title and whose second half is the head of the abstract passes the
verbatim check although that sentence appears nowhere in the paper — the
assembled-from-two-places fabrication the system prompt forbids, verified as
genuine.

The same weakness exists across sentence boundaries inside the abstract, so it
predates the title fix; including the title widened it by adding one more join,
and a high-value one, since the title is where the organism and the agent
usually are. Checking each quote against the title and the text as two separate
haystacks would close it without giving up the title fix.

Not done in Phase 2, because it trades one false-accept shape for a
false-reject shape — a quote legitimately spanning two sentences would start
failing. **Open question for Phase 3, reserved to the human:** does the
verbatim check run against one haystack or per-region?

#### What the gold set actually says about quote shape

Measured 2026-08-13 over all 221 `source_quote` values in `data/gold/`,
enumerated with `scripts/check_gold.py::iter_quotes` rather than by pattern
over the JSON:

| | count | share |
|---|---|---|
| total quotes | 221 | — |
| a single whole sentence | 100 | 45.2% |
| **not a whole sentence** | **121** | **54.8%** |
| **spanning a sentence boundary** | **5** | **2.3%** |

96 quotes are `extracted_from: abstract`, 125 `full_text`.

Two things follow, and they are not the same number — worth separating, because
one sentence of prompt and one design decision each turn on a different one.

**54.8% are sub-sentence slices** — clauses like `extends the lifespan of male,
but not female, mice by 23%`, and table rows. Whole sentences are the minority.
The system prompt briefly told the model "if the sentence that states the value
is long, the quote is that whole sentence", which contradicts how the set was
labelled; Phase 3 would have scored the resulting mismatch as model error when
it was a prompt error. Removed. The prompt now states the convention as
labelled: a contiguous verbatim slice, no ellipsis and no editorial brackets,
long enough to carry the value and what the value refers to.

**Only 2.3% — five quotes — span a sentence boundary at all**, and all five are
`full_text` mechanism or background sentences. That is the number bearing on
the straddle question above: switching the verbatim check from one haystack to
per-region would false-reject on the order of five of 221 gold quotes, against
closing a fabrication shape that currently verifies. It does not settle the
question — the rate in *extracted* output is not the rate in gold, and the
title/abstract join is a different boundary from the sentence joins measured
here — but it does mean the false-reject cost is small and countable rather
than unknown. Method caveat: sentence boundaries were detected on the quote
text itself, with an abbreviation list (`e.g.`, `et al.`, `Fig.`, initials) to
avoid counting a period inside an abbreviation; the gold records do not carry
the abstract, so this measures the quotes' own shape rather than their position
in the source.

---

## 2026-08-13 — The live run: the endpoint rejects the derived schema

First run against the real API. Seven of eight papers were screened out, one
reached extraction, and it came back **HTTP 400 from `claude-opus-5`** on
`doi:10.1016/j.mad.2025.112088`:

> Schemas contains too many parameters with union types (24 parameters with
> type arrays or anyOf). This causes exponential compilation cost. Reduce the
> number of nullable or union-typed parameters (limit: 16 parameters with
> unions).

Everything around it behaved as built: the paper failed, the failure was
counted and printed, the summary ran, the batch survived, the exit status was
non-zero. The pipeline is sound; the schema it sends is not accepted.

### What the tests could not have caught, and why

The `anthropic` request surface was verified by hand against the installed SDK
during review — that `output_config` is the real parameter, that the nesting is
`{"format": {"type": "json_schema", "schema": …}}`, that the model ids exist.
**Nothing verified that the schema inside that parameter is one the endpoint
will accept.** Every test stubs `messages.create`, so the suite validates the
schema against `jsonschema` — which is happy with it — and never against the
service, which applies its own limits. `requirements.txt` already conceded that
the suite cannot catch a request-shape regression and that the version floor
was the only guard; this is the same blind spot one level in, on the payload
rather than the parameter.

The general shape: a stub can prove you send what you meant to send. It cannot
prove the other end accepts it. Anything that is only true of the live service —
compilation limits, undocumented caps, per-model differences — is invisible to
a fully stubbed suite by construction, and the only instrument for it is a
live call. This is the second thing the loop could not reach, after the
prompt-quality question, and both were found in the first minute of a real run.

### The 24, counted

Every union in the derived schema is `X | null`. **Not one is a genuine
polymorphic union** — there is no `string | number` anywhere. So the entire
count is the cost of representing absence.

| group | count | why it is a union |
|---|---|---|
| `source_quote` on every claim wrapper | 15 | the provenance rule: when a value is null or `not_reported`, its quote is null too |
| `value` on optional claims | 9 | `species`, `sample_size`, `dose`, `age_at_start`, `mechanism`, `median_change_pct`, `mean_change_pct`, `max_change_pct`, `p_value` — absent data is null |
| **total** | **24** | limit is 16 |

Two facts from the gold set that bear on any fix:

- **Four claims never have a null quote and cannot have a null value**:
  `organism`, `intervention.type`, `intervention.agent`,
  `lifespan_effect.direction`. All 26 gold records carry a real value and a
  real quote for each. Their quote nullability is structurally unnecessary
  under the current convention, not merely unused.
- **For all nine nullable-value claims, the null-quote count equals the
  null-value count exactly** — 15/15 for `dose`, 21/21 for `mean_change_pct`,
  and so on. The invariant "value null ⟺ quote null" is not just enforced by
  `check_provenance`; it is what the labelled data actually looks like.
  `sex` and `strain` are the two that carry `not_reported` in the value and a
  null quote (5 and 8 records), which is the same rule wearing a sentinel.

### Not decided here

Getting under 16 means changing how absent data is represented in the request,
which is a schema question and an eval question before it is an implementation
one — `data/gold/` is labelled under the null convention, and Phase 3 scores
extracted output against it. Options and their trade-offs were reported to the
human; none is implemented. **Reserved to the human**, alongside the
`experiment_id` convention and the eval alignment key.

### Decision (human, 2026-08-13): A + D

**A — drop nullability on the four structurally-unnecessary quotes**
(`organism`, `intervention.type`, `intervention.agent`,
`lifespan_effect.direction`). Their values can never be absent and their quotes
are never null in any of the 26 gold records, so this aligns the schema with
the facts rather than changing how anything is represented. 24 → 20.

**D — split extraction into two requests.** Keeps the gold set's
representation untouched, which keeps the like-for-like eval design intact.
Costs a second Opus call per extracted paper: **extraction cost roughly
doubles per paper that passes the gate.** The classifier gate is unchanged, so
screened-out papers cost the same as before.

**C rejected — sentinel strings for optional values.** It would have turned
the four percent fields into strings, losing schema-level type checking on
exactly the fields PLAN.md names as the hardest to get right (median vs mean vs
max), and making Phase 3's numeric-tolerance comparison run on parsed strings.
The measurement matters more than one round trip.

**B rejected — optional-not-nullable properties.** Its headroom was the
largest, but it rests on the endpoint honouring optional properties the way we
expect, and that is the same class of unverified assumption about the live
service that produced this defect in the first place. Not a thing to bet the
schema on before probing it.

**E rejected — dropping optional claims.** `mean_change_pct` is the
discriminator for the median-vs-max confusion PLAN.md explicitly watches;
removing it to buy schema headroom would remove a Phase 3 metric.

---

## 2026-08-13 — A + D implemented: four quotes, two calls

What the decision above turned into. Nothing in
`schema/experiment.schema.json` changed, and nothing about how absence is
represented changed.

### A — the override, and why it is an override

`source_quote` is declared nullable once, in `$defs/provenance`, and all
fifteen claim wrappers inherit it through `allOf`. Four of them cannot use it:
`organism`, `intervention.type`, `intervention.agent` and
`lifespan_effect.direction` each carry a value that can never be absent, so
under the provenance rule their quote can never be null. Dropping those four
unions takes the derived request from 24 union-typed parameters to **20**,
measured.

It is implemented in the derivation — `extract/schema.py::_non_nullable_quote`,
keyed on `NON_NULLABLE_QUOTE_CLAIMS` — and not in the file. Opening v0.5.0
inside Phase 2 would move the gold-set contract mid-phase, and v0.5.0 is
already the container for `species`-required-when-`organism == "other"` and the
other candidates that belong to Phase 3.

**The correct form of this change, deferred to v0.5.0.** When that version
opens, fold the override into the file and delete it from the derivation:

- a `provenance_quoted` `$def`, identical to `provenance` but with a
  non-nullable `source_quote`, referenced by `claim_organism`,
  `claim_intervention_type` and `claim_direction`;
- a new `claim_string_quoted` for `intervention.agent`, so that it stops
  sharing `claim_string` with `strain` — `strain` legitimately carries the
  literal `"not_reported"` and a null quote in 8 of the 26 gold records, and
  the two cannot keep sharing a definition once one of them loses its
  nullability.

Until then the override restates a fact the schema file does not carry, which
is the same class of drift as `_ID_PATTERN` duplicating the schema's regex
(`DEBT.md` D6). It is closed the only way a restatement can be: the fact is
checked against the data.
`tests/test_extract_schema.py::test_no_gold_record_has_a_null_quote_on_a_non_nullable_claim`
enumerates the claims of every `data/gold/` record with
`scripts/check_gold.py::iter_claims` and fails if any of the four is ever null
— and asserts the per-path count as well, so a path that stopped matching
cannot make it pass by finding nothing.

### D — two calls, and where the seam runs

The split is a partition of the experiment object's properties, and both
request schemas are derived from the same file by the same code:

| call | properties | union-typed parameters |
|---|---|---|
| 1 — the experiment as designed | `organism`, `species`, `strain`, `sex`, `sample_size`, `intervention.*` | 10 |
| 2 — what it found | `mechanism`, `lifespan_effect.*` | 10 |

Six under the endpoint's limit of 16 each, and the suite holds them to 12 —
`test_each_request_schema_stays_well_under_the_union_limit`, measured on the
schemas the extractor actually sends. That test cannot prove the endpoint
accepts either schema; nothing in a fully stubbed suite can, which is the whole
lesson of the entry above. It proves only that the count has not crept back.

**How the two join.** Call 1 fixes the list of experiments and their order.
Call 2 is shown the same paper prompt plus a numbered list of the identities
call 1 returned, and answers one outcome per number, carrying a required
integer `experiment_index` — non-nullable, so the join itself costs no union.
`_merge_outcomes` requires the returned index multiset to be exactly
{0 … N-1} before it merges anything, which is the one condition that rejects a
dropped outcome, a duplicated index, an extra outcome and an index naming no
experiment. Each has a silent reading — a record with no result, two
experiments sharing one, a finding thrown away — and none of them is visible in
the assembled record, which validates either way.

**The verbatim haystack is the paper prompt only, for both calls.** Call 2's
user message additionally carries the echoed identity block, and that block is
call 1's output rather than source text: a quote lifted from it would verify
while appearing nowhere in the paper, which is exactly the fabrication the
check exists to catch. The prompt is built once and passed to both calls as the
haystack; the echo is excluded deliberately and there is a test that lifts a
quote out of the echo and expects a rejection.

**Two system prompts.** The shared rules — this paper's own results versus
background, how a quote and a confidence are chosen, that absent data is
recorded as absent — are written once as module constants and interpolated into
both, so the two cannot drift apart on the guidance they share. Each half keeps
the field guidance for the fields it asks for: `organism`/`species` and the
`not_reported` fields with call 1, median-versus-mean-versus-max and
`no_effect` with call 2. One prompt
for both would have told call 2 to "record one experiment per (organism,
intervention) pair", which is precisely the instruction that would make it
re-derive the list instead of reporting against the one it was given — the
failure the merge exists to catch, invited by the prompt.

**Where the per-experiment steps now run.** `_refuse_code_assigned` runs on
both halves as they arrive, because either response can volunteer a key it was
not asked for, and on the outcome half an unnoticed one is quietest: the
field-wise merge copies the two fields it knows about and would drop the rest
without a word. `_refuse_unknown` **ran** on the outcome half only at first, on
the reasoning that an unexpected key on an identified experiment survives into
the record and `validate_record` refuses it downstream. That reasoning was
wrong twice over, and review found it: a key that is a real `experiment`
property belonging to the *other* half — `mechanism`, `lifespan_effect` — never
reaches `validate_record` at all, because the merge hits it first and reports
it as a partition fault it had nothing to do with; and even for the keys
`validate_record` does catch, catching them there means the second Opus call
has already been paid for. It runs on both halves now, and the identity-half
refusal fires before call 2. `_stamp_provenance` runs once, after the
merge: `extracted_from` is a fact about the text, both calls read the same
text, so there is one value and one place to write it. `experiment_id` is
assigned after call 1, because it is built from call 1's fields alone and a
paper whose ids collide is refused — refusing it there saves the second call
rather than paying for it first. `_paper_metadata` moved ahead of both calls
for the same reason: it reads only the ingest row, so a paper that cannot
supply a DOI or a year is refused for nothing.

**No partial record, structurally.** `extract_record` has no early return, the
merged record is validated against the full real schema and both halves of the
provenance invariant before it is returned, and `extract/cli.py::_write_record`
is only reachable from a record it returned. A paper whose first call succeeds
and whose second fails therefore produces no record and no file, and — since a
file's existence is the only "already extracted" marker there is — stays
pending for the next run. There is no cleanup step because there is nothing to
clean up.

**Cost.** One extra call to the expensive model per paper that passes the gate:
extraction cost roughly doubles per extracted paper. The classifier gate is
unchanged, so screened-out papers cost exactly what they did before.

### What this did not touch

`MAX_TOKENS` is unchanged and shared by both calls. It caps one response, and
each response is now a part of what one used to return, so the same number is
more headroom rather than less.

Two checks were added that the split implies rather than requires:
`_require_properties` on both halves, refusing a response that omits a property
its request schema marked required. Most of these fields are optional in the
real schema, so an omission used to validate perfectly well and land as a
record silently missing `sex` — which is not the same claim as
`sex: "not_reported"`, and Phase 3 would have scored the difference.

### The prompt-diff comment, removed — a hand-maintained diff is the wrong instrument

A comment above `_OWN_RESULTS_RULE` in `extract/extract.py` used to narrate how
the two system prompts differ from the single prompt they were cut from: what
was moved, what was reworded, and which rewording was deliberate. **It has been
deleted.**

It carried a false statement in three consecutive review rounds, and the third
round's rewrite was itself the correction of the second's. The last version
claimed the prompts were the single prompt "cut, not reworded" apart from two
named sentences, when the single `_ABSENCE_RULE` bullet had been rewritten
rather than moved and `a starting age` has no counterpart in the old prompt at
all — verified as zero hits for `age_at_start` or `starting age` anywhere in
`f4f0f9d:extract/extract.py`'s `SYSTEM_PROMPT`. An audit trail that has been
wrong three times running is worse than no audit trail: a reader who checks it
learns nothing they could not get from `git show`, and a reader who trusts it is
misled. `git` already holds the diff, exactly and for free, and it is the only
copy that cannot drift.

What was load-bearing in that comment was not the diff. It was one fact about
the code: `_QUOTING_RULE` says "copied verbatim from the **paper** text above",
and the word `paper` is doing work, because call 2's user message is the paper
prompt *plus* the echoed identity block while only the paper prompt is ever a
quote haystack. That fact now sits immediately above `_QUOTING_RULE`, stated as
what the code does rather than as what a past edit changed, next to the string
it constrains and pointing at `extract_record`, which enforces the other half.

**The general rule.** A comment describing how the code differs from a previous
version of itself has no mechanism keeping it true — nothing fails when it goes
stale, and it goes stale on the next edit. State the invariant the code holds
now, next to the code that holds it, where a reader can check it against what
is on the screen.

## 2026-08-14 — protect_paths.py hook was silently inert

`protect_paths.py` raised `TypeError` at import under Python 3.9: the annotation
`list[str] | None` in `split_argv` is evaluated at import time on 3.9, and PEP 604
unions are not supported there. Both interpreters on the primary machine are 3.9
(system 3.9.6, conda 3.9.12), so the hook never ran to completion.

PreToolUse treats exit code 2 as "block" and every other non-zero code as "the hook
itself errored" — the tool call then proceeds. The crash exited 1, so writes to
`data/gold/` were permitted. Combined with `skipDangerousModePermissionPrompt: true`,
this meant the gold set had no enforced protection at all: the only remaining barrier
was the prose rule in CLAUDE.md, which is an instruction, not a mechanism.

Verified `git log -- data/gold/` afterwards: every commit is a human commit. No
programmatic write occurred while the guard was down.

Fixes:
- `from __future__ import annotations` — defers annotation evaluation, so the hook
  runs on any 3.9+ interpreter. Chosen over pinning a newer Python because the second
  machine will run 3.13; the guard must not depend on which interpreter is on PATH.
- fail-closed: an unexpected exception must exit 2, not 1. A broken guard should stop
  work, not wave it through.
- subprocess tests asserting exit codes in both directions (block AND allow), so CI
  catches the regression instead of a manual spot-check months later.

Wider lesson: a guard that is present, committed, and reviewed can still be inert.
Existence is not enforcement — only an executed test proves it. The hook was written
in Phase 0 and had never been exercised.

Caveat on the tests above: they invoke the hook via `sys.executable`, i.e. the
interpreter running pytest (3.11 in `.venv`). Claude Code invokes it via `python3`
from PATH, which on this machine is conda 3.9. So these tests would NOT have caught
the original failure — they assert the exit-code contract, not interpreter
compatibility. `from __future__ import annotations` is what closes that gap, and it
is why pinning a newer Python would have been the wrong fix.
