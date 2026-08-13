# lifespan-extract — Project Plan

Structured extraction of lifespan-intervention experiments from the longevity literature, with a measured evaluation harness and a research-grade browsing UI.

**Problem.** Thousands of intervention experiments are locked in PDF prose. Manually curated databases (DrugAge, GenAge) lag the literature by years. This tool ingests papers, extracts structured experiment records with an LLM, measures extraction accuracy against a hand-labeled gold set, and serves the results through an API and a UI a scientist would actually enjoy using.

**Scope (MVP).** Organisms: *C. elegans*, *M. musculus*, and *M. mulatta* (rhesus macaque) only. Sources: PubMed abstracts + PMC open-access full text + bioRxiv. No fine-tuning; prompt + schema + evals.

---

## Architecture

```
ingest/        PubMed E-utilities + bioRxiv clients, dedup by DOI
extract/       classify (cheap model) -> extract (structured output), JSON repair + retry
evals/         gold-set runner, per-field accuracy, DrugAge cross-validation
api/           FastAPI: /experiments (filters), /interventions/{agent} (aggregate)
web/           Next.js dashboard: filterable table, intervention pages, CSV export
schema/        JSON Schema for experiment records (source of truth)
data/gold/     hand-labeled gold set — human-controlled (see working agreements)
data/drafts/   in-progress labels, gitignored; promoted into data/gold/ by hand
data/extracted/
               pipeline output, gitignored; one file per paper, written by
               `python -m extract` into <schema-version>/ — Phase 3's eval input
```

**Stack:** Python 3.11, FastAPI, SQLModel, PostgreSQL; Next.js + TypeScript; pytest; GitHub Actions.

**Key invariants:**
- Every extracted field carries `source_quote` (verbatim sentence from the paper) and `confidence`.
- Absent data is `not_reported`, never guessed. This is tested.
- Model output is untrusted input: JSON repair heuristic + one retry; unrepairable payloads raise with a windowed excerpt.
- Extraction is idempotent per (paper, schema_version); re-runs never silently overwrite gold data.

---

## Phase 0 — Foundation (week 1)

- [ ] Repo, README (problem / approach / status), MIT license
- [ ] `schema/experiment.schema.json` v0
- [ ] `data/gold/`: 10 hand-labeled papers (see gold-set design below)
- [ ] `NOTES.md`: running log of schema changes and why — feeds the final README's Design Decisions section

**Gold set covers hard cases by design:** two consistency pairs (Harrison 2009 / Miller 2011 rapamycin; Colman 2009 / Mattison 2012 CR-in-macaques with opposite conclusions), one multi-organism paper (Eisenberg 2009 spermidine), one sex-specific effect (Strong 2016 17-α-estradiol), one small-effect paper (Martin-Montalvo 2013 metformin), one recent preprint.

**Schema is at v0.4.0.** `experiments[].species` (v0.4.0) carries the actual species as free text when `organism` — a closed enum sized for the MVP filters — can only say `other`. Without it a multi-organism paper's yeast and fly records differ in no validated field. `organism` was deliberately *not* extended: the enum is the filter vocabulary, and widening it would put organisms out of MVP scope into the API's aggregates.

**Done when:** 10 valid JSON files pass schema validation; schema has survived contact with real papers.

## Phase 1 — Ingestion (week 2, first half)

- [ ] `ingest/pubmed.py`: `fetch_abstracts(query, limit) -> list[RawPaper]`, retry with backoff on 429
- [ ] `ingest/biorxiv.py`: same contract
- [ ] Dedup preprint/publication by DOI; store raw abstracts in Postgres
- [ ] CLI: `python -m ingest --query "autophagy lifespan" --limit 100`

**Done when:** 100+ raw papers in DB from one command; re-running does not duplicate.

## Phase 2 — Classification + Extraction (weeks 2–3)

- [ ] `extract/classify.py`: cheap-model gate — "does this paper report lifespan-intervention data?" (cost cascade)
- [ ] `extract/extract.py`: structured output per schema; one experiment record per (organism, intervention) — multi-organism papers yield multiple records
- [ ] JSON repair + single retry; failures raise loudly
- [ ] Every field: `source_quote`, `confidence`, `extracted_from: abstract | full_text`

**Done when:** pipeline runs end-to-end on 20 unseen papers without manual intervention; failures are loud and logged.

## Phase 3 — Evals (week 3) — the core of the project

- [ ] `evals/run.py`: run pipeline on gold set, per-field accuracy table (exact match for categoricals, tolerance for numerics)
- [ ] Classifier precision/recall on a labeled positive/negative set
- [ ] Iterate prompt against metrics until plateau; log each iteration's scores in `evals/history.md`
- [ ] DrugAge cross-validation: run on 20–30 papers already curated in DrugAge, report agreement rate

**Done when:** README can state "X% per-field accuracy on a N-paper hand-labeled gold set; reproduces Y% of DrugAge entries." Numbers are honest, methodology documented.

**Fields expected to be hardest (watch these):** median vs max lifespan confusion; `sex` buried in methods (must yield `not_reported` from abstract-only); `no_effect` phrased evasively ("did not significantly alter") being coerced into weak increase.

## Phase 4 — API + UI (week 4)

API:
- [ ] `GET /experiments?organism=&intervention_type=&mechanism=&direction=` — filterable list
- [ ] `GET /interventions/{agent}` — aggregate: all studies, effect range, organisms tested
- [ ] CSV export endpoint

UI (design matters — target user is a scientist):
- [ ] Dense, information-rich table; instant sidebar filters, no Apply button
- [ ] Click any value → expand `source_quote` with the sentence highlighted (the trust feature)
- [ ] Intervention page: effect sizes across studies as a small chart
- [ ] Calm, colorblind-safe encoding for effect direction; aesthetic register of a serious research tool (UniProt/Ensembl/Linear, not a SaaS landing page)
- [ ] CSV export button — scientists pull everything into R

**Done when:** a scientist can answer "which pharmacological interventions increase lifespan in mice via autophagy?" in under 30 seconds, and verify any value against its source quote in one click.

## Phase 5 — Polish (week 5)

- [ ] pytest: schema validation, JSON repair, dedup, eval metrics on fixtures, `not_reported` honesty test
- [ ] GitHub Actions: lint (ruff) + tests on every push
- [ ] Deploy: API on DigitalOcean, frontend on Vercel
- [ ] README: problem, architecture diagram, eval numbers, screenshots, Design Decisions (from NOTES.md), honest Limitations section
- [ ] Stretch: contradiction detection — group by (intervention, organism), flag opposite effect directions as "conflicting evidence"

**Done when:** a stranger can clone, run, and understand the project from the README alone; CI is green; the deployed demo is live.

---

## Working agreements (for Claude Code sessions)

- One phase = one session = one PR. Do not run ahead.
- `data/gold/` is human-controlled ground truth. Claude Code never writes to it. The
  one permitted programmatic write is `scripts/check_gold.py --promote`, run by the
  human, and only on a draft that passes every check — schema, verbatim quotes,
  cross-file consistency. It refuses on any failure, on an unverified quote, and on
  an existing target; it never overwrites a record.
- Evals are written with the human driving; Claude Code assists, not the reverse.
- Prefer loud failure over silent fallback everywhere.
- Small commits, written by the human, in English.
