"""Corpus loading with provenance.

Every document carries its source URL and retrieval date, parsed from the
file header that the starter pack writes:

    SOURCE: https://...
    RETRIEVED: 2026-10-01 22:44 UTC

Nothing in this module is jurisdiction-specific. Adding a jurisdiction means
adding rows to corpus_manifest.csv and text files to corpus/text/ -- no code
changes.
"""
from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SOURCE_RE = re.compile(r"^SOURCE:\s*(\S+)\s*$", re.M)
RETRIEVED_RE = re.compile(r"^RETRIEVED:\s*(.+?)\s*$", re.M)

# A source older than this (relative to the query date) gets a staleness flag.
STALE_AFTER_DAYS = 180


@dataclass
class Document:
    doc_id: str
    jurisdiction: str
    url: str
    source_type: str
    capture: str
    status: str
    text: str | None = None
    retrieved_at: str | None = None
    manifest_sha256: str | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def has_text(self) -> bool:
        return bool(self.text)

    @property
    def is_official(self) -> bool:
        return self.source_type.startswith("official")

    def sha256(self) -> str | None:
        if self.text is None:
            return None
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def body(self) -> str:
        """Text with the provenance header stripped, for extraction."""
        if not self.text:
            return ""
        parts = self.text.split("\n\n", 1)
        return parts[1] if len(parts) == 2 and parts[0].startswith("SOURCE:") else self.text

    def contains_verbatim(self, span: str) -> bool:
        """Whitespace-normalised containment check used by the quote validator.

        Source documents come from PDFs and HTML, so runs of whitespace and
        line breaks are not meaningful. Everything else must match exactly --
        we deliberately do not fuzzy-match, because the whole point is to
        catch invented quotations.
        """
        if not self.text or not span:
            return False
        norm = lambda s: re.sub(r"\s+", " ", s).strip()
        return norm(span) in norm(self.text)


class Corpus:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.manifest_path = self.root / "corpus_manifest.csv"
        self.text_dir = self.root / "text"
        self.docs: dict[str, Document] = {}
        self._load()

    def _load(self) -> None:
        with self.manifest_path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                doc = Document(
                    doc_id=row["doc_id"],
                    jurisdiction=row["jurisdictions"],
                    url=row["url"],
                    source_type=row["source_type"],
                    capture=row["capture"],
                    status=row["status"],
                    manifest_sha256=row.get("sha256") or None,
                    retrieved_at=row.get("retrieved_at") or None,
                )
                tf = row.get("text_file")
                if tf:
                    path = self.root / tf
                    if not path.exists():  # tolerate flat layout
                        path = self.text_dir / f"{doc.doc_id}.txt"
                    if path.exists():
                        raw = path.read_text(encoding="utf-8", errors="replace")
                        doc.text = raw
                        m = SOURCE_RE.search(raw)
                        if m and m.group(1) != doc.url:
                            doc.flags.append("source_url_mismatch")
                        if not m:
                            doc.flags.append("missing_source_header")
                        r = RETRIEVED_RE.search(raw)
                        if r:
                            doc.retrieved_at = r.group(1)
                        else:
                            doc.flags.append("missing_retrieved_header")
                    else:
                        doc.flags.append("text_file_listed_but_absent")
                self.docs[doc.doc_id] = doc

    # -- access -------------------------------------------------------
    def __getitem__(self, doc_id: str) -> Document:
        return self.docs[doc_id]

    def get(self, doc_id: str) -> Document | None:
        return self.docs.get(doc_id)

    def with_text(self) -> list[Document]:
        return [d for d in self.docs.values() if d.has_text]

    def jurisdictions(self) -> list[str]:
        return sorted({d.jurisdiction for d in self.docs.values()})

    # -- data quality -------------------------------------------------
    def coverage_report(self) -> list[dict]:
        """Per-jurisdiction captured-text coverage. Surfaced in the dashboard
        so thin coverage is visible rather than silently under-extracted."""
        out: dict[str, dict] = {}
        for d in self.docs.values():
            e = out.setdefault(d.jurisdiction, {"jurisdiction": d.jurisdiction, "total": 0, "with_text": 0, "link_only": 0, "check_terms": 0})
            e["total"] += 1
            if d.has_text:
                e["with_text"] += 1
            if d.capture == "link-only":
                e["link_only"] += 1
            elif d.capture == "check-terms":
                e["check_terms"] += 1
        for e in out.values():
            e["coverage"] = round(e["with_text"] / e["total"], 3) if e["total"] else 0.0
            e["no_text"] = e["total"] - e["with_text"]
        return sorted(out.values(), key=lambda r: r["coverage"])

    def stale_sources(self, as_of: str) -> list[dict]:
        ref = datetime.fromisoformat(as_of)
        out = []
        for d in self.with_text():
            if not d.retrieved_at:
                continue
            try:
                got = datetime.strptime(d.retrieved_at.replace(" UTC", ""), "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            age = (ref - got).days
            if age > STALE_AFTER_DAYS:
                out.append({"doc_id": d.doc_id, "retrieved_at": d.retrieved_at, "age_days": age})
        return out

    def integrity_report(self) -> list[dict]:
        """Flag header problems.

        Note on sha256: the manifest's sha256 is the digest of the ORIGINAL
        fetched artifact (the PDF or HTML at the source URL), not of the
        extracted .txt. We verified this empirically -- no digest of the text
        file (raw, header-stripped, or newline-normalised) reproduces the
        manifest value for any sampled document. So the manifest hash
        authenticates the upstream capture, and cannot be used to check the
        text files. We record it as provenance and do not flag a mismatch,
        which would otherwise fire on all 54 documents.
        """
        out = []
        for d in self.docs.values():
            if d.flags:
                out.append({"doc_id": d.doc_id, "jurisdiction": d.jurisdiction, "flags": list(d.flags)})
        return out
