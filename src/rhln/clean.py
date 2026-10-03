"""Address data cleaning and quality flagging.

Principle: FLAG, DO NOT FIX. Every anomaly found in the supplied sample is
recorded as a quality flag attached to the address, and problematic inputs are
withheld from downstream consumers rather than corrected. We never impute a
missing year built, never repair a ZIP, never guess a legal city.

This matters because the gaps are deliberate. README section 4.1 describes
missing owner names, missing unit counts and missing construction years as
facts the data does not have -- the correct behaviour is `unknown`, and any
imputation would manufacture a confident wrong answer.

Flags produced (see QualityFlag):
    zip_state_conflict   ZIP prefix inconsistent with the state column
    zip_missing          no ZIP supplied
    year_built_missing   no construction year
    units_missing        no unit count
    co_cutoff_boundary   year built falls exactly on a certificate-of-occupancy
                         cutoff, so coverage cannot be determined from year alone
    postal_city_alias    mailing city differs from the likely legal city
"""
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ADDRESSES = ROOT / "data" / "sample_addresses.csv"
JURIS_CONFIG = ROOT / "config" / "jurisdictions.yml"
OUT = ROOT / "out" / "addresses_clean.json"


@dataclass
class QualityFlag:
    code: str
    severity: str          # info | warn | error
    detail: str
    affects: list[str] = field(default_factory=list)   # what it makes unanswerable


@dataclass
class CleanAddress:
    address_id: str
    street_address: str
    postal_city: str
    state: str
    zip: str | None
    year_built: int | None
    units: int | None
    use_code: str
    use_description: str
    source_dataset: str
    retrieved_at: str
    # geocoder input, with conflicting values withheld
    geocode_input: dict = field(default_factory=dict)
    flags: list[QualityFlag] = field(default_factory=list)

    def flag_codes(self) -> list[str]:
        return [f.code for f in self.flags]


def load_config(path: Path = JURIS_CONFIG) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _int_or_none(v: str | None) -> int | None:
    v = (v or "").strip()
    return int(v) if v.isdigit() else None


def clean_row(row: dict, cfg: dict) -> CleanAddress:
    state = (row.get("state") or "").strip()
    raw_zip = (row.get("zip") or "").strip()
    postal_city = (row.get("postal_city") or "").strip()

    addr = CleanAddress(
        address_id=row["address_id"],
        street_address=(row.get("street_address") or "").strip(),
        postal_city=postal_city,
        state=state,
        zip=raw_zip or None,
        year_built=_int_or_none(row.get("year_built")),
        units=_int_or_none(row.get("units")),
        use_code=(row.get("use_code") or "").strip(),
        use_description=(row.get("use_description") or "").strip(),
        source_dataset=(row.get("source_dataset") or "").strip(),
        retrieved_at=(row.get("retrieved_at") or "").strip(),
    )

    zip_prefixes = (cfg.get("zip_prefixes") or {})
    valid_prefixes = tuple(zip_prefixes.get(state, ()))

    # --- ZIP integrity ----------------------------------------------------
    use_zip = True
    if not raw_zip:
        addr.flags.append(QualityFlag(
            "zip_missing", "info",
            "No ZIP supplied; geocoding on street + city + state.",
        ))
        use_zip = False
    elif valid_prefixes and not any(raw_zip.startswith(p) for p in valid_prefixes):
        addr.flags.append(QualityFlag(
            "zip_state_conflict", "error",
            f"ZIP {raw_zip} is inconsistent with state {state} "
            f"(expected prefix {'/'.join(valid_prefixes)}). ZIP withheld from "
            f"geocoding; NOT corrected, because the correct value is unknown.",
            affects=["jurisdiction_resolution"],
        ))
        use_zip = False

    addr.geocode_input = {
        "street": addr.street_address,
        "city": postal_city,
        "state": state,
        **({"zip": raw_zip} if use_zip else {}),
    }

    # --- coverage facts the data does not have ---------------------------
    if addr.year_built is None:
        addr.flags.append(QualityFlag(
            "year_built_missing", "warn",
            "No construction year; any rule whose coverage turns on a build-year "
            "or certificate-of-occupancy cutoff must return 'unknown'.",
            affects=["rent_increase_limits", "just_cause_eviction"],
        ))
    if addr.units is None:
        addr.flags.append(QualityFlag(
            "units_missing", "warn",
            "No unit count; any rule whose coverage or exemption turns on unit "
            "count must return 'unknown'.",
            affects=["security_deposits", "just_cause_eviction", "rent_increase_limits"],
        ))

    # --- certificate-of-occupancy boundary --------------------------------
    # README 4.1: year built is not the certificate date. A building in the
    # cutoff year cannot be resolved from year alone.
    for entry in (cfg.get("co_cutoffs") or []):
        if entry.get("postal_city") and entry["postal_city"] != postal_city:
            continue
        if entry.get("state") and entry["state"] != state:
            continue
        cutoff_year = int(str(entry["cutoff"])[:4])
        if addr.year_built == cutoff_year:
            addr.flags.append(QualityFlag(
                "co_cutoff_boundary", "warn",
                f"year_built {addr.year_built} falls exactly on the "
                f"{entry.get('label', entry['cutoff'])} cutoff. Year built is not "
                f"the certificate-of-occupancy date, so coverage is 'unknown'.",
                affects=[entry.get("category", "rent_increase_limits")],
            ))

    # --- postal city alias -------------------------------------------------
    # Recorded as a hint only. The authoritative legal city comes from the
    # geocoder; we never substitute a name ourselves.
    aliases = (cfg.get("postal_city_aliases") or {}).get(state, {})
    if postal_city in aliases:
        addr.flags.append(QualityFlag(
            "postal_city_alias", "info",
            f"Mailing city '{postal_city}' is commonly within "
            f"'{aliases[postal_city]}'. Hint only -- legal jurisdiction is "
            f"resolved by the geocoder, not by this mapping.",
            affects=["jurisdiction_resolution"],
        ))
    return addr


def run(addresses: Path, out: Path) -> dict:
    cfg = load_config()
    rows = list(csv.DictReader(addresses.open(newline="", encoding="utf-8")))
    cleaned = [clean_row(r, cfg) for r in rows]

    counts: dict[str, int] = {}
    severity: dict[str, int] = {}
    for a in cleaned:
        for f in a.flags:
            counts[f.code] = counts.get(f.code, 0) + 1
            severity[f.severity] = severity.get(f.severity, 0) + 1

    by_state: dict[str, int] = {}
    for a in cleaned:
        by_state[a.state] = by_state.get(a.state, 0) + 1

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "address_count": len(cleaned),
        "by_state": by_state,
        "flag_counts": counts,
        "severity_counts": severity,
        "addresses": [
            {**asdict(a), "flags": [asdict(f) for f in a.flags]} for a in cleaned
        ],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description="Clean and quality-flag sample addresses.")
    ap.add_argument("--addresses", default=str(ADDRESSES))
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    p = run(Path(args.addresses), Path(args.out))
    print(f"Addresses: {p['address_count']}   by state: {p['by_state']}")
    print("\nQuality flags (flagged, never silently repaired):")
    for code, n in sorted(p["flag_counts"].items(), key=lambda kv: -kv[1]):
        print(f"  {code:22s} {n:4d}")
    print("\nSeverity:", p["severity_counts"])
    print(f"Written: {Path(args.out).relative_to(ROOT)}")


if __name__ == "__main__":
    main()
