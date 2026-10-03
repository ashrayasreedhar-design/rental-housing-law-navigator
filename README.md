# Rental Housing Law Navigator

For any apartment address, answer: **which housing rules apply here on a given date, what is the source, and what is about to change?**

Built for the MIT AI Hackathon challenge (RealPage discussion draft, October 2026). Reads a corpus of real state and city law, turns each rule into a structured record, resolves each address to its legal jurisdiction stack, and reports how supplied law-change cases affect the answer.

> **Not legal advice.** This is a prototype that summarizes public law for informational purposes. It is not a compliance certification and must not be relied on for legal decisions. See [`docs/compliance.md`](docs/compliance.md).

---

## What it does

| Module | Purpose |
|---|---|
| **A — Rule extraction** | Dual-model pipeline (Claude + OpenAI) reads every corpus document and emits one JSON record per rule, conforming to `schema/rule_record.schema.json`. Where the two extractors disagree, confidence drops and `conflict_flag` is set. |
| **B — Address lookup** | Geocodes an address, builds its state/county/city jurisdiction stack, and returns every applicable rule with a plain-language explanation and citation. Says `unknown` rather than guessing when coverage depends on a fact the data lacks. |
| **C — Change tracking** | Runs change cases T1–T5, listing affected addresses and before/after rule sets, supporting an "as of date" query. |

**Default query date: 2026-10-01.**

## Quick start

```bash
make setup                 # install dependencies
make clean-data            # clean + quality-flag the 500 addresses
make probe                 # dry-run the fetcher (robots + config check, no requests)
make reproduce             # full pipeline end to end
```

Override the query date anywhere:

```bash
make lookups AS_OF=2027-07-02
```

### API keys

Extraction needs two keys. Set them as environment variables — **never commit them**:

```bash
export ANTHROPIC_API_KEY=...
export OPENAI_API_KEY=...
```

On Replit, add these under **Tools → Secrets**.

### Individual steps

```bash
make fetch      # retrieve allowlisted sources            [network]
make clean-data # clean + quality-flag addresses
make geocode    # resolve 500 addresses to jurisdictions   [network]
make signals    # extract T1-T5 change signals             [API keys]
make extract    # dual-model rule extraction               [API keys]
make validate   # schema + verbatim-quote + coverage
make lookups    # Module B
make changes    # Module C: T1-T5
make audit      # data-quality red flags
make serve      # FastAPI backend
```

---

## How we handle evidence

Every rule carries the **tier of evidence** behind it. Tier drives confidence, and weak evidence is structurally prevented from reaching the "applies today" list.

| Tier | Source | Confidence | Can originate a rule? |
|---|---|---|---|
| 1 | Organizer-captured operative text | 0.90 | yes |
| 2 | Organizer-captured official restatement | 0.70 | yes |
| 3 | Code publisher / official, retrieved by us | 0.60 | yes — review required |
| 4 | Secondary (law firm / news / mirror), last resort | 0.35 | yes — quarantined |
| 5 | No retrievable text | — | **no** |

**Tier 5 is the point.** The schema requires a `quoted_span`. No text means no honest quote, which means **we do not invent a rule** — the lookup returns `unknown` naming the missing source.

### The verbatim-quote check

Every `quoted_span` must appear character-for-character (whitespace-normalized) in its cited source document. Any record that fails is rejected before it reaches `rules.json`. This is the primary defense against hallucinated citations, and it applies to output from both extractors.

```
exact match       : True
across linebreak  : True     # handles PDF line wrapping
invented quote    : False    # rejects fabrication
```

### Source retrieval

`config/sources.yml` is an **explicit allowlist** — no crawling, no link discovery. robots.txt is honored, requests are rate limited per domain, and every retrieved document is written with a header marking it team-retrieved rather than organizer-captured.

Two registries with deliberately different powers:

- **authority** — code publishers and official pages. May originate rules.
- **signal** — secondary sources. May *only* attach corroboration or contradiction to rules that already exist. Drives `conflict_flag` and effective-date discrepancy detection.

`analyst_leads` records facts supplied by a human operator during the build. These are never citations — they become fetch assertions and cross-checks, and a mismatch between an analyst's belief and retrieved text is surfaced as a conflict.

---

## Jurisdiction-agnostic by construction

Adding a jurisdiction means adding **data and config**, never code:

1. Add rows to `corpus/corpus_manifest.csv` and text to `corpus/text/`
2. Add any retrieval targets to `config/sources.yml`
3. Add precedence, ZIP prefixes and cutoffs to `config/jurisdictions.yml`

No module hardcodes a city, state, or statute. Change-test scoping derives its jurisdiction list from the corpus manifest at runtime.

---

## Data quality

The supplied data has real defects and deliberate gaps. We **flag, never silently repair**.

Found by auditing all 500 supplied addresses:

| Flag | Count | Handling |
|---|---|---|
| `units_missing` | 242 | coverage turning on unit count → `unknown` |
| `year_built_missing` | 212 | coverage turning on build year → `unknown` |
| `zip_missing` | 130 | geocode on street + city + state |
| `postal_city_alias` | 38 | hint only; geocoder decides legal city |
| `zip_state_conflict` | 27 | ZIP withheld from geocoding, **not corrected** |
| `co_cutoff_boundary` | 2 | year on the cutoff → `unknown` |

The 27 ZIP conflicts are genuine errors in the source data (NJ rows carrying New York, Texas and Connecticut ZIPs), and 16 of them fall on Hoboken and Jersey City rows — exactly the population T2 tests. We withhold the bad ZIP and geocode on street + city + state rather than invent a correction.

Only 23 of the 60 Boston-area addresses literally say "Boston"; the rest carry 9 neighbourhood mailing names. Filtering on `postal_city` would drop 37 addresses from the Massachusetts change tests. The legal city comes from the Census incorporated place, never from a name match.

### Captured-text coverage by jurisdiction

```
Boston, MA         5/5    Los Angeles, CA   5/7     Santa Ana, CA    2/4
San Francisco, CA  6/6    MA               11/15    San Diego, CA    2/5
Berkeley, CA       8/9    CA                7/14    Jersey City, NJ  1/3
Cambridge, MA      2/3    NJ                5/10    Hoboken, NJ      0/3
                                                    Newark, NJ       0/3
```

- **Hoboken and Newark** have zero captured text; all six sources are code-publisher links.
- **Jersey City's** algorithmic ban is not in the captured corpus at all.
- **San Francisco's** Rent Ordinance (Admin Code ch. 37) is absent — the six captured SF documents are official summaries.
- **NJ statutes** 2A:18-61.1 and 46:8-21.2 are link-only; the official NJ DCA *Truth in Renting* handbook (D067) quotes both and serves as Tier 2 authority.
- **Santa Ana** has rules but no sample addresses — extraction only, by design.

---

## Repository layout

```
config/          sources.yml (allowlist + provenance policy), jurisdictions.yml
corpus/          corpus_manifest.csv, links_only.csv, text/ (54 captured), text_team/ (retrieved)
data/            sample_addresses.csv (500 properties)
schema/          rule_record.schema.json, sample_rule_record.json
dev/             change_tests.json (T1-T5)
src/rhln/        corpus, clean, fetch, geocode, signals, extract, validate, lookup, changes, audit, api
submission/      rules.json, lookups.json, changes.json (generated)
out/             fetch_report.json, addresses_clean.json, geocode_cache.json, change_signals.json
docs/            participant_guide.md, compliance.md
```

## Change tests

The supplied `dev/change_tests.json` contains **five** deterministic tests, T1–T5:

| Test | What it checks |
|---|---|
| T1 | California AB 325 / SB 763: as of 2025-12-31 vs 2026-01-02 |
| T2 | Hoboken vs Jersey City local algorithmic bans: boundary correctness |
| T3 | NJ FAIR Act: enacted 2026-07-20, effective 2027-07-01; possible preemption |
| T4 | Massachusetts S.2983 and H.5222: pending bills |
| T5 | Massachusetts rent-control ballot question struck: affected set must be empty |

The T3 effective date is derived from the statute's own words — *"the first day of the twelfth month next following the date of enactment"* — combined with the approval date, not hardcoded.

## License & data

Code: MIT. Corpus documents remain under the terms of their original publishers; `corpus_manifest.csv` records source URL and retrieval date for each.
