"""Change-signal extraction for the T1-T5 change tests.

Reads EVERY available document -- organizer-captured corpus, team-retrieved
authority pages, and secondary signal sources -- and pulls out the facts the
change tests turn on:

    * legislative status (in_force / not_yet_effective / pending / failed)
    * enactment date and effective date (including relative formulations such
      as "the first day of the twelfth month next following enactment")
    * preemption language
    * court action that strikes or upholds a measure
    * which jurisdictions and categories a document speaks to

Three independent passes, deliberately:

    1. deterministic  -- regex over dates, citations, effective-date idioms.
       No model involved, so it cannot hallucinate, and it anchors the others.
    2. claude         -- primary LLM extraction.
    3. openai         -- independent second extraction over the same text.

Disagreement between passes 2 and 3 lowers confidence and raises
conflict_flag. Disagreement with pass 1 on a DATE is treated as serious,
because dates are exactly what T1 and T3 hinge on.

Every emitted signal carries a quoted_span that is verified verbatim against
its source document before it is written. Unverified signals are dropped and
counted, never silently kept.

Runs on Replit (needs ANTHROPIC_API_KEY and OPENAI_API_KEY).
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .corpus import Corpus, find_corpus_root

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "dev" / "change_tests.json"
TEAM_DIR = ROOT / "corpus" / "text_team"
OUT = ROOT / "out" / "change_signals.json"

STATUS_VALUES = ["in_force", "not_yet_effective", "pending", "failed"]


# ---------------------------------------------------------------------------
# Pass 1: deterministic extraction
# ---------------------------------------------------------------------------

MONTHS = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"

# Effective-date idioms. The NJ FAIR Act uses the relative form, and getting
# it wrong breaks T3, so it is matched explicitly rather than left to a model.
EFFECTIVE_IDIOMS = [
    (re.compile(r"shall take effect\s+immediately", re.I), "immediate"),
    (re.compile(r"shall take effect on the first day of the (\w+) month next following", re.I), "relative_month"),
    (re.compile(r"shall take effect\s+on\s+([^.;]{3,60})", re.I), "explicit"),
    (re.compile(r"effective\s+(?:date[:\s]+)?((?:" + MONTHS + r")\s+\d{1,2},\s*\d{4})", re.I), "explicit"),
    (re.compile(r"operative\s+on\s+([^.;]{3,60})", re.I), "explicit"),
]

ORDINAL_MONTHS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12,
}

ENACTED_PATTERNS = [
    re.compile(rf"approved\s+({MONTHS}\s+\d{{1,2}},\s*\d{{4}})", re.I),
    re.compile(rf"enacted\s+(?:on\s+)?({MONTHS}\s+\d{{1,2}},\s*\d{{4}})", re.I),
    re.compile(rf"signed\s+(?:into law\s+)?(?:on\s+)?({MONTHS}\s+\d{{1,2}},\s*\d{{4}})", re.I),
]

PREEMPTION_PATTERNS = [
    re.compile(r"municipalit\w+\s+shall\s+be\s+prohibited\s+from\s+enacting", re.I),
    re.compile(r"preempt\w*", re.I),
    re.compile(r"supersed\w+", re.I),
]

COURT_ACTION_PATTERNS = [
    (re.compile(r"\bstruck\s+(?:down|from)\b", re.I), "struck"),
    (re.compile(r"\binvalidat\w+", re.I), "struck"),
    (re.compile(r"\bruled?\s+unconstitutional\b", re.I), "struck"),
    (re.compile(r"\bupheld\b", re.I), "upheld"),
    (re.compile(r"\bremoved\s+from\s+the\s+ballot\b", re.I), "struck"),
]

PENDING_PATTERNS = [
    (re.compile(r"\breferred\s+to\s+(?:the\s+)?committee\b", re.I), "pending"),
    (re.compile(r"\bbill\s+(?:no\.|number)?\s*[SH]\.?\s*\d+", re.I), "pending"),
    (re.compile(r"\bproposed\s+(?:bill|ordinance|measure)\b", re.I), "pending"),
]

CITATION_PATTERNS = [
    re.compile(r"\bN\.J\.S\.A\.\s*[\d:A-Za-z.\-]+"),
    re.compile(r"\bCal\.\s*(?:Civ|Gov)\.\s*Code\s*\u00a7+\s*[\d.]+"),
    re.compile(r"\bP\.L\.\s*\d{4},\s*c\.\s*\d+", re.I),
    re.compile(r"\bM\.G\.L\.\s*c\.\s*\d+[A-Za-z]?"),
    re.compile(r"\b\d{3}\s+CMR\s+[\d.]+"),
    re.compile(r"\u00a7+\s*[\d.\-]+"),
]


def _norm_date(raw: str) -> str | None:
    raw = raw.strip().rstrip(".,;")
    for fmt, out in (("%B %d, %Y", "%Y-%m-%d"), ("%Y-%m-%d", "%Y-%m-%d"), ("%B %Y", "%Y-%m")):
        try:
            return datetime.strptime(raw, fmt).strftime(out)
        except ValueError:
            continue
    return None


def _add_months(iso: str, months: int) -> str | None:
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    m = d.month - 1 + months
    return f"{d.year + m // 12:04d}-{m % 12 + 1:02d}-01"


def _window(text: str, start: int, end: int, pad: int = 170) -> str:
    a, b = max(0, start - pad), min(len(text), end + pad)
    return re.sub(r"\s+", " ", text[a:b]).strip()


def deterministic_pass(doc_id: str, raw_text: str) -> list[dict]:
    """Regex findings. Cannot hallucinate; used to check the models.

    Patterns run against a whitespace-flattened copy of the document. Source
    documents come from PDFs and HTML where a statutory phrase routinely wraps
    across lines -- the NJ FAIR Act's effective-date clause breaks as
    "the first day of\nthe twelfth month", which defeats any pattern written
    with literal spaces. Quoted spans are taken from the flattened text, which
    is sound because the verbatim checker normalises whitespace on both sides.
    """
    text = re.sub(r"\s+", " ", raw_text)
    out: list[dict] = []

    def emit(kind: str, value, m, extra: dict | None = None):
        rec = {"doc_id": doc_id, "pass": "deterministic", "kind": kind, "value": value,
               "quoted_span": _window(text, m.start(), m.end())}
        if extra:
            rec.update(extra)
        out.append(rec)

    enacted_iso = None
    for pat in ENACTED_PATTERNS:
        for m in pat.finditer(text):
            iso = _norm_date(m.group(1))
            if iso:
                enacted_iso = enacted_iso or iso
                emit("enacted_date", iso, m)

    # A relative clause is also matched by the generic "shall take effect on"
    # pattern, producing an unresolvable duplicate. Record which spans a
    # higher-priority idiom claimed and skip overlaps.
    claimed: list[tuple[int, int]] = []
    for pat, idiom in EFFECTIVE_IDIOMS:
        for m in pat.finditer(text):
            if idiom == "explicit" and any(a <= m.start() < b for a, b in claimed):
                continue
            if idiom in ("relative_month", "immediate"):
                claimed.append((m.start(), m.end() + 90))
            if idiom == "immediate":
                emit("effective_date", {"type": "immediate_on_enactment"}, m, {"idiom": idiom})
            elif idiom == "relative_month":
                n = ORDINAL_MONTHS.get(m.group(1).lower())
                resolved = _add_months(enacted_iso, n) if (n and enacted_iso) else None
                emit("effective_date",
                     {"type": "relative", "months_after_enactment": n,
                      "enacted_date": enacted_iso, "resolved": resolved},
                     m, {"idiom": idiom})
            else:
                raw = m.group(1) if m.groups() else ""
                emit("effective_date",
                     {"type": "explicit", "raw": raw.strip(), "resolved": _norm_date(raw)},
                     m, {"idiom": idiom})

    for pat in PREEMPTION_PATTERNS:
        for m in pat.finditer(text):
            emit("preemption", True, m)
            break

    # Court action requires a nearby legal-measure subject. A bare "upheld"
    # in an unrelated tenant handbook is not evidence that a ballot question
    # was struck -- that false positive appeared on the first real run.
    for pat, action in COURT_ACTION_PATTERNS:
        for m in pat.finditer(text):
            ctx = text[max(0, m.start() - 320): m.end() + 320].lower()
            if any(k in ctx for k in ("ballot", "question", "initiative", "petition",
                                      "ordinance", "statute", "measure", "court",
                                      "supreme judicial", "sjc", "unconstitutional")):
                emit("court_action", action, m)
                break

    for pat, status in PENDING_PATTERNS:
        for m in pat.finditer(text):
            emit("status_hint", status, m)
            break

    seen = set()
    for pat in CITATION_PATTERNS:
        for m in pat.finditer(text):
            c = re.sub(r"\s+", " ", m.group(0)).strip()
            if len(c) > 3 and c not in seen:
                seen.add(c)
                emit("citation", c, m)
            if len(seen) >= 25:
                break
    return out


# ---------------------------------------------------------------------------
# Passes 2 & 3: model extraction
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM = """You extract change-tracking facts from US rental housing law documents.

You will be given one document. Return ONLY facts the document actually states.

Rules you must follow:
- Every finding MUST include `quoted_span`: text copied EXACTLY from the document, 20-300 characters, verbatim. Never paraphrase inside quoted_span. Never invent.
- If the document does not state a fact, omit it. Do not infer, do not fill gaps from prior knowledge.
- For effective dates expressed relatively (e.g. "the first day of the twelfth month next following the date of enactment"), report the relative form AND the enactment date if stated. Do not compute the result yourself.
- A news report that a body "approved" a measure establishes ENACTMENT only. It does not establish operative text, coverage, exemptions, or an effective date.
- Distinguish carefully: enacted-and-in-force vs enacted-but-not-yet-effective vs pending bill vs failed/struck.

Return JSON only, no prose:
{
  "jurisdiction": "<state code or 'City, ST' or null>",
  "categories": ["rent_increase_limits"|"just_cause_eviction"|"security_deposits"|"application_screening_fees"|"screening_restrictions"|"algorithmic_rent_setting"],
  "findings": [
    {
      "kind": "status"|"enacted_date"|"effective_date"|"preemption"|"court_action"|"citation"|"penalty"|"coverage_condition",
      "value": "<concise value; for status use exactly one of in_force|not_yet_effective|pending|failed>",
      "quoted_span": "<verbatim from document>",
      "confidence": 0.0-1.0
    }
  ]
}"""


def _user_prompt(doc_id: str, jurisdiction: str, text: str, max_chars: int) -> str:
    body = text[:max_chars]
    truncated = "\n\n[TRUNCATED]" if len(text) > max_chars else ""
    return (f"doc_id: {doc_id}\nmanifest jurisdiction: {jurisdiction}\n\n"
            f"--- DOCUMENT ---\n{body}{truncated}\n--- END ---\n\n"
            "Extract change-tracking facts as JSON.")


def _parse_json(raw: str) -> dict | None:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def claude_pass(doc_id: str, jurisdiction: str, text: str, model: str, max_chars: int) -> dict | None:
    try:
        import anthropic
    except ImportError:
        return None
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    client = anthropic.Anthropic(api_key=key)
    try:
        resp = client.messages.create(
            model=model, max_tokens=4000, temperature=0,
            system=EXTRACTION_SYSTEM,
            messages=[{"role": "user", "content": _user_prompt(doc_id, jurisdiction, text, max_chars)}],
        )
        return _parse_json("".join(b.text for b in resp.content if b.type == "text"))
    except Exception as exc:
        print(f"    claude error on {doc_id}: {exc.__class__.__name__}: {exc}")
        return None


def openai_pass(doc_id: str, jurisdiction: str, text: str, model: str, max_chars: int) -> dict | None:
    try:
        from openai import OpenAI
    except ImportError:
        return None
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    client = OpenAI(api_key=key)
    try:
        resp = client.chat.completions.create(
            model=model, temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": EXTRACTION_SYSTEM},
                      {"role": "user", "content": _user_prompt(doc_id, jurisdiction, text, max_chars)}],
        )
        return _parse_json(resp.choices[0].message.content or "")
    except Exception as exc:
        print(f"    openai error on {doc_id}: {exc.__class__.__name__}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def _date_values(findings: list[dict], kind: str) -> set[str]:
    out = set()
    for f in findings:
        if f.get("kind") != kind:
            continue
        v = f.get("value")
        if isinstance(v, dict):
            v = v.get("resolved") or v.get("raw") or v.get("type")
        if isinstance(v, str):
            m = re.search(r"\d{4}-\d{2}(?:-\d{2})?", v)
            out.add(m.group(0) if m else v.strip().lower())
    return {v for v in out if v}


def reconcile(doc_id: str, det: list[dict], claude: dict | None, oai: dict | None,
              source_text: str) -> dict:
    """Merge three passes; disagreement lowers confidence and raises conflicts."""
    conflicts: list[dict] = []
    c_find = (claude or {}).get("findings", []) or []
    o_find = (oai or {}).get("findings", []) or []

    # -- verbatim gate: a model finding whose quote is not in the document is dropped
    def verified(findings: list[dict], who: str) -> list[dict]:
        keep, dropped = [], 0
        norm = lambda s: re.sub(r"\s+", " ", s or "").strip().lower()
        hay = norm(source_text)
        for f in findings:
            q = f.get("quoted_span") or ""
            if len(q) >= 20 and norm(q) in hay:
                f["pass"] = who
                f["quote_verified"] = True
                keep.append(f)
            else:
                dropped += 1
        if dropped:
            conflicts.append({"type": "unverified_quote", "pass": who, "dropped": dropped,
                              "note": "finding discarded: quoted_span not found verbatim in source"})
        return keep

    c_ok, o_ok = verified(c_find, "claude"), verified(o_find, "openai")

    def statuses(fs):
        return {str(f.get("value")).strip().lower() for f in fs
                if f.get("kind") == "status" and str(f.get("value")).strip().lower() in STATUS_VALUES}

    cs, os_ = statuses(c_ok), statuses(o_ok)
    if cs and os_ and not (cs & os_):
        conflicts.append({"type": "status_disagreement", "claude": sorted(cs), "openai": sorted(os_)})

    for kind in ("effective_date", "enacted_date"):
        cd, od = _date_values(c_ok, kind), _date_values(o_ok, kind)
        dd = _date_values(det, kind)
        if cd and od and not (cd & od):
            conflicts.append({"type": f"{kind}_disagreement", "severity": "high",
                              "claude": sorted(cd), "openai": sorted(od), "deterministic": sorted(dd)})
        for who, vals in (("claude", cd), ("openai", od)):
            if dd and vals and not (dd & vals):
                conflicts.append({"type": f"{kind}_vs_deterministic", "severity": "high",
                                  "pass": who, "model": sorted(vals), "regex": sorted(dd)})

    conf = 0.9
    if not c_ok or not o_ok:
        conf -= 0.2
    for c in conflicts:
        conf -= 0.25 if c.get("severity") == "high" else 0.1
    conf = round(max(0.05, min(1.0, conf)), 2)

    juris = (claude or {}).get("jurisdiction") or (oai or {}).get("jurisdiction")
    cats = sorted({*(((claude or {}).get("categories")) or []), *(((oai or {}).get("categories")) or [])})

    return {
        "doc_id": doc_id,
        "jurisdiction": juris,
        "categories": cats,
        "findings": det + c_ok + o_ok,
        "counts": {"deterministic": len(det), "claude": len(c_ok), "openai": len(o_ok)},
        "models_agree": bool(c_ok and o_ok and not conflicts),
        "conflicts": conflicts,
        "conflict_flag": bool(conflicts),
        "confidence": conf,
    }


# ---------------------------------------------------------------------------
# Mapping signals onto the change tests
# ---------------------------------------------------------------------------

def infer_test_jurisdictions(test: dict, known: list[str]) -> tuple[set[str], set[str]]:
    """Work out which states and cities a change test concerns.

    `states` is used when present. Otherwise the test's own title and
    expected_behavior are matched against the jurisdictions that actually
    exist in the corpus manifest -- so a new jurisdiction needs no code
    change. T2 ("Hoboken vs Jersey City local algorithmic bans") carries no
    `states` key, and without this every document in the corpus matched it.
    """
    states = set(test.get("states") or [])
    cities: set[str] = set()
    blob = " ".join(str(test.get(k, "")) for k in ("title", "expected_behavior")).lower()

    for j in known:
        if "," in j:
            city, st = (x.strip() for x in j.split(",", 1))
            if city.lower() in blob:
                cities.add(j)
                states.add(st)
        elif re.search(rf"\b{re.escape(j)}\b", blob):
            states.add(j)
    return states, cities


def map_to_tests(signals: list[dict], tests: list[dict], corpus: Corpus) -> dict:
    """Attach documentary evidence to each change test."""
    by_doc = {s["doc_id"]: s for s in signals}
    out: dict[str, dict] = {}
    known = corpus.jurisdictions()

    for t in tests:
        tid = t["test_id"]
        states, cities = infer_test_jurisdictions(t, known)
        entry = {
            "test_id": tid,
            "title": t.get("title"),
            "type": t.get("type"),
            "rule_ids": t.get("rule_ids", []),
            "expected_behavior": t.get("expected_behavior"),
            "as_of": {k: t[k] for k in ("as_of", "as_of_before", "as_of_after") if k in t},
            "scope": {"states": sorted(states), "cities": sorted(cities),
                      "states_source": "declared" if t.get("states") else "inferred_from_title"},
            "conflict_with": t.get("conflict_with", []),
            "evidence": [],
            "evidence_gaps": [],
            "conflict_flags": [],
        }

        for doc_id, sig in by_doc.items():
            doc = corpus.get(doc_id)
            juris = (sig.get("jurisdiction") or (doc.jurisdiction if doc else "") or "")
            state = juris.split(",")[-1].strip() if "," in juris else juris.strip()
            if cities and juris in cities:
                pass                      # explicitly named city: always relevant
            elif states and state not in states:
                continue
            elif not states and not cities:
                continue                  # refuse to match everything

            useful = [f for f in sig["findings"]
                      if f.get("kind") in ("status", "effective_date", "enacted_date",
                                           "preemption", "court_action")]
            if not useful:
                continue

            entry["evidence"].append({
                "doc_id": doc_id,
                "jurisdiction": juris,
                "tier": sig.get("tier"),
                "confidence": sig.get("confidence"),
                "conflict_flag": sig.get("conflict_flag"),
                "findings": useful[:12],
            })
            if sig.get("conflict_flag"):
                entry["conflict_flags"].append({"doc_id": doc_id, "conflicts": sig["conflicts"]})

        if not entry["evidence"]:
            entry["evidence_gaps"].append(
                f"No document yielded change-relevant findings for {tid}. "
                f"Rules {entry['rule_ids']} cannot be evidenced; dependent lookups must return 'unknown'.")
        out[tid] = entry
    return out


# ---------------------------------------------------------------------------

def load_team_docs() -> dict[str, dict]:
    """Team-retrieved documents, with their tier taken from the file header."""
    out = {}
    if not TEAM_DIR.exists():
        return out
    for p in sorted(TEAM_DIR.glob("*.txt")):
        raw = p.read_text(encoding="utf-8", errors="replace")
        tier = "signal" if re.search(r"^TIER:\s*signal", raw, re.M) else "authority"
        out[p.stem] = {"text": raw, "tier": tier, "path": str(p.relative_to(ROOT))}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract T1-T5 change signals from all documents.")
    ap.add_argument("--claude-model", default=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5"))
    ap.add_argument("--openai-model", default=os.environ.get("OPENAI_MODEL", "gpt-4o"))
    ap.add_argument("--max-chars", type=int, default=120_000)
    ap.add_argument("--deterministic-only", action="store_true",
                    help="skip model passes (no API keys needed)")
    ap.add_argument("--only", nargs="*", help="restrict to doc_ids")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    corpus = Corpus(find_corpus_root(ROOT))
    tests = json.loads(TESTS.read_text(encoding="utf-8"))
    team = load_team_docs()

    targets: list[tuple[str, str, str, str]] = []
    for d in corpus.with_text():
        targets.append((d.doc_id, d.jurisdiction, d.text, "captured"))
    for doc_id, meta in team.items():
        if doc_id not in {t[0] for t in targets}:
            juris = (corpus.get(doc_id).jurisdiction if corpus.get(doc_id) else "")
            targets.append((doc_id, juris, meta["text"], meta["tier"]))
    if args.only:
        targets = [t for t in targets if t[0] in set(args.only)]

    print(f"Documents to analyze: {len(targets)}  "
          f"(captured={sum(1 for t in targets if t[3]=='captured')}, team={len(team)})")
    print(f"Change tests: {[t['test_id'] for t in tests]}")
    if args.deterministic_only:
        print("Mode: deterministic only (no model calls)")

    signals: list[dict] = []
    for i, (doc_id, juris, text, tier) in enumerate(targets, 1):
        print(f"  [{i:3d}/{len(targets)}] {doc_id:10s} {tier:9s} {len(text):7d} chars  {juris}")
        det = deterministic_pass(doc_id, text)
        c = o = None
        if not args.deterministic_only:
            c = claude_pass(doc_id, juris, text, args.claude_model, args.max_chars)
            o = openai_pass(doc_id, juris, text, args.openai_model, args.max_chars)
        rec = reconcile(doc_id, det, c, o, text)
        rec["tier"] = tier
        rec["manifest_jurisdiction"] = juris
        signals.append(rec)

    mapped = map_to_tests(signals, tests, corpus)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "models": {"claude": args.claude_model, "openai": args.openai_model,
                   "deterministic_only": args.deterministic_only},
        "documents_analyzed": len(signals),
        "summary": {
            "with_conflicts": sum(1 for s in signals if s["conflict_flag"]),
            "models_agree": sum(1 for s in signals if s["models_agree"]),
            "total_findings": sum(len(s["findings"]) for s in signals),
        },
        "tests": mapped,
        "signals": signals,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("\n=== Change-signal summary ===")
    for tid, e in mapped.items():
        gap = "  <-- EVIDENCE GAP" if e["evidence_gaps"] else ""
        print(f"  {tid}  evidence_docs={len(e['evidence']):3d}  conflicts={len(e['conflict_flags']):2d}{gap}")
    print(f"\nFindings: {payload['summary']['total_findings']}  "
          f"conflicted docs: {payload['summary']['with_conflicts']}")
    print(f"Written: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
