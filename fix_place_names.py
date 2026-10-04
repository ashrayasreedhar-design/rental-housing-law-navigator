"""Re-normalise Census place names in an existing geocode cache.

Census returns the legal place NAME with its incorporation type appended --
"Los Angeles city", "Cambridge city", "Hoboken city". The corpus manifest and
config use the bare name ("Los Angeles, CA"), so every jurisdiction match
would fail silently and no rule would ever attach to an address.

Detected on the first real geocode run: alias_verification flagged all 485
resolved addresses, which is the signature of a normalisation bug rather than
485 genuine boundary problems.

Makes NO network calls -- it rewrites the existing cache in place. Run from
the repo root:

    python3 fix_place_names.py
"""
import collections
import json
from pathlib import Path

import yaml

SUFFIXES = (" city and borough", " consolidated government", " metro government",
            " unified government", " urban county", " municipality", " township",
            " borough", " village", " town", " city", " CDP")


def normalize(name):
    """Strip the Census incorporation-type suffix.

    Ordered longest-first so "Jersey City city" -> "Jersey City" rather than
    being over-stripped, and "Sitka city and borough" -> "Sitka".
    """
    if not name:
        return name
    out = name.strip()
    low = out.lower()
    for suf in sorted(SUFFIXES, key=len, reverse=True):
        if low.endswith(suf.lower()):
            return out[: -len(suf)].strip()
    return out


def main():
    cache = Path("out/geocode_cache.json")
    data = json.loads(cache.read_text())

    changed = 0
    for rec in data["results"].values():
        before = rec.get("city")
        after = normalize(before)
        if before != after:
            rec["city"] = after
            changed += 1

    counts = collections.Counter()
    for rec in data["results"].values():
        if rec.get("resolved") and rec.get("city"):
            counts[f"{rec['city']}, {rec['state']}"] += 1
    data["legal_city_counts"] = dict(counts.most_common())

    clean = json.loads(Path("out/addresses_clean.json").read_text())["addresses"]
    cfg = yaml.safe_load(Path("config/jurisdictions.yml").read_text())
    aliases = cfg.get("postal_city_aliases", {}) or {}

    issues = []
    for a in clean:
        r = data["results"].get(a["address_id"])
        if not r or not r.get("resolved"):
            continue
        expected = (aliases.get(a["state"], {}) or {}).get(a["postal_city"])
        city = r.get("city")
        if expected and city and city.lower() != expected.lower():
            issues.append({"address_id": a["address_id"], "postal_city": a["postal_city"],
                           "expected": expected, "geocoded": city})
        elif not expected and city and city.lower() != a["postal_city"].lower():
            issues.append({"address_id": a["address_id"], "postal_city": a["postal_city"],
                           "expected": None, "geocoded": city})
    data["alias_verification"] = issues

    cache.write_text(json.dumps(data, indent=2))

    print(f"Re-normalised {changed} place names (no network calls).\n")
    print("=== legal_city_counts ===")
    for k, v in data["legal_city_counts"].items():
        print(f"  {k:24s} {v:4d}")
    print(f"\n=== alias_verification issues: {len(issues)} (was 485) ===")
    for i in issues[:15]:
        print("  ", i)


if __name__ == "__main__":
    main()
