# Compliance & Responsible-AI Note

## Not legal advice

This system summarizes publicly available law for informational purposes. It is
**not legal advice and not a compliance certification**. No output should be
relied on for a legal or business decision. Every interface surfaces this
notice. The system never suggests ways to avoid or circumvent a rule.

## Provenance

Every rule record carries:

- `source_url` — the document actually read
- `source_doc_id` — manifest identifier
- `quoted_span` — text copied verbatim from that document
- `citation` — the official cite
- `confidence` — derived from evidence tier and cross-model agreement
- retrieval date, from the document's own header

**The verbatim gate**: a `quoted_span` that does not appear character-for-character
(whitespace-normalized) in its cited source is rejected before it reaches
`rules.json`. This is the primary defense against hallucinated citations, and it
applies to model output from both extractors.

## Evidence tiers

Rules are never presented as equally well-founded. See `config/sources.yml`.

| Tier | Source | Confidence | Review |
|---|---|---|---|
| 1 | Organizer-captured operative text | 0.90 | not required |
| 2 | Organizer-captured official restatement | 0.70 | not required |
| 3 | Code publisher / official, retrieved by us | 0.60 | **required** |
| 4 | Secondary (law firm / news / mirror), last resort | 0.35 | **required**, quarantined |
| 5 | No retrievable text | — | **no rule created** |

Tier 5 is deliberate: no text means no honest quote, so no rule is invented. The
lookup returns `unknown` naming the missing source.

## Human review

Any rule that is pending, newly enacted, team-retrieved, or secondary-attested is
flagged `requires_human_review` and **excluded from the "Rules that apply today"
list until a reviewer approves it**. Review status is tracked per rule
(`not_required` / `requires_human_review` / `approved` / `rejected`) with
reviewer identity and timestamp.

The "applies today" gate requires all of: status `in_force` as of the query date,
review status `not_required` or `approved`, tier ≤ 3, and a verified quoted span.

## Analyst leads

Facts supplied by a human operator during the build are recorded under
`analyst_leads` in `config/sources.yml`. They are **never** citations. They serve
only as fetch assertions and cross-checks; a mismatch between an analyst's belief
and retrieved text is surfaced as a conflict for review rather than silently
resolved in either direction.

## Data-quality red flags

Logged and surfaced in the UI, not buried:

- cross-extractor disagreement (`conflict_flag` + `conflict_note`)
- model-vs-deterministic date disagreement (treated as high severity)
- missing or unverifiable citations
- stale sources (retrieval older than the configured window)
- low confidence
- per-jurisdiction coverage gaps, including jurisdictions with zero captured text
- unresolved conflicting effective dates reported by different publishers
- source-data defects: ZIP/state contradictions, missing coverage facts,
  mailing-city vs legal-city divergence

## Uncertainty

`unknown` is a first-class answer. Where coverage depends on a fact the data does
not contain — owner type, certificate-of-occupancy date, unit count — the system
says `unknown` and explains which fact is missing. It does not guess, and it does
not fall back to a default.

Defective input data is flagged, never silently repaired. A ZIP code inconsistent
with its state is withheld from geocoding rather than corrected, because the
correct value is not knowable from the data.

## Data handling

Public data only. No customer, resident, or pricing data. No owner names. Source
retrieval is allowlist-only with robots.txt honored and per-domain rate limiting;
there is no crawling or bulk scraping.
