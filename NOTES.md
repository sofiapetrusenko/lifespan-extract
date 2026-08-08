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

### Blocked — two guidelines reference fields that do not exist in v0.2.0

Recorded as written, but not yet actionable:

- **`notes` does not exist.** Two guidelines above route content to it (unreported cohorts, speculative mechanisms). There is no `notes` field at either paper or experiment level, and every object is `additionalProperties: false`, so adding the key to a gold file makes it fail validation. Needs ratification: per-experiment `notes` (nullable string, flat — it is commentary, not an extracted claim) is the smaller change; paper-level would not fit the "unreported cohort" case, which is experiment-scoped.
- **`proposed_mechanism` does not exist; the field is named `mechanism`.** Its description already says "Proposed mechanism as stated by the authors, not inferred", so the guideline matches the existing field's intent exactly. Either rename `mechanism` → `proposed_mechanism` (breaking, but no gold file uses it yet) or keep `mechanism` and treat the guideline as describing it. Not renamed unilaterally.

### Version drift to watch

- `scripts/validate_gold.py` hardcodes the version in its success message (`All N file(s) valid against schema v0.2.0`). The schema has no machine-readable version of its own — only the `schema_version` *instance* field — so this string must be bumped by hand on every schema release. Worth adding a root-level version keyword to the schema file if this drifts even once.
- `data/gold/harrison2009.json` declares `schema_version: "0.1.0"`. Not updated here: `data/gold/` is human-only per CLAUDE.md. It still validates against v0.2.0 (the change is additive), but the human should bump it when filling the file.
