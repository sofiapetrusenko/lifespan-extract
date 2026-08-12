# Schema & design log

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
