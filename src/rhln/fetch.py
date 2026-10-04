"""Targeted, allowlisted source retrieval.  RUNS ON REPLIT (needs network).

Design constraints, driven by README section 3 and section 6:

  * Allowlist only. Every URL is declared in config/sources.yml. There is no
    link discovery and no crawling. This is reading specific pages, not bulk
    scraping.
  * robots.txt is honoured. A disallowed URL is skipped and reported, never
    fetched anyway.
  * Rate limited per-domain with a real delay between requests.
  * Provenance is written into every output file, and team-retrieved documents
    are marked distinctly from organizer-captured ones so no reader can
    confuse the two.
  * Authority fetches may carry `expect_contains`. If the expected phrase is
    absent, the fetch is recorded as FAILED rather than silently accepted --
    this prevents a cookie wall or "page not found" body from becoming a
    citable source.

Outputs
    corpus/text_team/<doc_id>.txt     retrieved documents, with headers
    out/fetch_report.json             per-URL outcome, for the audit log
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.robotparser as robotparser
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "sources.yml"
OUT_DIR = ROOT / "corpus" / "text_team"
REPORT = ROOT / "out" / "fetch_report.json"


# --------------------------------------------------------------------------
def load_config(path: Path = CONFIG) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg", "form"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


class RobotsCache:
    """robots.txt checking that distinguishes 'disallowed' from 'unreachable'.

    urllib.robotparser treats an HTTP 401/403 on robots.txt as disallow-all.
    That is correct per the standard, but it makes a blocked egress proxy
    indistinguishable from a site that genuinely forbids us -- every fetch
    gets skipped and it *looks* like compliance. We fetch robots.txt
    ourselves so the two cases are reported differently and a network problem
    can never masquerade as a policy decision.
    """

    def __init__(self, user_agent: str, enabled: bool = True, session=None):
        self.ua = user_agent
        self.enabled = enabled
        self.session = session or requests.Session()
        self._cache: dict[str, tuple[str, robotparser.RobotFileParser | None, str]] = {}

    def _load(self, origin: str):
        try:
            resp = self.session.get(f"{origin}/robots.txt", timeout=15,
                                    headers={"User-Agent": self.ua})
        except Exception as exc:
            return ("unreachable", None, f"{exc.__class__.__name__}")
        if resp.status_code in (401, 403):
            return ("forbidden", None, f"robots.txt returned {resp.status_code}")
        if resp.status_code >= 400:
            # 404 and friends: no robots.txt published -> allowed by convention
            return ("absent", None, f"robots.txt returned {resp.status_code}")
        rp = robotparser.RobotFileParser()
        rp.parse(resp.text.splitlines())
        return ("ok", rp, f"robots.txt {resp.status_code}")

    def allowed(self, url: str) -> tuple[bool, str]:
        if not self.enabled:
            return True, "robots check disabled"
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self._cache:
            self._cache[origin] = self._load(origin)
        state, rp, detail = self._cache[origin]

        if state == "ok":
            ok = rp.can_fetch(self.ua, url)
            return ok, ("allowed by robots.txt" if ok else "DISALLOWED by robots.txt")
        if state == "absent":
            return True, f"no robots.txt ({detail}); proceeding"
        if state == "forbidden":
            # Genuinely ambiguous: could be site policy, could be a proxy.
            # Do not fetch, and say exactly why so it is never mistaken for
            # a clean robots-based skip.
            return False, f"BLOCKED: {detail} - cannot confirm policy (check egress if unexpected)"
        return False, f"BLOCKED: robots.txt {detail} - cannot confirm policy"


class Fetcher:
    def __init__(self, cfg: dict, dry_run: bool = False):
        d = cfg.get("defaults", {})
        self.delay = float(d.get("rate_limit_seconds", 3.0))
        self.timeout = int(d.get("timeout_seconds", 30))
        self.ua = d.get("user_agent", "RentalHousingLawNavigator/1.0")
        self.max_retries = int(d.get("max_retries", 2))
        self.dry_run = dry_run
        self._last_hit: dict[str, float] = defaultdict(float)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.ua, "Accept": "text/html,application/xhtml+xml,application/pdf,*/*"})
        self.robots = RobotsCache(self.ua, bool(d.get("respect_robots", True)), session=self.session)

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        wait = self.delay - (time.monotonic() - self._last_hit[host])
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def fetch_one(self, entry: dict, tier: str) -> dict:
        url = entry["url"]
        rec = {
            "doc_id": entry["doc_id"],
            "url": url,
            "tier": tier,
            "jurisdiction": entry.get("jurisdiction"),
            "status": "pending",
            "http_status": None,
            "bytes": 0,
            "notes": [],
        }

        allowed, why = self.robots.allowed(url)
        rec["robots"] = why
        if not allowed:
            rec["status"] = "skipped_robots"
            return rec

        if self.dry_run:
            rec["status"] = "dry_run"
            return rec

        last_exc = None
        for attempt in range(self.max_retries + 1):
            try:
                self._throttle(url)
                resp = self.session.get(url, timeout=self.timeout)
                rec["http_status"] = resp.status_code
                if resp.status_code == 200:
                    ctype = resp.headers.get("Content-Type", "")
                    if "pdf" in ctype.lower() or url.lower().endswith(".pdf"):
                        try:
                            from pypdf import PdfReader
                            from io import BytesIO
                            text = "\n".join((p.extract_text() or "") for p in PdfReader(BytesIO(resp.content)).pages)
                        except Exception as exc:
                            rec["status"] = "failed_pdf_parse"
                            rec["notes"].append(str(exc))
                            return rec
                    else:
                        text = html_to_text(resp.text)

                    expect = entry.get("expect_contains") or []
                    missing = [p for p in expect if p.lower() not in text.lower()]
                    if missing:
                        rec["status"] = "failed_expectation"
                        rec["notes"].append(f"expected phrase(s) absent: {missing}")
                        rec["bytes"] = len(text)
                        return rec

                    if len(text) < 400:
                        rec["status"] = "failed_too_short"
                        rec["notes"].append(f"only {len(text)} chars; likely a cookie wall or stub")
                        rec["bytes"] = len(text)
                        return rec

                    rec["bytes"] = len(text)
                    rec["status"] = "ok"
                    rec["text"] = text
                    return rec

                if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    time.sleep(self.delay * (attempt + 2))
                    continue
                rec["status"] = f"failed_http_{resp.status_code}"
                return rec
            except Exception as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(self.delay * (attempt + 2))
                    continue
        rec["status"] = "failed_exception"
        rec["notes"].append(f"{last_exc.__class__.__name__}: {last_exc}")
        return rec

    @staticmethod
    def write_doc(rec: dict, entry: dict, out_dir: Path) -> Path:
        """Write with a provenance header that is explicit about being team-retrieved."""
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{rec['doc_id']}.txt"
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        header = (
            f"SOURCE: {rec['url']}\n"
            f"RETRIEVED: {now}\n"
            f"CAPTURE: team-retrieved (not part of the organizer-supplied capture)\n"
            f"TIER: {rec['tier']}\n"
            f"PUBLISHER: {entry.get('publisher', entry.get('kind', 'unknown'))}\n"
            f"REVIEW: requires_human_review\n"
        )
        path.write_text(header + "\n" + rec["text"], encoding="utf-8")
        return path


# --------------------------------------------------------------------------
def run(tiers: list[str], dry_run: bool, only: list[str] | None) -> dict:
    cfg = load_config()
    f = Fetcher(cfg, dry_run=dry_run)

    seen_urls: dict[str, str] = {}
    results: list[dict] = []

    for tier in tiers:
        for entry in cfg.get(tier, []):
            if entry.get("enabled") is False:
                results.append({"doc_id": entry["doc_id"], "url": entry["url"], "tier": tier,
                                "status": "disabled", "notes": ["enabled: false in config"]})
                continue
            if only and entry["doc_id"] not in only:
                continue
            url = entry["url"].replace("https://www.", "https://").rstrip("/")
            if url in seen_urls:
                results.append({"doc_id": entry["doc_id"], "url": entry["url"], "tier": tier,
                                "status": "duplicate", "notes": [f"same URL as {seen_urls[url]}"]})
                continue
            seen_urls[url] = entry["doc_id"]

            rec = f.fetch_one(entry, tier)
            if rec["status"] == "ok":
                path = Fetcher.write_doc(rec, entry, OUT_DIR)
                rec["path"] = str(path.relative_to(ROOT))
                rec.pop("text", None)
            results.append(rec)
            print(f"  [{rec['status']:22s}] {rec['doc_id']:14s} {rec.get('bytes',0):7d}B  {entry['url'][:66]}")

    summary: dict = {"generated_at": datetime.now(timezone.utc).isoformat(), "tiers": tiers,
                     "counts": {}, "results": results}
    for r in results:
        summary["counts"][r["status"]] = summary["counts"].get(r["status"], 0) + 1
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="Allowlisted source fetcher (Replit).")
    ap.add_argument("--tier", choices=["authority", "signal", "both"], default="both")
    ap.add_argument("--dry-run", action="store_true", help="check robots + config, fetch nothing")
    ap.add_argument("--only", nargs="*", help="restrict to specific doc_ids")
    args = ap.parse_args()

    tiers = ["authority", "signal"] if args.tier == "both" else [args.tier]
    print(f"Fetching tiers={tiers} dry_run={args.dry_run}")
    s = run(tiers, args.dry_run, args.only)
    print("\nSummary:")
    for k, v in sorted(s["counts"].items()):
        print(f"  {k:22s} {v}")
    print(f"\nReport: {REPORT.relative_to(ROOT)}")

    failed = [r for r in s["results"] if r["status"].startswith("failed")]
    if failed:
        print(f"\n{len(failed)} failed. These become documented coverage gaps, not invented rules:")
        for r in failed:
            print(f"  {r['doc_id']:14s} {r['status']:22s} {r.get('notes')}")


if __name__ == "__main__":
    main()
