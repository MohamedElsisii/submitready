#!/usr/bin/env python3
"""SubmitReady references: read, audit and verify a manuscript's references.

Reads references from:
  * a Word manuscript with live Mendeley Cite citations (.docx)
  * reference-manager exports: RIS (.ris), BibTeX (.bib), CSL JSON (.json)

Commands:
  audit   offline checks of the metadata (missing DOI, broken fields, duplicates,
          uncited library entries, manually overridden citations, ...)
  verify  compares each reference with Crossref (by DOI) or PubMed (by title),
          and flags mismatches, retractions and preprints that are now published.
          Needs internet access to api.crossref.org and eutils.ncbi.nlm.nih.gov.

Nothing is ever corrected silently: every finding shows what was found and,
where possible, a suggested value for the author to accept or reject.

Usage:
  python scripts/refs.py audit manuscript.docx [--library export.ris] [--report r.md]
  python scripts/refs.py verify manuscript.docx [--report r.md] [--email you@uni.edu]
"""
from __future__ import annotations

import argparse
import base64
import difflib
import html
import json
import re
import sys
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

VERSION = "0.1.0"
DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
DOI_URL_RE = re.compile(r"^(?:https?://)?(?:dx\.)?doi\.org/(10\.\S+)$", re.I)


# ---------------------------------------------------------------- data model
@dataclass
class Ref:
    key: str                      # stable id (Mendeley id, RIS index, BibTeX key)
    data: dict                    # CSL-JSON style fields
    source: str                   # where it came from (file name + position)
    cited: int = 0                # times cited in the manuscript (docx only)


@dataclass
class Finding:
    ref: str                      # short label for the reference
    code: str
    severity: str                 # error | warning | info
    message: str
    found: str = ""
    suggested: str = ""


@dataclass
class ManuscriptRefs:
    refs: list[Ref] = field(default_factory=list)
    citations: int = 0
    manual_overrides: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- readers
def _b64json(s: str) -> dict:
    return json.loads(base64.b64decode(s + "=" * (-len(s) % 4)))


def read_docx(path: Path) -> ManuscriptRefs:
    """Live Mendeley Cite citations: CSL JSON stored in content-control tags."""
    xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8")
    out = ManuscriptRefs()
    by_key: dict[str, Ref] = {}
    for tag in re.findall(r'w:tag w:val="MENDELEY_CITATION_v3_([^"]+)"', xml):
        try:
            c = _b64json(html.unescape(tag))
        except Exception:
            continue
        out.citations += 1
        mo = c.get("manualOverride") or {}
        if mo.get("isManuallyOverridden"):
            out.manual_overrides.append(f"{mo.get('citeprocText', '')} typed over as {mo.get('manualOverrideText', '')!r}")
        for item in c.get("citationItems", []):
            d = item.get("itemData") or {}
            k = item.get("id") or d.get("id") or d.get("DOI") or d.get("title", "")
            if k not in by_key:
                by_key[k] = Ref(k, d, f"{path.name}, citation {out.citations}")
                out.refs.append(by_key[k])
            by_key[k].cited += 1
    return out


RIS_TYPES = {
    "JOUR": "article-journal", "JFULL": "article-journal", "EJOUR": "article-journal",
    "CHAP": "chapter", "BOOK": "book", "EBOOK": "book", "CONF": "paper-conference",
    "CPAPER": "paper-conference", "THES": "thesis", "RPRT": "report", "ELEC": "webpage",
    "COMP": "software", "DATA": "dataset", "GEN": "generic",
}


def _ris_name(a: str) -> dict:
    if "," in a:
        fam, giv = a.split(",", 1)
        return {"family": fam.strip(), "given": giv.strip()}
    return {"literal": a.strip()}


def read_ris(path: Path) -> list[Ref]:
    refs, cur, n = [], None, 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^([A-Z][A-Z0-9])  - ?(.*)$", line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if k == "TY":
            n += 1
            cur = {"type": RIS_TYPES.get(v, "generic"), "_ris_type": v}
        elif cur is None:
            continue
        elif k == "ER":
            refs.append(Ref(f"ris-{n}", cur, f"{path.name}, entry {n}"))
            cur = None
        elif k in ("AU", "A1"):
            cur.setdefault("author", []).append(_ris_name(v))
        elif k in ("ED", "A2") and cur.get("type") != "article-journal":
            cur.setdefault("editor", []).append(_ris_name(v))
        elif k in ("TI", "T1"):
            cur["title"] = v
        elif k in ("T2", "JO", "JF", "BT"):
            cur.setdefault("container-title", v)
        elif k == "J2":
            cur["container-title-short"] = v
        elif k in ("PY", "Y1"):
            y = re.match(r"(\d{4})", v)
            if y:
                cur["issued"] = {"date-parts": [[int(y.group(1))]]}
        elif k == "VL":
            cur["volume"] = v
        elif k == "IS":
            cur["issue"] = v
        elif k == "SP":
            cur["_sp"] = v
        elif k == "EP":
            cur["_ep"] = v
        elif k == "DO":
            cur["DOI"] = v
        elif k == "UR":
            cur.setdefault("URL", v)
        elif k == "PB":
            cur["publisher"] = v
        elif k == "SN":
            cur["ISSN"] = v
    for r in refs:
        sp, ep = r.data.pop("_sp", ""), r.data.pop("_ep", "")
        if sp:
            r.data["page"] = sp if not ep or ep == sp else f"{sp}-{ep}"
    return refs


def read_bib(path: Path) -> list[Ref]:
    text = path.read_text(encoding="utf-8", errors="replace")
    refs = []
    for i, m in enumerate(re.finditer(r"@(\w+)\s*\{\s*([^,]+),(.*?)\n\}", text, re.S), 1):
        typ, key, body = m.group(1).lower(), m.group(2).strip(), m.group(3)
        f = {k.lower(): re.sub(r"[{}]", "", v).strip()
             for k, v in re.findall(r"(\w+)\s*=\s*[{\"](.*?)[}\"]\s*,?\s*(?=\n\s*\w+\s*=|\Z)", body, re.S)}
        d = {"type": {"article": "article-journal", "inproceedings": "paper-conference",
                      "incollection": "chapter", "book": "book", "misc": "generic"}.get(typ, "generic")}
        if "title" in f:
            d["title"] = " ".join(f["title"].split())
        if "journal" in f or "booktitle" in f:
            d["container-title"] = f.get("journal") or f.get("booktitle")
        for src, dst in (("volume", "volume"), ("number", "issue"), ("doi", "DOI"), ("url", "URL")):
            if src in f:
                d[dst] = f[src]
        if "pages" in f:
            d["page"] = f["pages"].replace("--", "-")
        if "year" in f and f["year"][:4].isdigit():
            d["issued"] = {"date-parts": [[int(f["year"][:4])]]}
        if "author" in f:
            d["author"] = [_ris_name(a) for a in re.split(r"\s+and\s+", f["author"])]
        refs.append(Ref(key, d, f"{path.name}, entry {i}"))
    return refs


def read_csl_json(path: Path) -> list[Ref]:
    items = json.loads(path.read_text(encoding="utf-8"))
    return [Ref(str(d.get("id", i)), d, f"{path.name}, entry {i}") for i, d in enumerate(items, 1)]


def load(path: Path) -> ManuscriptRefs:
    suf = path.suffix.lower()
    if suf == ".docx":
        return read_docx(path)
    reader = {".ris": read_ris, ".bib": read_bib, ".json": read_csl_json}.get(suf)
    if not reader:
        raise SystemExit(f"Unsupported file type: {path.name}")
    return ManuscriptRefs(refs=reader(path))


# ---------------------------------------------------------------- helpers
def label(r: Ref) -> str:
    a = (r.data.get("author") or [{}])[0]
    name = a.get("family") or a.get("literal") or "?"
    y = year(r.data) or "n.d."
    t = (r.data.get("title") or "")[:50]
    return f"{name} {y}, \"{t}\""


def year(d: dict) -> str:
    try:
        return str(d["issued"]["date-parts"][0][0])
    except (KeyError, IndexError, TypeError):
        return ""


def strip_doi(doi: str) -> str:
    """The bare DOI, original letter case kept (for suggestions)."""
    doi = doi.strip()
    m = DOI_URL_RE.match(doi)
    return (m.group(1) if m else doi).strip().rstrip(".")


def norm_doi(doi: str) -> str:
    """The bare DOI in lower case (for comparisons; DOIs are case-insensitive)."""
    return strip_doi(doi).lower()


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKD", html.unescape(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def titles_match(a: str, b: str) -> bool:
    """Same work, allowing for version tags, subtitles cut off and trailing marks."""
    def clean(t):
        t = re.sub(r"\[(version|v)\s*\d[^\]]*\]", "", t or "", flags=re.I)
        return norm_text(t)
    x, y = clean(a), clean(b)
    if not x or not y:
        return True
    return x == y or similar(x, y) >= 0.9 or (min(len(x), len(y)) > 25 and (x in y or y in x))


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm_text(a), norm_text(b)).ratio()


# ---------------------------------------------------------------- audit
JOURNAL_TYPES = {"article-journal"}


def audit(ms: ManuscriptRefs, library: list[Ref] | None = None) -> list[Finding]:
    out: list[Finding] = []
    add = out.append
    for r in ms.refs:
        d, L = r.data, label(r)
        typ = d.get("type", "")
        doi = (d.get("DOI") or "").strip()
        if doi:
            if DOI_URL_RE.match(doi):
                add(Finding(L, "doi-as-url", "warning", "DOI is stored as a web link", doi, strip_doi(doi)))
            if not DOI_RE.match(norm_doi(doi)):
                add(Finding(L, "doi-invalid", "error", "DOI does not look valid", doi))
        elif typ in JOURNAL_TYPES or d.get("_ris_type") == "GEN":
            add(Finding(L, "doi-missing", "warning", "No DOI; verify will try to find it by title"))
        if typ == "generic":
            add(Finding(L, "type-generic", "warning",
                        "Reference type is 'generic'; styles may format it wrongly. Set the real type "
                        "(e.g. journal article) in your reference manager",
                        d.get("_ris_type", "generic")))
        if typ in JOURNAL_TYPES:
            for f, name in (("container-title", "journal name"), ("volume", "volume"),
                            ("issued", "year"), ("author", "authors")):
                if not d.get(f):
                    add(Finding(L, f"missing-{f}", "warning", f"Missing {name}"))
            if not d.get("page"):
                add(Finding(L, "missing-page", "info", "Missing pages or article number"))
            if not d.get("issue"):
                add(Finding(L, "missing-issue", "info", "Missing issue"))
        vol = str(d.get("volume") or "")
        if vol and not re.fullmatch(r"[A-Za-z]?\d+[A-Za-z]?(-\d+)?", vol):
            m = re.search(r"\d+", vol)
            add(Finding(L, "odd-volume", "warning", "Volume has extra text", vol, m.group(0) if m else ""))
        iss = str(d.get("issue") or "")
        if iss and not re.fullmatch(r"[A-Za-z]{0,2}\d+[A-Za-z]?(-\d+)?|S\d+|Suppl.*|W\d+", iss):
            m = re.match(r"\d+", iss)
            add(Finding(L, "odd-issue", "warning", "Issue has extra text", iss, m.group(0) if m else ""))
        page = str(d.get("page") or "")
        pm = re.fullmatch(r"(\S+?)\s*[-\u2013]\s*(\S+)", page)
        if pm and pm.group(1) == pm.group(2):
            add(Finding(L, "page-duplicated", "info", "Same first and last page", page, pm.group(1)))
        title = d.get("title") or ""
        if re.search(r"<[^>]+>", title):
            add(Finding(L, "title-html", "warning", "Title contains HTML tags", title,
                        re.sub(r"<[^>]+>", "", title)))
        if re.search(r"\w\?\w|\?\w+\?|\ufffd", title):
            add(Finding(L, "title-encoding", "warning",
                        "Title has a '?' where a special character was probably lost", title))
        if re.search(r"[*\u2020\u2021]\s*$", title):
            add(Finding(L, "title-trailing-mark", "warning", "Title ends with a footnote mark",
                        title, re.sub(r"\s*[*\u2020\u2021]+\s*$", "", title)))
        if title and title.isupper() and len(title) > 20:
            add(Finding(L, "title-all-caps", "info", "Title is in all capitals", title))
        authors = d.get("author") or []
        if authors and sum(1 for a in authors if re.fullmatch(r"[A-Z]\.?", (a.get("family") or "").strip())) >= max(1, len(authors) // 2):
            def swap(a):
                g = (a.get("given") or "").split()
                fam = (a.get("family") or "").strip()
                if not g:
                    return fam
                return f"{g[0]}, {' '.join(g[1:] + [fam]).strip()}"

            fixed = "; ".join(swap(a) for a in authors[:3])
            add(Finding(L, "author-swapped", "error",
                        "Author family and given names look swapped",
                        "; ".join(f"{a.get('family')}, {a.get('given')}" for a in authors[:3]), fixed + " ..."))
        for a in authors:
            fam, giv = a.get("family", ""), a.get("given", "")
            if re.search(r"\b(LLC|Inc|Ltd|GmbH|L L C|Consortium|Group)\b", f"{fam} {giv}"):
                add(Finding(L, "author-corporate-split", "warning",
                            "A company or group name was split into family and given names",
                            f"{fam}, {giv}", "store it as a single (institutional) author"))
                break
    # duplicates
    seen_doi: dict[str, str] = {}
    seen_title: dict[str, str] = {}
    for r in ms.refs:
        L = label(r)
        doi = norm_doi(r.data.get("DOI", "")) if r.data.get("DOI") else ""
        t = norm_text(r.data.get("title", ""))
        if doi and doi in seen_doi:
            add(Finding(L, "duplicate", "error", f"Same DOI as {seen_doi[doi]}", doi))
        elif t and t in seen_title:
            add(Finding(L, "duplicate", "error", f"Same title as {seen_title[t]}"))
        if doi:
            seen_doi.setdefault(doi, L)
        if t:
            seen_title.setdefault(t, L)
    for mo in ms.manual_overrides:
        add(Finding("(citation)", "manual-override", "warning",
                    "A citation was typed over by hand; it will not update with the style", mo))
    if library is not None:
        cited_dois = {norm_doi(r.data["DOI"]) for r in ms.refs if r.data.get("DOI")}
        cited_titles = {norm_text(r.data.get("title", "")) for r in ms.refs}
        for r in library:
            doi = norm_doi(r.data["DOI"]) if r.data.get("DOI") else ""
            if (doi and doi in cited_dois) or norm_text(r.data.get("title", "")) in cited_titles:
                continue
            add(Finding(label(r), "uncited", "warning",
                        "In your reference library export but never cited in the manuscript", r.source))
        lib_dois = {norm_doi(r.data["DOI"]) for r in library if r.data.get("DOI")}
        lib_titles = {norm_text(r.data.get("title", "")) for r in library}
        for r in ms.refs:
            doi = norm_doi(r.data["DOI"]) if r.data.get("DOI") else ""
            if not ((doi and doi in lib_dois) or norm_text(r.data.get("title", "")) in lib_titles):
                add(Finding(label(r), "not-in-library", "info",
                            "Cited in the manuscript but not in the library export you provided"))
    return out


# ---------------------------------------------------------------- verify (network)
Fetch = Callable[[str], "dict | None"]


def http_fetch(email: str = "") -> Fetch:
    import requests

    ua = f"SubmitReady/{VERSION} (https://github.com/MohamedElsisii/submitready" + (f"; mailto:{email})" if email else ")")
    session = requests.Session()
    session.headers["User-Agent"] = ua

    def fetch(url: str):
        for attempt in range(3):
            try:
                r = session.get(url, timeout=20)
            except requests.RequestException:
                time.sleep(1 + attempt)
                continue
            if r.status_code == 404:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 + 2 * attempt)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Could not reach {url.split('/')[2]}")

    return fetch


def _crossref_fields(m: dict) -> dict:
    def first(x):
        return (x or [""])[0]

    years = []
    for k in ("issued", "published-print", "published-online", "published", "journal-issue"):
        src = m.get(k) or {}
        if k == "journal-issue":
            src = src.get("published-print") or src.get("published-online") or {}
        dp = (src.get("date-parts") or [[None]])
        if dp and dp[0] and dp[0][0]:
            years.append(str(dp[0][0]))
    authors = m.get("author") or []
    return {
        "title": first(m.get("title")),
        "year": years[0] if years else "",
        "years": sorted(set(years)),
        "authors": [norm_text(a.get("family") or a.get("name") or "") for a in authors[:5]],
        "volume": m.get("volume", ""),
        "issue": m.get("issue", ""),
        "page": m.get("page", "") or m.get("article-number", ""),
        "journal": first(m.get("container-title")),
        "first_author": (m.get("author") or [{}])[0].get("family", "") or (m.get("author") or [{}])[0].get("name", ""),
        "type": m.get("type", ""),
    }


def _first_page(p: str) -> str:
    return re.split(r"[-\u2013]", str(p or ""))[0].strip().lower()


def verify(ms: ManuscriptRefs, fetch: Fetch, pause: float = 0.2) -> tuple[list[Finding], dict]:
    out: list[Finding] = []
    stats = {"checked": 0, "matched": 0, "not_found": 0, "no_doi_found_by_title": 0}
    for r in ms.refs:
        d, L = r.data, label(r)
        doi = norm_doi(d.get("DOI", "")) if d.get("DOI") else ""
        stats["checked"] += 1
        msg = None
        if doi and DOI_RE.match(doi):
            res = fetch(f"https://api.crossref.org/works/{doi}")
            msg = (res or {}).get("message")
            if msg is None:
                out.append(Finding(L, "doi-not-found", "error", "Crossref does not know this DOI", doi))
                stats["not_found"] += 1
        else:
            title = d.get("title", "")
            if title:
                res = fetch("https://api.crossref.org/works?rows=3&query.bibliographic="
                            + _q(f"{title} {year(d)}"))
                items = ((res or {}).get("message") or {}).get("items") or []
                best = max(items, key=lambda m: similar(title, (m.get("title") or [""])[0]), default=None)
                if best and similar(title, (best.get("title") or [""])[0]) >= 0.92:
                    msg = best
                    stats["no_doi_found_by_title"] += 1
                    out.append(Finding(L, "doi-found", "info", "Found the DOI by title; add it to the reference",
                                       "", best.get("DOI", "")))
                else:
                    pm = _pubmed_by_title(title, fetch)
                    if pm:
                        out.append(Finding(L, "pubmed-found", "info", "Not matched in Crossref; found in PubMed",
                                           "", pm))
                    else:
                        out.append(Finding(L, "not-found", "warning",
                                           "Could not find this reference in Crossref or PubMed; check it by hand"))
                        stats["not_found"] += 1
        if msg:
            cr = _crossref_fields(msg)
            stats["matched"] += 1
            checks = [
                ("title", d.get("title", ""), cr["title"], titles_match),
                # online-first and print years often differ by one; either is correct
                ("year", year(d), "/".join(cr["years"]) or cr["year"],
                 lambda a, b: not a or not b or a in b.split("/")),
                ("volume", str(d.get("volume", "")), cr["volume"],
                 lambda a, b: not a or not b or norm_text(a) == norm_text(b)),
                ("issue", str(d.get("issue", "")), cr["issue"],
                 lambda a, b: not a or not b or norm_text(a) == norm_text(b)),
                ("first page", _first_page(d.get("page", "")), _first_page(cr["page"]),
                 lambda a, b: not a or not b or a == b),
                # consortia are often listed first in Crossref; accept a match among the first authors
                ("first author", ((d.get("author") or [{}])[0].get("family") or ""), cr["first_author"],
                 lambda a, b, au=cr["authors"]: not a or not b or norm_text(a) == norm_text(b) or norm_text(a) in au),
            ]
            for name, mine, theirs, ok in checks:
                if not ok(mine, theirs):
                    out.append(Finding(L, f"mismatch-{name.replace(' ', '-')}", "warning",
                                       f"{name.capitalize()} differs from Crossref", str(mine), str(theirs)))
                elif not mine and theirs and name in ("volume", "issue", "first page"):
                    out.append(Finding(L, f"fill-{name.replace(' ', '-')}", "info",
                                       f"Missing {name}; Crossref has it", "", str(theirs)))
            for u in msg.get("updated-by") or []:
                if (u.get("type") or "").lower() in ("retraction", "withdrawal", "removal"):
                    out.append(Finding(L, "retracted", "error",
                                       f"This paper has a {u.get('type')} notice. Do not cite it as valid work",
                                       "", u.get("DOI", "")))
                elif (u.get("type") or "").lower() in ("expression_of_concern", "expression-of-concern"):
                    out.append(Finding(L, "expression-of-concern", "warning",
                                       "This paper has an expression of concern", "", u.get("DOI", "")))
            rel = msg.get("relation") or {}
            for p in rel.get("is-preprint-of") or []:
                out.append(Finding(L, "preprint-published", "warning",
                                   "You cite a preprint that is now published; cite the journal version",
                                   doi, p.get("id", "")))
        time.sleep(pause)
    return out, stats


def _q(s: str) -> str:
    from urllib.parse import quote

    return quote(s[:300])


def _pubmed_by_title(title: str, fetch: Fetch) -> str:
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    res = fetch(f"{base}esearch.fcgi?db=pubmed&retmode=json&retmax=1&term={_q(title)}[Title]")
    ids = ((res or {}).get("esearchresult") or {}).get("idlist") or []
    if not ids:
        return ""
    summ = fetch(f"{base}esummary.fcgi?db=pubmed&retmode=json&id={ids[0]}")
    rec = ((summ or {}).get("result") or {}).get(ids[0]) or {}
    if similar(title, rec.get("title", "")) < 0.9:
        return ""
    doi = next((a["value"] for a in rec.get("articleids", []) if a.get("idtype") == "doi"), "")
    return f"PMID {ids[0]}" + (f", DOI {doi}" if doi else "")


# ---------------------------------------------------------------- report
SEV_ORDER = {"error": 0, "warning": 1, "info": 2}
SEV_ICON = {"error": "ERROR", "warning": "WARNING", "info": "INFO"}


def report_markdown(title: str, ms: ManuscriptRefs, findings: list[Finding], stats: dict | None = None) -> str:
    n: dict = {}
    for f in findings:
        n[f.severity] = n.get(f.severity, 0) + 1
    affected = len({f.ref for f in findings if f.severity != "info"})
    lines = [
        f"# SubmitReady reference report: {title}",
        "",
        f"- References: **{len(ms.refs)}**" + (f" (from {ms.citations} live citations)" if ms.citations else ""),
        f"- Findings: {n.get('error', 0)} errors, {n.get('warning', 0)} warnings, {n.get('info', 0)} notes",
        f"- References needing attention: {affected} of {len(ms.refs)}",
    ]
    if stats:
        lines.append(f"- Online check: {stats['matched']} matched in Crossref, {stats['not_found']} not found")
    lines += ["", "Nothing was changed. Each suggestion is for you to accept or reject in your reference manager.", ""]
    for sev in ("error", "warning", "info"):
        group = [f for f in findings if f.severity == sev]
        if not group:
            continue
        lines += [f"## {SEV_ICON[sev]} ({len(group)})", "", "| Reference | Problem | Found | Suggested |", "| --- | --- | --- | --- |"]
        for f in sorted(group, key=lambda f: (f.code, f.ref)):
            esc = lambda s: (s or "").replace("|", "\\|").replace("\n", " ")[:120]
            lines.append(f"| {esc(f.ref)} | {esc(f.message)} | {esc(f.found)} | {esc(f.suggested)} |")
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    # Windows terminals and pipes default to a legacy code page; manuscripts contain Unicode.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="SubmitReady references")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("audit", "verify"):
        p = sub.add_parser(name)
        p.add_argument("source", type=Path, help=".docx with live citations, or .ris/.bib/.json")
        p.add_argument("--library", type=Path, help="reference-manager export to compare against")
        p.add_argument("--report", type=Path)
        p.add_argument("--json", action="store_true")
        if name == "verify":
            p.add_argument("--email", default="", help="contact email for Crossref's polite pool")
    args = ap.parse_args(argv)

    ms = load(args.source)
    library = load(args.library).refs if args.library else None
    findings = audit(ms, library)
    stats = None
    if args.cmd == "verify":
        try:
            more, stats = verify(ms, http_fetch(args.email))
        except Exception as e:  # network refused, offline, ...
            print(f"Online check failed: {e}. Showing offline audit only.", file=sys.stderr)
            more = []
        findings += more
    findings.sort(key=lambda f: (SEV_ORDER[f.severity], f.ref))
    md = report_markdown(args.source.name, ms, findings, stats)
    if args.report:
        args.report.write_text(md, encoding="utf-8")
    if args.json:
        print(json.dumps([f.__dict__ for f in findings], ensure_ascii=False, indent=1))
    else:
        print(md)
    return 2 if any(f.severity == "error" for f in findings) else (1 if findings else 0)


if __name__ == "__main__":
    sys.exit(main())
