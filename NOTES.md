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

- **Error reporting must surface ALL validator errors, never `errors[0]`.** When a claim wrapper's `value` violates its enum, `jsonschema` emits *two* errors: a deep, correct one (`experiments[0].organism.value: 'M. mulatta' is not one of [...]`) and a shallow, misleading one (`experiments[0].organism: Unevaluated properties are not allowed ('value' was unexpected)`). This is per spec — a failed subschema produces no annotation, so `value` counts as unevaluated — and it is not a schema bug to fix. Reporting only the first error would make the "loud failure with a windowed excerpt" rule emit confident nonsense. `scripts/validate_gold.py` already does this correctly: it prints every error, sorted deepest-path-first so the real diagnosis leads. Phase 2's extraction error path must match that behaviour. Nothing to implement before Phase 2.

### Known limitations — accepted for MVP

- **Per-source full-text coverage is not answerable from records.** PMC open-access full text is encoded as `source: "pubmed"` + `extracted_from: "full_text"`, so a record cannot distinguish "PubMed abstract only" from "retrieved via PMC". Any Phase 3 statistic of the form "N% of bioRxiv records used full text" is computable, but "N% of records came from PMC" is not. Accepted: PMC is a retrieval detail, and `extracted_from` already captures the part that affects extraction quality. Revisit only if full-text provenance becomes a reported eval dimension.

### Tooling

- `scripts/validate_gold.py` validates every `data/gold/*.json` against the schema, prints all errors per file with dotted paths (`experiments[0].lifespan_effect.direction`), and exits 1 on any failure. Read-only with respect to `data/gold/`. An empty gold directory prints "nothing to validate" and exits 0 — legitimate during Phase 0, and stated out loud so an empty glob never resembles a clean run.
- `.github/workflows/ci.yml` still does this validation with an inline heredoc, which now duplicates the script. Replace that step with `python scripts/validate_gold.py` so local and CI checks cannot drift apart.

### Resolved — do not reopen

- **Preprints and DOIs**: bioRxiv assigns DOIs under the `10.1101/...` prefix, so a preprint is never DOI-less. `paper.doi` stays required. Covered by a validation case.
- **Per-experiment identity**: settled by decision 9 above.
