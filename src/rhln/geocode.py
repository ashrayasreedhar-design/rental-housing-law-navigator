"""Address -> jurisdiction stack resolution via the Census Geocoder.

RUNS ON REPLIT (needs network; the Census host is not reachable from the
development sandbox).

Why the Census Geocoder: README section 6 names it, it needs no API key, it
handles batches up to 10,000 rows, and critically it returns the
*incorporated place* -- the legal city -- rather than the mailing city. That
is what resolves the postal-vs-legal trap: "Dorchester" and "Allston" resolve
to Boston, "San Ysidro" to San Diego, "Van Nuys" to Los Angeles.

Design notes:

  * Results are CACHED to disk and committed. A judge reproducing the demo
    should not need 500 live API calls, and the development sandbox cannot
    make them at all.
  * An unresolved address is NEVER back-filled from postal_city. It is marked
    unresolved, and every rule lookup against it returns `unknown`. Guessing
    the jurisdiction would defeat the entire point of Module B.
  * A ZIP that contradicts its state has already been withheld upstream by
    rhln.clean, so it cannot drag a match into the wrong state.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[2]
CLEAN = ROOT / "out" / "addresses_clean.json"
CONFIG = ROOT / "config" / "jurisdictions.yml"

BATCH_URL = "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch"
ONELINE_URL = "https://geocoding.geo.census.gov/geocoder/geographies/address"


@dataclass
class Jurisdiction:
    """The jurisdiction stack for one address."""
    state: str | None = None
    county: str | None = None
    city: str | None = None            # incorporated place -- the LEGAL city
    place_fips: str | None = None
    county_fips: str | None = None
    state_fips: str | None = None
    matched_address: str | None = None
    lat: float | None = None
    lon: float | None = None
    resolved: bool = False
    method: str | None = None          # oneline | cache
    notes: list[str] = field(default_factory=list)

    def stack(self) -> list[dict]:
        """Ordered most-specific-first, for rule precedence."""
        out = []
        if self.city and self.state:
            out.append({"level": "city", "name": f"{self.city}, {self.state}"})
        if self.county and self.state:
            out.append({"level": "county", "name": f"{self.county}, {self.state}"})
        if self.state:
            out.append({"level": "state", "name": self.state})
        return out


def load_config() -> dict:
    return (yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}) if CONFIG.exists() else {}


def _geo_from_payload(geos: dict) -> dict:
    """Pull state/county/place out of a Census `geographies` block."""
    out: dict = {}
    places = geos.get("Incorporated Places") or geos.get("Census Designated Places") or []
    if places:
        out["city"] = places[0].get("NAME")
        out["place_fips"] = places[0].get("PLACE") or places[0].get("GEOID")
    counties = geos.get("Counties") or []
    if counties:
        out["county"] = counties[0].get("NAME")
        out["county_fips"] = counties[0].get("GEOID") or counties[0].get("COUNTY")
    states = geos.get("States") or []
    if states:
        out["state"] = states[0].get("STUSAB") or states[0].get("STATE")
        out["state_fips"] = states[0].get("GEOID") or states[0].get("STATE")
    return out


def geocode_one(row: dict, cfg: dict, session: requests.Session) -> Jurisdiction:
    """Single-address lookup. Returns named geographies including the place."""
    g = cfg.get("geocode", {})
    params = {
        "street": row.get("street", ""),
        "city": row.get("city", ""),
        "state": row.get("state", ""),
        "benchmark": g.get("benchmark", "Public_AR_Current"),
        "vintage": g.get("vintage", "Current_Current"),
        "layers": g.get("layers", "Incorporated Places,Counties,States"),
        "format": "json",
    }
    if row.get("zip"):
        params["zip"] = row["zip"]

    j = Jurisdiction(method="oneline")
    retries = int(g.get("max_retries", 2))
    for attempt in range(retries + 1):
        try:
            resp = session.get(ONELINE_URL, params=params,
                               timeout=g.get("timeout_seconds", 30))
            if resp.status_code != 200:
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                j.notes.append(f"HTTP {resp.status_code}")
                return j
            matches = (resp.json().get("result", {}) or {}).get("addressMatches", [])
            if not matches:
                j.notes.append("no address match")
                return j
            m = matches[0]
            j.matched_address = m.get("matchedAddress")
            coord = m.get("coordinates") or {}
            j.lat, j.lon = coord.get("y"), coord.get("x")
            for k, v in _geo_from_payload(m.get("geographies", {}) or {}).items():
                setattr(j, k, v)
            j.resolved = bool(j.state and j.city)
            if not j.city:
                j.notes.append("matched, but no incorporated place "
                               "(likely unincorporated area)")
            return j
        except Exception as exc:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            j.notes.append(f"{exc.__class__.__name__}: {exc}")
    return j


def verify_aliases(results: dict[str, Jurisdiction], addresses: list[dict],
                   cfg: dict) -> list[dict]:
    """Check resolved legal city against the postal-city alias hints.

    This does not change any result. It reports where the geocoder disagreed
    with expectation so a human can look -- including the case we care about
    most, where a neighbourhood mailing name should have resolved to its
    parent city.
    """
    aliases = cfg.get("postal_city_aliases", {}) or {}
    issues = []
    for a in addresses:
        j = results.get(a["address_id"])
        if not j or not j.resolved:
            continue
        expected = (aliases.get(a["state"], {}) or {}).get(a["postal_city"])
        if expected and j.city and j.city.lower() != expected.lower():
            issues.append({"address_id": a["address_id"], "postal_city": a["postal_city"],
                           "expected_legal_city": expected, "geocoded_city": j.city,
                           "note": "geocoder disagrees with alias hint; geocoder wins, review the hint"})
        elif not expected and j.city and j.city.lower() != a["postal_city"].lower():
            issues.append({"address_id": a["address_id"], "postal_city": a["postal_city"],
                           "expected_legal_city": None, "geocoded_city": j.city,
                           "note": "mailing city differs from legal city; no alias configured"})
    return issues


def main() -> None:
    ap = argparse.ArgumentParser(description="Resolve addresses to jurisdiction stacks (Census).")
    ap.add_argument("--addresses", default=str(CLEAN),
                    help="addresses_clean.json from `make clean-data`")
    ap.add_argument("--out", default=None)
    ap.add_argument("--limit", type=int, default=0, help="only first N (smoke test)")
    ap.add_argument("--refresh", action="store_true", help="ignore cache")
    args = ap.parse_args()

    cfg = load_config()
    cache_path = ROOT / (args.out or cfg.get("geocode", {}).get("cache_path", "out/geocode_cache.json"))

    src = json.loads(Path(args.addresses).read_text(encoding="utf-8"))
    addresses = src["addresses"]
    if args.limit:
        addresses = addresses[: args.limit]

    cache: dict = {}
    if cache_path.exists() and not args.refresh:
        cache = json.loads(cache_path.read_text(encoding="utf-8")).get("results", {})
        print(f"Cache: {len(cache)} entries at {cache_path.relative_to(ROOT)}")

    session = requests.Session()
    session.headers.update({"User-Agent": "RentalHousingLawNavigator/1.0"})
    delay = float(cfg.get("geocode", {}).get("rate_limit_seconds", 0.4))

    results: dict[str, Jurisdiction] = {}
    todo = []
    for a in addresses:
        if a["address_id"] in cache:
            j = Jurisdiction(**{k: v for k, v in cache[a["address_id"]].items()
                                if k in Jurisdiction.__dataclass_fields__})
            j.method = "cache"
            results[a["address_id"]] = j
        else:
            todo.append(a)

    print(f"Addresses: {len(addresses)}   cached: {len(results)}   to resolve: {len(todo)}")

    for i, a in enumerate(todo, 1):
        row = {"address_id": a["address_id"], **a["geocode_input"]}
        j = geocode_one(row, cfg, session)
        results[a["address_id"]] = j
        if i % 25 == 0 or i == len(todo):
            ok = sum(1 for r in results.values() if r.resolved)
            print(f"  [{i:4d}/{len(todo)}] resolved so far: {ok}")
        time.sleep(delay)

    resolved = [a for a in addresses if results[a["address_id"]].resolved]
    unresolved = [a for a in addresses if not results[a["address_id"]].resolved]
    alias_issues = verify_aliases(results, addresses, cfg)

    city_counts: dict[str, int] = {}
    for a in addresses:
        j = results[a["address_id"]]
        if j.resolved:
            k = f"{j.city}, {j.state}"
            city_counts[k] = city_counts.get(k, 0) + 1

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(addresses),
        "resolved": len(resolved),
        "unresolved": len(unresolved),
        "unresolved_ids": [a["address_id"] for a in unresolved],
        "legal_city_counts": dict(sorted(city_counts.items(), key=lambda kv: -kv[1])),
        "alias_verification": alias_issues,
        "results": {k: asdict(v) for k, v in results.items()},
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"\nResolved {len(resolved)}/{len(addresses)}   unresolved: {len(unresolved)}")
    print("\nLegal city counts (from Census incorporated place):")
    for k, v in payload["legal_city_counts"].items():
        print(f"  {k:28s} {v:4d}")
    if unresolved:
        print(f"\nUnresolved -> every lookup returns 'unknown': "
              f"{payload['unresolved_ids'][:12]}{' ...' if len(unresolved) > 12 else ''}")
    print(f"\nWritten: {cache_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
