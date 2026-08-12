# Classifier evaluation set

PLAN.md Phase 3 scores the classifier on precision and recall over a labeled
positive/negative set. This directory is the **negative** half.

- **Positives** — the 10 hand-labeled papers in `data/gold/`. Every one is a
  lifespan-intervention study in a covered organism.
- **Negatives** — `negatives.json`. Papers that a naive keyword classifier
  would call positive, and that are not.

## Why these are *hard* negatives

A negative set drawn at random from PubMed is trivially separable: a classifier
that keys on "lifespan" and "mice" scores near-perfect precision against a
control group of cardiology papers, and the number tells you nothing about
whether it can do the job. Precision only means something when the negatives are
the cases the classifier is actually at risk of getting wrong.

So every entry here shares surface features with the positives — the vocabulary,
often the organism, sometimes a real intervention — and fails on exactly one
thing. Each entry names which.

## Categories

| category | fails because |
|---|---|
| `aging-no-lifespan` | Aging is studied; lifespan is never measured. Biomarkers, biological-age clocks, healthspan endpoints. |
| `lifespan-no-intervention` | Lifespan *is* measured, but nothing is administered. Observational cohorts, GWAS/QTL, natural variation. |
| `lifespan-adjacent-outcome` | A real intervention on something lifespan-shaped that is not an organism's lifespan. Cellular senescence, replicative lifespan of cell lines, organoids. |
| `review-or-meta-analysis` | The right words in the right order, and no experiment of its own. |
| `wrong-organism` | Heavy lifespan vocabulary in an organism the project does not cover, or where "lifespan" denotes something else entirely. |

Each category stresses a different failure mode, and the boundary each one
probes is a different one:

- `aging-no-lifespan` asks whether the classifier requires a **lifespan outcome**
  rather than a topic.
- `lifespan-no-intervention` asks whether it requires an **intervention** rather
  than an association.
- `lifespan-adjacent-outcome` asks whether it distinguishes the lifespan of an
  **organism** from the lifespan of a cell.
- `review-or-meta-analysis` asks whether it distinguishes **doing** an experiment
  from **describing** one.
- `wrong-organism` asks whether it reads "lifespan" **in context**.

## Metrics

**The headline is aggregate precision and recall over all 25 papers** — 10
positives from `data/gold/`, 15 negatives from here.

The per-category breakdown is reported **alongside, as diagnostics only**. With
n=3 per category, a per-category rate is not a measurement: one entry moving
swings it by 33 points, and quoting "67% on reviews" would give a number that
looks like a metric and carries the precision of a coin flip. What the breakdown
is for is naming *which* boundary the classifier failed to hold — a run that
misses all three reviews and nothing else is a different problem from one that
misses three scattered entries, and only the breakdown distinguishes them.

**The 15:10 ratio stands, and is not rebalanced.** Precision and recall are
properties of a threshold and its errors, not of set composition. The set is
what the search found when the criterion was "hard", and adjusting the ratio to
make the resulting number look better would be tuning the measuring instrument
rather than the thing being measured.

## Limitations

**The positives are the extraction gold set.** The same 10 papers serve as the
classifier's positive class and as the ground truth for per-field extraction
accuracy, so the two sets of Phase 3 numbers are computed over a shared sample
and are not independent evidence about the pipeline.

**The classifier prompt will be iterated against the set it is scored on.**
There is no held-out split. Every prompt revision is chosen partly by how it
scores here, which means the reported precision and recall are optimistic by an
unknown amount — the same overfitting exposure the Phase 3 prompt loop carries
for extraction, in a second place. Closing it needs papers this project has not
labelled, which is a Phase 3 decision and not a fix that can be applied to this
file.

**A paper listed twice under different identifiers is not detected.** Duplicate
detection keys on whichever of `pmid`/`doi` an entry uses, so the same work
entered once by PMID and once by DOI would pass. Nothing in the file records the
mapping between the two. Matters only if preprint negatives are added alongside
their published versions.

## These are not gold records

No experiment is extracted, no schema record exists, and nothing here is ever
written to `data/gold/`. An entry carries a binary label (`negative`), a
category, a one-line reason, and enough identity to fetch the abstract again.
`negatives.json` is validated by `scripts/validate_classifier_set.py`, not by
`schema/experiment.schema.json`.

## Human review gates inclusion

Every entry carries `"reviewed": false` until a human has read the abstract and
agreed. The validator prints the unreviewed count on every run, and an entry
that has not been reviewed **does not count toward the eval**. The candidates
were assembled by searching PubMed and reading each abstract, but "an agent read
it and thought so" is not the standard for a set whose whole purpose is to
measure whether the extraction pipeline can be trusted.

## Usage

```bash
# structure, vocabulary, uniqueness — offline, runs in CI
python scripts/validate_classifier_set.py

# also confirm every PMID resolves and its title still matches — network, opt-in
python scripts/validate_classifier_set.py --resolve
```
