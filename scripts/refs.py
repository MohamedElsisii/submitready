#!/usr/bin/env python3
"""SubmitReady references: read, audit and verify a manuscript's references.

Reads references from:
  * a Word manuscript with live Mendeley Cite, Zotero or EndNote citations (.docx), or a typed reference list
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
DOI_PREFIX_RE = re.compile(r"^\s*doi\s*:?\s*(10\.\S+)$", re.I)     # "doi:10.1161/..." or "DOI 10.1161/..."


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
    library: list[Ref] | None = None          # full library when the source carries one (.tex + .bib)
    missing_keys: list[str] = field(default_factory=list)  # cited keys with no library entry
    plain: bool = False                       # references typed as plain text (no reference manager)
    body_text: str = ""                       # manuscript text before the reference list (plain mode)
    manager: str = ""                         # "Mendeley", "Zotero", "EndNote" or "" (typed or a file)
    text_units: list[str] = field(default_factory=list)  # paragraphs outside the reference list, live citations as a token
    printed: list[str] = field(default_factory=list)     # the reference list as Word shows it (live lists only)


# ---------------------------------------------------------------- reference managers
MANAGER_STEPS = {
    "Mendeley": {
        "library": "your Mendeley library",
        "style": "In the Mendeley Cite panel in Word, change the citation style to \u201c{style}\u201d.",
        "refresh": "refresh the citations in the Mendeley Cite panel",
        "plain": "",
    },
    "Zotero": {
        "library": "your Zotero library",
        "style": "In Word's Zotero tab, click Document Preferences and choose \u201c{style}\u201d "
                 "(if it is not listed, add it in Zotero from the Zotero Style Repository).",
        "refresh": "click Refresh in Word's Zotero tab",
        "plain": "On the copy you upload, click Unlink Citations in Word's Zotero tab; keep your working file linked.",
    },
    "EndNote": {
        "library": "your EndNote library",
        "style": "In Word's EndNote tab, choose the style for {journal} in the Style list. EndNote uses its own style files: "
                 "if it is not listed, download it from EndNote's output styles page and open it in EndNote.",
        "refresh": "click Update Citations and Bibliography in Word's EndNote tab",
        "plain": "On the copy you upload, choose Convert Citations and Bibliography > Convert to Plain Text in Word's "
                 "EndNote tab; keep your working file linked.",
    },
    "": {
        "library": "your reference manager",
        "style": "Choose \u201c{style}\u201d in your reference manager.",
        "refresh": "refresh the citations in Word",
        "plain": "",
    },
}


def detect_manager(document_xml: str) -> str:
    """Which reference manager inserted the live citations: Mendeley, Zotero, EndNote, or ''."""
    found = []
    if "MENDELEY_CITATION" in document_xml:
        found.append("Mendeley")
    if "ZOTERO_ITEM" in document_xml:
        found.append("Zotero")
    if "EN.CITE" in document_xml:
        found.append("EndNote")
    return found[0] if len(found) == 1 else (" and ".join(found) if found else "")


def manager_steps(manager: str) -> dict:
    return MANAGER_STEPS.get(manager, MANAGER_STEPS[""])


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
    if out.citations == 0:
        fields = read_docx_fields(path)
        return fields if fields.citations else read_docx_plain(path)
    out.manager = "Mendeley"
    return out


def _fields(xml: bytes) -> list[tuple[str, str]]:
    """(instruction, visible result text) of every Word field, in document order.

    Fields can span many runs and can be nested; a nested field's text also counts in the outer result."""
    from lxml import etree
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    out: list[tuple[str, str]] = []
    stack: list[dict] = []
    for el in etree.fromstring(xml).iter(W + "fldChar", W + "instrText", W + "fldSimple", W + "t"):
        if el.tag == W + "fldSimple":
            out.append((el.get(W + "instr") or "", "".join(t.text or "" for t in el.iter(W + "t"))))
        elif el.tag == W + "instrText":
            if stack and not stack[-1]["sep"]:
                stack[-1]["instr"].append(el.text or "")
        elif el.tag == W + "t":
            if any(a.tag == W + "fldSimple" for a in el.iterancestors()):
                continue
            for f in stack:
                if f["sep"]:
                    f["res"].append(el.text or "")
        else:
            kind = el.get(W + "fldCharType")
            if kind == "begin":
                stack.append({"instr": [], "res": [], "sep": False})
                data = el.find(W + "fldData")
                if data is not None and (data.text or "").strip():
                    # EndNote keeps long citation records here, base64-encoded (EN.CITE.DATA)
                    stack[-1]["instr"].append("\x00FLDDATA" + re.sub(r"\s+", "", data.text) + "\x00")
            elif kind == "separate" and stack:
                stack[-1]["sep"] = True
            elif kind == "end" and stack:
                f = stack.pop()
                out.append(("".join(f["instr"]), "".join(f["res"])))
    return out


def _field_instructions(xml: bytes) -> list[str]:
    """The instruction text of every Word field, in document order."""
    return [i for i, _ in _fields(xml)]


def _same_citation_text(a: str, b: str) -> bool:
    """Equal after spacing differences: Word and Zotero do not always agree on spaces."""
    n = lambda s: re.sub(r"\s+", " ", s.replace("\u00a0", " ")).strip()
    return n(a) == n(b)


def _endnote_to_csl(rec) -> dict:
    """An EndNote <record> (as stored in EN.CITE fields) to CSL JSON."""
    def t(path):
        e = rec.find(path)
        return "".join(e.itertext()).strip() if e is not None else ""
    authors = []
    for a in rec.findall(".//contributors/authors/author"):
        name = "".join(a.itertext()).strip()
        fam, _, giv = name.partition(",")
        if giv:
            authors.append({"family": fam.strip(), "given": giv.strip()})
        elif re.fullmatch(r"(?:[A-Z][\w'\-]*\.?\s+){1,3}(?:[a-z]{1,4}\s+)*[A-Z][\w'\-]+", name):
            # EndNote reads "Donald M. Lloyd-Jones" (no comma) as given names, then the family name
            words = name.split()
            k = len(words) - 1
            while k > 1 and words[k - 1].lower() in PARTICLES:
                k -= 1
            authors.append({"family": " ".join(words[k:]), "given": " ".join(words[:k])})
        else:
            authors.append({"literal": fam.strip()})
    y = re.search(r"(19|20)\d{2}", t(".//dates/year"))
    d = {"type": "article-journal", "title": t(".//titles/title"),
         "container-title": t(".//periodical/full-title") or t(".//titles/secondary-title"),
         "volume": t(".//volume"), "issue": t(".//number"), "page": t(".//pages"),
         "DOI": strip_doi(t(".//electronic-resource-num")), "author": authors,
         "issued": {"date-parts": [[int(y.group(0))]]} if y else {}}
    return {k: v for k, v in d.items() if v not in ("", [], {})}


def read_docx_fields(path: Path) -> ManuscriptRefs:
    """Live Zotero (ZOTERO_ITEM CSL_CITATION, CSL JSON) and EndNote (EN.CITE, EndNote XML) citation fields."""
    from lxml import etree
    out = ManuscriptRefs()
    by_key: dict[str, Ref] = {}
    kinds: set[str] = set()

    def add(k, d, n):
        if k not in by_key:
            by_key[k] = Ref(k, d, f"{path.name}, citation {n}")
            out.refs.append(by_key[k])
        by_key[k].cited += 1

    for instr, visible in _fields(zipfile.ZipFile(path).read("word/document.xml")):
        z = re.match(r"\s*ADDIN ZOTERO_ITEM\s+CSL_CITATION\s*(\{.*\})\s*$", instr, re.S)
        if z:
            try:
                c = json.loads(z.group(1))
            except ValueError:
                continue
            out.citations += 1
            kinds.add("Zotero")
            props = c.get("properties") or {}
            # formattedCitation is RTF or HTML and never matches the page text, so it is not compared.
            # Zotero marks a kept hand edit with dontUpdate; an edit made after the last refresh shows
            # as page text that differs from plainCitation.
            plain = props.get("plainCitation") or ""
            if props.get("dontUpdate"):
                out.manual_overrides.append(f"{plain!r} is set to keep a hand edit, so Zotero will not update it")
            elif plain and visible and not _same_citation_text(plain, visible):
                out.manual_overrides.append(f"{plain} edited by hand as {visible!r}")
            for item in c.get("citationItems", []):
                d = item.get("itemData") or {}
                add(str(item.get("id") or d.get("id") or d.get("DOI") or d.get("title", "")), d, out.citations)
            continue
        xml_text = None
        e = re.search(r"ADDIN EN\.CITE\s*(<EndNote>.*</EndNote>)", instr, re.S)
        if e:
            xml_text = e.group(1)
        elif "ADDIN EN.CITE" in instr:
            blob = re.search(r"\x00FLDDATA([A-Za-z0-9+/=]+)\x00", instr)
            if blob:
                raw = base64.b64decode(blob.group(1) + "=" * (-len(blob.group(1)) % 4))
                for enc in ("utf-8", "utf-16"):
                    try:
                        dec = raw.decode(enc)
                    except UnicodeDecodeError:
                        continue
                    m = re.search(r"<EndNote>.*</EndNote>", dec, re.S)
                    if m:
                        xml_text = m.group(0)
                        break
        if xml_text:
            try:
                x = etree.fromstring(xml_text.encode("utf-8"))
            except etree.XMLSyntaxError:
                continue
            kinds.add("EndNote")
            out.citations += 1
            for cite in x.findall("Cite"):
                rec = cite.find("record")
                d = _endnote_to_csl(rec) if rec is not None else {}
                if not d.get("author") and cite.findtext("Author"):
                    d["author"] = [{"family": cite.findtext("Author").strip()}]
                if not d.get("issued") and (cite.findtext("Year") or "").strip().isdigit():
                    d["issued"] = {"date-parts": [[int(cite.findtext("Year").strip())]]}
                add(cite.findtext("RecNum") or d.get("DOI") or d.get("title", ""), d, out.citations)
    out.manager = " and ".join(sorted(kinds))
    return out


def read_endnote_xml(path: Path) -> list[Ref]:
    """An EndNote library exported as XML (File > Export > XML)."""
    from lxml import etree
    root = etree.parse(str(path)).getroot()
    out = []
    for n, rec in enumerate(root.iter("record"), 1):
        d = _endnote_to_csl(rec)
        out.append(Ref(rec.findtext("rec-number") or f"rec{n}", d, f"{path.name}, record {n}"))
    return out


PLAIN_DOI = re.compile(r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?(10\.\d{4,9}/[^\s,;]+)", re.I)


def parse_plain(text: str) -> dict:
    """Best-effort fields from one typed reference (author-year or numbered styles)."""
    t = re.sub(r"^\s*(\[\d+\]|\d+\.)\s*", "", text)
    d: dict = {"_raw": text, "_plain": True}
    m = PLAIN_DOI.search(t)
    if m:
        d["DOI"] = m.group(1).rstrip(".")
    # "(2009)" in author-date styles; "Journal. 2009 Jan;12(3):45" in Vancouver; else the first year-like number
    ym = (re.search(r"\((\d{4})([a-z]?)\)", t)
          or re.search(r"(?<![\d/.])((?:19|20)\d{2})([a-z]?)(?:\s+[A-Z][a-z]{2,8}(?:\s+\d{1,2})?)?\s*[;:]", t)
          or re.search(r"(?<![\d/.])((?:19|20)\d{2})([a-z]?)(?![\d/])", t))
    if ym:
        d["issued"] = {"date-parts": [[int(ym.group(1))]]}
        d["_year_label"] = ym.group(1) + ym.group(2)
    # family name = everything before the initials: "Roca Pereira JF," "van Dijk MC," "Smith AB,"
    # "Agapow, P.-M., and Burt, A. (2001)": family name, comma, then initials with full stops
    fm = re.match(r"\s*([A-Z\u00C0-\u017F][^,()]{0,40}?),\s+(?:[A-Z\u00C0-\u00DE]\.[\s\-]?){1,4}", t[:120])
    if not fm:
        fm = re.match(r"\s*(.+?)\s+[A-Z\u00C0-\u00DE]{1,4}(?:-[A-Z\u00C0-\u00DE]{1,2})?\.?(?:,|\s*\(|\s+et al|\.\s)", t[:120])
    if not fm:
        fm = re.match(r"\s*([A-Z\u00C0-\u017F][\w\u00C0-\u017F'\-]+)", t)
    # an organisation as author: "R Core Team (2023)", "IUCN (2025)": no comma and no initials before the year
    org = re.match(r"\s*([^,()]+?)\s*\((?:19|20)\d{2}[a-z]?\)", t)
    if org and not re.search(r"\s[A-Z\u00C0-\u00DE]{1,3}\.?$", org.group(1).strip()):
        d["author"] = [{"literal": org.group(1).strip(), "family": org.group(1).strip()}]
    elif fm:
        d["author"] = [{"family": fm.group(1).strip(" ,")}]
    # Vancouver: "Jaffe ES, Smith A. Title. Journal. 2009": the authors end with initials and a full stop
    vanc = re.match(r"\s*((?:[^.,;()]+?\s[A-Z\u00C0-\u00DE]{1,4},\s*)*[^.,;()]+?\s[A-Z\u00C0-\u00DE]{1,4}(?:,?\s*et al)?)\.\s+(\S.*)", t)
    if ym and re.match(r"\(\d{4}", t[ym.start():]):
        rest = t[ym.end():].strip(" .")
        d["title"] = re.split(r"(?<=[a-z0-9\)])\.\s", rest, maxsplit=1)[0].strip()
    elif vanc:
        d["title"] = re.split(r"(?<=[\w\)\]}\'\"])[.?!]\s", vanc.group(2), maxsplit=1)[0].strip()
    else:
        parts = re.split(r"(?<=[a-z0-9\)])\.\s", t)
        d["title"] = parts[1].strip() if len(parts) > 1 else t
    return d


def _looks_like_reference(t: str) -> bool:
    """One typed reference: starts with a name or a number, has a year, and has a DOI, a journal-like
    volume(issue) or pages. Used only when the list has no "References" heading."""
    if len(t) < 40 or len(t) > 1500:
        return False
    starts = re.match(r"\s*(\[\d+\]|\d+\.|[A-Z\u00C0-\u017F][\w\u00C0-\u017F'\-]+,?\s+[A-Z\u00C0-\u00DE])", t)
    year = re.search(r"(?<!\d)(?:19|20)\d{2}[a-z]?(?!\d)", t)
    detail = PLAIN_DOI.search(t) or re.search(r"\d+\s*\(\d+[^)]*\)|\d+\s*[-\u2013]\s*\d+|;\s*\d+", t)
    return bool(starts and year and detail)


def _guess_reference_start(texts: list[str]) -> int | None:
    """Index of the first paragraph of a reference list at the end of the document, or None.

    At least 3 paragraphs in a row must look like references, counted back from the last text."""
    idx = [i for i, t in enumerate(texts) if t]
    first = None
    count = 0
    for i in reversed(idx):
        if not _looks_like_reference(texts[i]):
            break
        first, count = i, count + 1
    return first if count >= 3 else None


def read_docx_plain(path: Path) -> ManuscriptRefs:
    import docx as pydocx

    paras = [p for p in pydocx.Document(path).paragraphs]
    texts = [p.text.strip() for p in paras]
    start = None
    for i, t in enumerate(texts):
        if re.fullmatch(r"(?:\d+\.?\s*)?(references|bibliography|literature cited)\s*:?", t, re.I):
            start = i
    out = ManuscriptRefs(plain=True)
    first = start + 1 if start is not None else _guess_reference_start(texts)
    if first is None:
        return out
    # body text before the reference list, table cells included (reviews often cite inside tables)
    doc = pydocx.Document(path)
    ref_el = paras[start if start is not None else first]._p
    chunks = []
    for el in doc.element.body.iterchildren():
        if el is ref_el:
            break
        chunks.append("".join(t.text or "" for t in el.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")))
    out.body_text = " ".join(chunks)
    n = 0
    for p, t in zip(paras[first:], texts[first:]):
        if not t:
            continue
        if p.style.name.lower().startswith("heading"):
            break
        n += 1
        out.refs.append(Ref(f"ref-{n}", parse_plain(t), f"{path.name}, reference {n}"))
    return out


PARTICLE = r"(?:[Vv]an|[Vv]on|[Dd]e|[Dd]er|[Dd]a|[Dd]i|[Dd]u|[Dd]os|[Dd]el|[Ll]e|[Ll]a)"
NAME = (rf"(?:{PARTICLE}\s+)?(?:{PARTICLE}\s+)?(?![A-Z\u00C0-\u00DE\-]{{1,4}}\b)[A-Z\u00C0-\u017F][\w\u00C0-\u017F'\-]+"
        r"(?:\s+(?![A-Z\u00C0-\u00DE\-]{1,4}\b)[A-Z\u00C0-\u017F][\w\u00C0-\u017F'\-]+(?=\s+(?:et al|and|&|\(?(?:19|20)\d{2})))?")


def author_year_citations(text: str) -> list[tuple[str, str]]:
    """(family, year label) pairs from in-text author-year citations.

    Parenthetical: (Smith 2020; Smith and Jones 2019a, b; Smith et al., 2018, 2019)
    Narrative:     Smith et al. (2020), Smith and Jones (2019), Smith (2018)
    """
    yr = r"(?:19|20)\d{2}[a-z]?"
    item = re.compile(rf"^\s*(?:e\.g\.,?\s*|see\s+|cf\.\s*|reviewed in\s+)?({NAME})"
                      rf"(?:\s+et al\.?|\s+(?:and|&)\s+{NAME})?,?\s+((?:{yr})(?:\s*,\s*(?:{yr}|[a-z]))*)\s*$")
    # an acronym as author, alone before the year: (IUCN 2025), (WHO, 2021)
    acronym = re.compile(rf"^\s*(?:e\.g\.,?\s*|see\s+)?([A-Z][A-Z0-9&]{{1,9}}(?:\s+[A-Z][A-Z0-9&]{{1,9}})*),?\s+((?:{yr})(?:\s*,\s*(?:{yr}|[a-z]))*)\s*$")
    found = []
    for grp in re.findall(r"\(([^()]*?(?:19|20)\d{2}[a-z]?[^()]*?)\)", text):
        for part in grp.split(";"):
            m = item.match(part) or acronym.match(part)
            if not m:
                continue
            fam, years = m.group(1), m.group(2)
            last = None
            for y in re.split(r"\s*,\s*", years):
                if re.fullmatch(yr, y):
                    last = y[:4]
                    found.append((fam, y))
                elif re.fullmatch(r"[a-z]", y) and last:
                    found.append((fam, last + y))
    for m in re.finditer(rf"({NAME})(?:\s+et al\.?|\s+(?:and|&)\s+{NAME})?\s+\(({yr})\)", text):
        found.append((m.group(1), m.group(2)))
    not_names = {"journal", "sciences", "science", "biology", "plant", "plants", "molecular", "research", "reports",
                 "letters", "physiology", "frontiers", "international", "nature", "cell", "table", "figure", "fig"}
    return [(f, y) for f, y in found if not ({w.lower() for w in f.split()} & not_names)]


def match_plain_citations(ms: ManuscriptRefs) -> list[Finding]:
    """Every in-text citation has a list entry, and every list entry is cited (author-year or numbered)."""
    out: list[Finding] = []
    text = ms.body_text
    nums = re.findall(r"\[(\d+(?:\s*[-\u2013,]\s*\d+)*)\]", text)
    if nums and not author_year_citations(text):
        cited = set()
        for grp in nums:
            for part in re.split(r"\s*,\s*", grp):
                a = re.split(r"\s*[-\u2013]\s*", part)
                if len(a) == 2 and a[0].isdigit() and a[1].isdigit():
                    cited.update(range(int(a[0]), int(a[1]) + 1))
                elif a[0].isdigit():
                    cited.add(int(a[0]))
        for k in sorted(c for c in cited if c > len(ms.refs)):
            out.append(Finding(f"[{k}]", "cited-not-listed", "error", f"Cited as [{k}] but the list has only {len(ms.refs)} entries"))
        for i, r in enumerate(ms.refs, 1):
            if i not in cited:
                out.append(Finding(label(r), "listed-not-cited", "warning", f"Reference {i} is never cited in the text"))
        return out
    def key(fam, y):
        # match on the last word of the family name: "Roca Pereira" ~ "Pereira", "van Dijk" ~ "Dijk"
        return (norm_text(fam).split(" ")[-1] if fam else "", y.lower())
    listed = {}
    for r in ms.refs:
        fam = ((r.data.get("author") or [{}])[0].get("family") or "")
        y = r.data.get("_year_label", "")
        listed.setdefault(key(fam, y), r)
    cited = {}
    for fam, y in author_year_citations(text):
        cited.setdefault(key(fam, y), f"{fam} {y}")
    stop = {"in", "the", "and", "since", "from", "until", "by", "after", "before", "during", "between", "of", "to",
            "for", "on", "at", "fig", "figure", "table", "et", "al"}
    for k, shown in sorted(cited.items()):
        if k in listed or k[0] in stop:
            continue
        near = [lk for lk in listed if lk[0] == k[0]]
        hint = ("; the list has " + ", ".join(f"{listed[n].data['author'][0]['family']} {n[1]}" for n in near[:3])) if near else ""
        if re.fullmatch(r"[A-Z][A-Z0-9&]+(?:\s+[A-Z][A-Z0-9&]+)*\s+\S+", shown):
            # an all-capitals name may be a dataset, tool or agency rather than a paper
            out.append(Finding(shown, "cited-not-listed", "warning",
                               "Written like a citation but not in the reference list; add a reference if it is one" + hint))
        else:
            out.append(Finding(shown, "cited-not-listed", "error", "Cited in the text but not in the reference list" + hint))
    for k, r in listed.items():
        if k not in cited:
            out.append(Finding(label(r), "listed-not-cited", "warning", "In the reference list but never cited in the text"))
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
    last = None   # (field, key in cur) of the last text tag, for lines that wrap
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^([A-Z][A-Z0-9])  - ?(.*)$", line)
        if not m:
            # Mendeley and Zotero wrap long titles and abstracts onto untagged lines
            if cur is not None and last and line.strip() and last in cur:
                cur[last] = f"{cur[last]} {line.strip()}"
            continue
        k, v = m.group(1), m.group(2).strip()
        last = {"TI": "title", "T1": "title", "T2": "container-title", "JO": "container-title",
                "JF": "container-title", "BT": "container-title"}.get(k)
        if k in ("T2", "JO", "JF", "BT") and cur is not None and "container-title" in cur:
            last = None   # only the first container tag is kept, so do not extend it
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
        elif k == "Y2" and v:
            cur["accessed"] = {"raw": v}
        elif k == "PB":
            cur["publisher"] = v
        elif k == "SN":
            cur["ISSN"] = v
    for r in refs:
        sp, ep = r.data.pop("_sp", ""), r.data.pop("_ep", "")
        if sp:
            r.data["page"] = sp if not ep or ep == sp else f"{sp}-{ep}"
    return refs


BIB_ACCENTS = {"'": "\u0301", "`": "\u0300", "^": "\u0302", '"': "\u0308", "~": "\u0303", "c": "\u0327", "=": "\u0304"}


def _bib_text(value: str) -> str:
    """A BibTeX value as plain text: {\\textit{Arabidopsis}} -> Arabidopsis, {\\'e} -> \u00e9, -- -> \u2013."""
    import unicodedata
    v = re.sub(r"\{?\\([\'`^\"~=]|c\s)\s*\{?([A-Za-z])\}?\}?", lambda m: unicodedata.normalize(
        "NFC", m.group(2) + BIB_ACCENTS[m.group(1).strip()]), value)
    v = re.sub(r"\\(?:textit|textbf|emph|textsc|textrm|mathrm|mbox|textsuperscript|textsubscript)\s*\{", "{", v)
    v = re.sub(r"\\(?:&|%|_|\$|#)", lambda m: m.group(0)[1:], v)
    v = v.replace("---", "\u2014").replace("--", "\u2013")
    return " ".join(v.replace("{", "").replace("}", "").split())


def _bib_entries(text: str):
    """Yield (type, key, {field: value}) from BibTeX text; nested braces and quotes respected."""
    i = 0
    while True:
        m = re.compile(r"@(\w+)\s*[{(]").search(text, i)
        if not m:
            return
        typ = m.group(1).lower()
        j = m.end()
        if typ in ("comment", "preamble", "string"):
            i = j
            continue
        k = text.find(",", j)
        if k < 0:
            return
        key = text[j:k].strip()
        pos, fields = k + 1, {}
        while pos < len(text):
            fm = re.compile(r"\s*(\w[\w-]*)\s*=\s*").match(text, pos)
            if not fm:
                close = re.compile(r"\s*[})]").match(text, pos)
                pos = close.end() if close else pos + 1
                break
            name, pos = fm.group(1).lower(), fm.end()
            if pos < len(text) and text[pos] == "{":
                depth, start = 0, pos
                while pos < len(text):
                    if text[pos] == "{":
                        depth += 1
                    elif text[pos] == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    pos += 1
                value = text[start + 1:pos]
                pos += 1
            elif pos < len(text) and text[pos] == '"':
                end = pos + 1
                while end < len(text) and not (text[end] == '"' and text[end - 1] != "\\"):
                    end += 1
                value = text[pos + 1:end]
                pos = end + 1
            else:
                vm = re.compile(r"[^,}\n]+").match(text, pos)
                value = vm.group(0).strip() if vm else ""
                pos = vm.end() if vm else pos
            fields[name] = _bib_text(value)
            cm = re.compile(r"\s*,").match(text, pos)
            if cm:
                pos = cm.end()
        yield typ, key, fields
        i = pos


def _bib_to_csl(typ: str, f: dict) -> dict:
    d = {"type": {"article": "article-journal", "inproceedings": "paper-conference", "incollection": "chapter",
                  "inbook": "chapter", "book": "book", "phdthesis": "thesis", "mastersthesis": "thesis",
                  "techreport": "report", "online": "webpage", "software": "software", "misc": "generic"}.get(typ, "generic")}
    if "title" in f:
        d["title"] = f["title"]
    if f.get("journal") or f.get("journaltitle") or f.get("booktitle"):
        d["container-title"] = f.get("journal") or f.get("journaltitle") or f.get("booktitle")
    for src, dst in (("volume", "volume"), ("number", "issue"), ("doi", "DOI"), ("url", "URL"), ("publisher", "publisher")):
        if f.get(src):
            d[dst] = f[src]
    if f.get("pages"):
        d["page"] = re.sub(r"\s*-{1,3}\s*", "-", f["pages"])
    y = re.match(r"(\d{4})", f.get("year") or f.get("date") or "")
    if y:
        d["issued"] = {"date-parts": [[int(y.group(1))]]}
    if f.get("author"):
        d["author"] = [_ris_name(a) for a in re.split(r"\s+and\s+", f["author"])]
    return d


def read_bib(path: Path) -> list[Ref]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return [Ref(key, _bib_to_csl(typ, f), f"{path.name}, entry {i}")
            for i, (typ, key, f) in enumerate(_bib_entries(text), 1)]


def read_tex(path: Path) -> "ManuscriptRefs":
    """LaTeX: the cited keys, resolved in the .bib files the manuscript names."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import latex

    lib = [r for b in latex.bib_files(path) for r in read_bib(b)]
    full = latex.read_project(path)
    # PLOS, Bioinformatics and others ask for the reference list pasted into the .tex file
    # (\begin{thebibliography} ... \bibitem{key} ...): those entries count like .bib entries
    inline = [Ref(k, parse_plain(latex.detex(t)), f"{path.name}, \\bibitem{{{k}}}") for k, t in latex.bibitems(full)]
    by_key = {r.key: r for r in lib}
    for r in inline:
        by_key.setdefault(r.key, r)
    ms = ManuscriptRefs(library=lib)
    ms.bib_named = [n.strip() + ("" if n.strip().endswith(".bib") else ".bib")
                    for b in re.findall(r"\\(?:bibliography|addbibresource)\s*\{([^}]*)\}", latex.strip_comments(full))
                    for n in b.split(",") if n.strip()]
    _, body = latex.split_document(full)
    for m in latex.CITE_RE.finditer(body):
        ms.citations += 1
        for k in m.group(1).split(","):
            k = k.strip()
            if not k:
                continue
            if k not in by_key:
                if k not in ms.missing_keys:
                    ms.missing_keys.append(k)
                continue
            r = by_key[k]
            if r.cited == 0:
                ms.refs.append(r)
            r.cited += 1
    if not ms.citations and not inline:
        return _tex_typed(body, path) or ms
    return ms


def _tex_typed(body: str, path: Path) -> "ManuscriptRefs | None":
    """References typed by hand in LaTeX (no \\cite, no \\bibitem): read the list under a References heading."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import latex

    m = re.search(r"\\(?:section|chapter)\*?\s*\{\s*(references|bibliography|literature cited)\s*\}", body, re.I)
    if not m:
        return None
    tail = body[m.end():]
    nxt = re.search(r"\\(?:section|chapter)\*?\s*\{|\\end\{document\}", tail)
    block = tail[:nxt.start()] if nxt else tail
    entries = [latex.detex(e) for e in re.split(r"\n\s*\n|\\item\b|\\par\b|\\\\", block)]
    # under a References heading every entry with a year counts (software and websites have no pages)
    entries = [e for e in entries if len(e) > 20 and re.search(r"(?<!\d)(?:19|20)\d{2}[a-z]?(?!\d)", e)]
    if len(entries) < 3:
        return None
    out = ManuscriptRefs(plain=True)
    out.body_text = latex.detex(body[:m.start()])
    for n, e in enumerate(entries, 1):
        out.refs.append(Ref(f"ref-{n}", parse_plain(e), f"{path.name}, reference {n}"))
    return out


def read_csl_json(path: Path) -> list[Ref]:
    items = json.loads(path.read_text(encoding="utf-8"))
    return [Ref(str(d.get("id", i)), d, f"{path.name}, entry {i}") for i, d in enumerate(items, 1)]


def load(path: Path) -> ManuscriptRefs:
    suf = path.suffix.lower()
    if suf == ".docx":
        ms = read_docx(path)
        try:
            import lock  # same folder; paragraphs with live citations replaced by a token, reference list left out
            ms.text_units = [u.text for u in lock.extract(path)["units"]]
        except Exception:  # the reference checks still run without it
            ms.text_units = []
        try:
            ms.printed = printed_bibliography(path)
        except Exception:
            ms.printed = []
        return ms
    if suf == ".tex":
        return read_tex(path)
    reader = {".ris": read_ris, ".bib": read_bib, ".json": read_csl_json, ".xml": read_endnote_xml}.get(suf)
    if not reader:
        raise SystemExit(f"Unsupported file type: {path.name}")
    return ManuscriptRefs(refs=reader(path))


# ---------------------------------------------------------------- helpers
def printed_bibliography(path: Path) -> list[str]:
    """The reference list as Word shows it (inside the reference manager's bibliography field or control)."""
    import zipfile
    from lxml import etree
    import lock
    out = []
    with zipfile.ZipFile(path) as z:
        root = etree.fromstring(z.read("word/document.xml"))
    ex = lock.Extractor()
    for p in lock._paragraphs(root, "body"):
        in_bib = ex.in_bibliography(p)
        text, _ = ex.paragraph_text(p)
        if in_bib or (ex.in_bibliography(p) and not text.strip()):
            v = " ".join(lock._visible_text(p).split())
            if v:
                out.append(v)
    return out


def _printed_times(r: Ref, printed: list[str]) -> int:
    """How many printed entries show this reference (by DOI, else by the start of its title)."""
    doi = norm_doi(r.data.get("DOI", "")) if r.data.get("DOI") else ""
    by_doi = sum(doi in p.lower() for p in printed) if doi else 0
    t = norm_text(r.data.get("title", ""))[:40]
    by_title = sum(t in norm_text(p) for p in printed) if len(t) >= 15 else 0
    return max(by_doi, by_title)   # one copy may print without its DOI


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
    m = DOI_URL_RE.match(doi) or DOI_PREFIX_RE.match(doi)
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


def journals_match(a: str, b: str) -> bool:
    """'N Engl J Med' matches 'New England Journal of Medicine': each abbreviated word starts a word, in order."""
    a, b = norm_text(a), norm_text(b)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    short, full = (a, b) if len(a) <= len(b) else (b, a)
    words = iter(full.split())
    return all(any(w.startswith(t) for w in words) for t in short.split())


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm_text(a), norm_text(b)).ratio()


# ---------------------------------------------------------------- audit
JOURNAL_TYPES = {"article-journal"}


PREPRINT_SERVERS = re.compile(r"\b(bioRxiv|medRxiv|arXiv|ChemRxiv|Research Square|SSRN|Preprints|Authorea|"
                              r"OSF Preprints|PsyArXiv|SocArXiv|EarthArXiv|Qeios|TechRxiv)\b", re.I)
# DOI prefixes used only for preprints: bioRxiv/medRxiv, Research Square, Authorea, arXiv, SSRN, Preprints.org
PREPRINT_DOI = re.compile(r"^10\.(1101/\d{4}\.\d{2}\.\d{2}|21203/rs\.|22541/au\.|48550/arxiv|2139/ssrn|20944/preprints)", re.I)


def is_preprint(d: dict) -> bool:
    return bool(d.get("type") == "posted-content" or d.get("subtype") == "preprint"
                or PREPRINT_SERVERS.search(d.get("container-title") or "")
                or PREPRINT_DOI.match(norm_doi(d.get("DOI") or "")))


def _page_key(p: str) -> str:
    """'e36905-e36905' and 'e36905' are the same page; so are '1–22' and '1-22'."""
    p = str(p or "").replace("\u2013", "-").replace(" ", "")
    m = re.fullmatch(r"(\S+?)-(\S+)", p)
    return m.group(1) if m and m.group(1) == m.group(2) else p


CITE_TOKEN = "\u27e6CITE\u27e7"   # as in lock.py


def typed_reference_numbers(ms: ManuscriptRefs) -> list[Finding]:
    """Reference numbers typed by hand in a manuscript whose citations are live.

    A reference-annotation list ("[6] Hasin Y, ... ** why it matters") or a number typed into a sentence
    does not renumber when the reference manager does. Each typed list number is checked against the
    first author of that entry in the generated list."""
    out: list[Finding] = []
    listed, checked, wrong = len(ms.refs), 0, 0
    for t in ms.text_units:
        m = re.match(r"\s*\[(\d{1,3})\]\s+([^\W\d][\w\u00C0-\u017F'\- ]{1,40}?)[,\s]", t)
        if re.match(r"\s*\[\d+\]", t) and re.search(r"\S {2,}\S|^\s*\[\d+\]\s+[\"'\d]|\[\d+\]\s+[a-z][\w.]*_[\w.]+", t):
            continue  # R console output ("[1] stats     graphics  utils"), not a citation
        if m:
            n = int(m.group(1))
            checked += 1
            if n > listed:
                wrong += 1
                out.append(Finding(f"[{n}] (typed)", "typed-number-missing", "error",
                                   f"Typed as reference [{n}], but the reference list has only {listed} entries", t[:80]))
                continue
            fam = norm_text(((ms.refs[n - 1].data.get("author") or [{}])[0].get("family") or
                             (ms.refs[n - 1].data.get("author") or [{}])[0].get("literal") or ""))
            last = fam.split(" ")[-1] if fam else ""
            if last and last not in norm_text(t[:80]):
                wrong += 1
                out.append(Finding(f"[{n}] (typed)", "typed-number-wrong", "error",
                                   f"Typed as reference [{n}], but reference {n} in the list is {label(ms.refs[n - 1])}. "
                                   "Typed numbers do not update when citations move", t[:80]))
            continue
        bare = re.sub(re.escape(CITE_TOKEN), " ", t)
        toks = bare.split()
        if toks and len(toks) <= 8 and sum(bool(re.fullmatch(r"[\d.,%()\[\]\u2013\-]+", x)) for x in toks) * 2 >= len(toks):
            continue  # a table cell of numbers ("160 [116]", "0 [0]"): data, not citations
        for hit in re.finditer(r"(?<![\w\[])\[(\d{1,3}(?:\s*[-\u2013,]\s*\d{1,3})*)\](?!\w)", bare):
            before = bare[:hit.start()].rstrip()
            if hit.group(1) == "0" or re.search(r"[\d%]$", before):
                continue  # "12 [3]" is a count with a percentage or range, not a citation
            out.append(Finding(f"[{hit.group(1)}] (typed)", "typed-citation", "warning",
                               "A citation number typed by hand among live citations; it will not update. "
                               "Insert it with your reference manager instead",
                               bare[max(0, hit.start() - 40):hit.end() + 10].strip()))
    if checked and not wrong:
        out.append(Finding("(typed numbers)", "typed-numbers-ok", "info",
                           f"{checked} reference numbers are typed by hand (for example a reference annotation list). "
                           "They match the list now; check them again after you add, remove or move a citation"))
    return out


UNCITED_LIST_MAX = 10


def compare_library(ms: ManuscriptRefs, library: list[Ref]) -> list[Finding]:
    """The manuscript against a reference-manager export.

    * library entries never cited: listed one by one only when there are few, as in an export of this
      paper's folder. A whole-library export has many on purpose, so they become one note.
    * a cited reference whose library entry has data the manuscript lacks: the citation is older than the
      library, and refreshing the citations in Word brings the fix in.
    """
    out: list[Finding] = []
    by_doi = {norm_doi(r.data["DOI"]): r for r in library if r.data.get("DOI")}
    by_title = {norm_text(r.data.get("title", "")): r for r in library if r.data.get("title")}
    used: set[int] = set()
    fields = (("title", "title"), ("volume", "volume"), ("issue", "issue"), ("page", "pages"), ("DOI", "DOI"))
    for r in ms.refs:
        d = r.data
        if d.get("_plain"):
            continue
        lib = (by_doi.get(norm_doi(d["DOI"])) if d.get("DOI") else None) or by_title.get(norm_text(d.get("title", "")))
        if lib is None:
            continue
        used.add(id(lib))
        newer, differ = [], []
        for k, name in fields:
            a, b = str(d.get(k) or "").strip(), str(lib.data.get(k) or "").strip()
            if k == "DOI":
                a, b = norm_doi(a), norm_doi(b)
            elif k == "page":
                a, b = _page_key(a), _page_key(b)
            if not b or norm_text(a) == norm_text(b):
                continue
            (newer if not a else differ).append(f"{name}: {b}" if not a else f"{name} '{a}' here, '{b}' in the library")
        if year(lib.data) and year(d) and year(lib.data) != year(d):
            differ.append(f"year {year(d)} here, {year(lib.data)} in the library")
        if newer:
            out.append(Finding(label(r), "library-newer", "info",
                               "Your library has data this citation lacks; refresh the citations in Word to bring it in",
                               "; ".join(newer)))
        if differ:
            out.append(Finding(label(r), "library-differs", "info",
                               "The citation and your library differ; check which is right, then refresh",
                               "; ".join(differ)))
    for r in library:
        if id(r) in used:
            continue
        doi = norm_doi(r.data["DOI"]) if r.data.get("DOI") else ""
        if doi and any(norm_doi(x.data.get("DOI", "")) == doi for x in ms.refs if x.data.get("DOI")):
            used.add(id(r))
        elif norm_text(r.data.get("title", "")) in {norm_text(x.data.get("title", "")) for x in ms.refs}:
            used.add(id(r))
    uncited = [r for r in library if id(r) not in used]
    if uncited and len(uncited) <= UNCITED_LIST_MAX and len(uncited) <= len(library) // 4 + 1:
        for r in uncited:
            out.append(Finding(label(r), "uncited", "warning",
                               "In your reference library export but never cited in the manuscript", r.source))
    elif uncited:
        out.append(Finding("(library)", "uncited-many", "info",
                           f"{len(uncited)} of {len(library)} library entries are not cited in this manuscript. "
                           "That is normal for a whole-library export, so they are not listed. To check for a "
                           "forgotten citation, export only this paper's folder or collection",
                           f"{len(library) - len(uncited)} of {len(library)} entries are cited"))
    return out


def audit(ms: ManuscriptRefs, library: list[Ref] | None = None) -> list[Finding]:
    out: list[Finding] = []
    add = out.append
    for r in ms.refs:
        d, L = r.data, label(r)
        if d.get("_plain"):
            doi = d.get("DOI", "")
            if doi and not DOI_RE.match(norm_doi(doi)):
                add(Finding(L, "doi-invalid", "error", "DOI does not look valid", doi))
            continue
        typ = d.get("type", "")
        doi = (d.get("DOI") or "").strip()
        if d.get("URL") and typ in ("webpage", "post-weblog", "software", "dataset", "post"):
            if not d.get("author") and not d.get("issued"):
                add(Finding(L, "web-no-date", "warning", "Web page or software with only a title and a link: add who made "
                            "it and the year (or version), so readers can find what you used", d.get("URL", "")))
        elif not d.get("author") and not d.get("issued") and not doi and not d.get("container-title"):
            # e.g. a PDF dropped into the library: title "nihms-731362 ( ref 10)", nothing else
            add(Finding(L, "placeholder-entry", "error",
                        "This entry is only a name, with no authors, year, journal or DOI. "
                        "Replace it with the real reference", d.get("title", "")))
            continue
        preprint = is_preprint(d)
        if doi:
            if DOI_URL_RE.match(doi):
                add(Finding(L, "doi-as-url", "warning", "DOI is stored as a web link", doi, strip_doi(doi)))
            elif DOI_PREFIX_RE.match(doi):
                add(Finding(L, "doi-prefix", "warning", "DOI is stored with a 'doi:' prefix; keep only the DOI itself",
                            doi, strip_doi(doi)))
            if not DOI_RE.match(norm_doi(doi)):
                add(Finding(L, "doi-invalid", "error", "DOI does not look valid", doi))
        elif typ in JOURNAL_TYPES or d.get("_ris_type") == "GEN":
            add(Finding(L, "doi-missing", "warning", "No DOI; verify will try to find it by title"))
        if typ == "generic":
            add(Finding(L, "type-generic", "warning",
                        "Reference type is 'generic'; styles may format it wrongly. Set the real type "
                        "(e.g. journal article) in your reference manager",
                        d.get("_ris_type", "generic")))
        if (typ in ("webpage", "post", "post-weblog") or (d.get("URL") and not doi and not d.get("container-title")
                                                          and typ in ("generic", "webpage", "software", "dataset"))) \
                and not d.get("accessed"):
            add(Finding(L, "no-access-date", "warning",
                        "Website without an access date; most journals ask for the date you viewed it "
                        "(set 'Accessed' in your reference manager)", d.get("URL", "")))
        if preprint:
            add(Finding(L, "preprint", "info",
                        "Preprint: check whether it is published now and cite the journal version if so "
                        "(verify checks this online)", d.get("container-title") or doi))
        if typ in JOURNAL_TYPES and not preprint:
            for f, name in (("container-title", "journal name"), ("volume", "volume"),
                            ("issued", "year"), ("author", "authors")):
                if not d.get(f):
                    add(Finding(L, f"missing-{f}", "warning", f"Missing {name}"))
            if not d.get("page"):
                add(Finding(L, "missing-page", "info", "Missing pages or article number"))
            if not d.get("issue"):
                add(Finding(L, "missing-issue", "info", "Missing issue"))
        vol = str(d.get("volume") or "")
        if vol and not re.fullmatch(r"[A-Za-z]?\d+[A-Za-z]?(\s*[-/\u2013]\s*\d+)?", vol):
            m = re.search(r"\d+", vol)
            add(Finding(L, "odd-volume", "warning", "Volume has extra text", vol, m.group(0) if m else ""))
        iss = str(d.get("issue") or "")
        if iss and not re.fullmatch(r"[A-Za-z]{0,2}\d+[A-Za-z]?(\s*[-/\u2013&]\s*\d+)?|S\d+|Suppl.*|W\d+", iss):
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
        if re.search(r"[\r\n\t]", title.strip()) or title != title.strip() and re.search(r"[\r\n]", title):
            # Mendeley Cite prints it as a no-break space, so it is a note, not a warning
            add(Finding(L, "title-linebreak", "info",
                        "Title has a hidden line break from a copy-paste. It usually prints as a space; "
                        "clean it once in your library to be safe", repr(title),
                        re.sub(r"\s+", " ", title).strip()))
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
                k = 0  # "van der Drift Koen M G" + "M": the particles belong to the family name
                while k < len(g) - 1 and g[k].lower() in PARTICLES:
                    k += 1
                return f"{' '.join(g[:k + 1])}, {' '.join(g[k + 1:] + [fam]).strip()}"

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
        t = norm_text(r.data.get("_raw") or r.data.get("title", ""))
        once = ms.printed and _printed_times(r, ms.printed) == 1
        if doi and doi in seen_doi:
            if once:   # two records of one paper (often from two EndNote libraries); the list shows it once
                add(Finding(L, "duplicate-record", "info", f"Same paper as {seen_doi[doi]}, inserted from two library "
                            "records. The reference list prints it once now; merge the two records in your library so "
                            "it stays that way", doi))
            else:
                add(Finding(L, "duplicate", "error", f"Same DOI as {seen_doi[doi]}", doi))
        elif t and t in seen_title:
            if once:
                add(Finding(L, "duplicate-record", "info", f"Same paper as {seen_title[t]}, inserted from two library "
                            "records. The reference list prints it once now; merge the two records in your library"))
            else:
                add(Finding(L, "duplicate", "error", f"Same title as {seen_title[t]}"))
        if doi:
            seen_doi.setdefault(doi, L)
        if t:
            seen_title.setdefault(t, L)
    if ms.plain and ms.body_text:
        out += match_plain_citations(ms)
    if ms.missing_keys and ms.library == [] and getattr(ms, "bib_named", []):
        # no .bib file at all: one clear finding instead of one per key
        add(Finding("(bibliography)", "bib-missing", "error",
                    f"The bibliography file {', '.join(ms.bib_named)} named in \\bibliography{{}} is not next to the "
                    f"manuscript, so all {len(ms.missing_keys)} cited keys will print as '?'. Put the .bib file in the "
                    "same folder and run again", ", ".join(ms.missing_keys[:5]) + (" ..." if len(ms.missing_keys) > 5 else "")))
    else:
        for k in ms.missing_keys:
            add(Finding(f"\\cite{{{k}}}", "missing-key", "error",
                        "Cited in the manuscript but not found in any .bib file or \\bibitem; it will print as '?'", k))
    for mo in ms.manual_overrides:
        add(Finding("(citation)", "manual-override", "warning",
                    "A citation was typed over by hand; it will not update with the style", mo))
    if library is not None:
        out += compare_library(ms, library)
        lib_dois = {norm_doi(r.data["DOI"]) for r in library if r.data.get("DOI")}
        lib_titles = {norm_text(r.data.get("title", "")) for r in library}
        for r in ms.refs:
            doi = norm_doi(r.data["DOI"]) if r.data.get("DOI") else ""
            if not ((doi and doi in lib_dois) or norm_text(r.data.get("title", "")) in lib_titles):
                add(Finding(label(r), "not-in-library", "info",
                            "Cited in the manuscript but not in the library export you provided"))
    if ms.citations and not ms.plain and ms.text_units:
        out += typed_reference_numbers(ms)
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


MOJIBAKE = re.compile("[\u00c2\u00c3\u00c5][\u0080-\u00bf]")


def unmangle(s: str) -> str:
    """Some publishers deposit UTF-8 read as Latin-1 ("Herrera-V\u00c3\u00a1squez" for "Herrera-V\u00e1squez"). Undo that."""
    if not s or not MOJIBAKE.search(s):
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


PARTICLES = {"van", "von", "de", "der", "den", "da", "di", "du", "dos", "das", "del", "della", "le", "la",
             "el", "al", "ten", "ter", "bin", "ibn", "st", "st."}


def family_core(name: str) -> str:
    """A family name without its particles: "van Verk" and "Verk", "EL Sabagh" and "Sabagh" are the same person."""
    words = [w for w in re.split(r"[\s]+", norm_text(name)) if w]
    while len(words) > 1 and words[0].lower() in PARTICLES:
        words = words[1:]
    return " ".join(words)


def _crossref_fields(m: dict) -> dict:
    def first(x):
        return unmangle((x or [""])[0])

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
        "authors": [norm_text(unmangle(a.get("family") or a.get("name") or "")) for a in authors[:5]],
        "volume": m.get("volume", ""),
        "issue": m.get("issue", ""),
        "page": m.get("page", "") or m.get("article-number", ""),
        "journal": first(m.get("container-title")),
        "first_author": unmangle((m.get("author") or [{}])[0].get("family", "") or (m.get("author") or [{}])[0].get("name", "")),
        "type": m.get("type", ""),
    }


def _first_page(p: str) -> str:
    return re.split(r"[-\u2013]", str(p or ""))[0].strip().lower()


def _last_page(p: str) -> str:
    """The end page written in full: '1343-72' gives '1372'. Empty for a single page."""
    parts = re.split(r"\s*[-\u2013]\s*", str(p or "").strip().lower())
    if len(parts) != 2 or not parts[1]:
        return ""
    a, b = parts
    if a.isdigit() and b.isdigit() and len(b) < len(a):
        b = a[: len(a) - len(b)] + b
    return "" if b == a else b


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
                fixed = _repair_doi(doi, fetch)
                if fixed:
                    out.append(Finding(L, "doi-junk-suffix", "error",
                                       "DOI has extra text copied from a web page; the corrected DOI exists", doi, fixed[0]))
                    msg = fixed[1]
                    doi = fixed[0]
                else:
                    out.append(Finding(L, "doi-not-found", "error", "Crossref does not know this DOI", doi))
                    stats["not_found"] += 1
        else:
            title = d.get("title", "")
            if title:
                query = d["_raw"] if d.get("_plain") else f"{title} {year(d)}"
                res = fetch("https://api.crossref.org/works?rows=5&query.bibliographic=" + _q(query))
                items = ((res or {}).get("message") or {}).get("items") or []

                def score(m):
                    ct = (m.get("title") or [""])[0]
                    if d.get("_plain") and norm_text(ct) and norm_text(ct) in norm_text(d["_raw"]):
                        return 1.0
                    return similar(title, ct)

                my_year = year(d)
                my_first = norm_text(((d.get("author") or [{}])[0].get("family") or ""))
                my_journal = norm_text(d.get("container-title", ""))

                def agrees(m):
                    """How many of year, first author and journal agree (several records can share a title:
                    a journal digest reprints NEJM abstracts under the same title)."""
                    f = _crossref_fields(m)
                    n = 0
                    if my_year and any(abs(int(my_year) - int(y)) <= 1 for y in f["years"] if y.isdigit()):
                        n += 1
                    if my_first and (my_first in f["authors"] or (d.get("_plain") and f["authors"] and f["authors"][0] in norm_text(d["_raw"]))):
                        n += 1
                    if my_journal and any(journals_match(my_journal, j) for j in
                                          [f["journal"]] + list(m.get("short-container-title") or [])):
                        n += 1
                    return n

                same_title = [m for m in items if score(m) >= 0.92]
                best = max(same_title, key=lambda m: (agrees(m), score(m)), default=None)
                known = sum(1 for x in (my_year, my_first, my_journal) if x)
                # a short title ("PyMOL") matches many unrelated records: it needs a year, author or journal too
                if best and agrees(best) == 0 and len(norm_text(title).split()) < 5:
                    best = None
                if best and known >= 2 and agrees(best) == 0:
                    out.append(Finding(L, "title-only-match", "warning",
                                       "Crossref has this title, but with a different year, first author and journal; "
                                       "check the reference by hand", "", best.get("DOI", "")))
                    best = None
                    stats["not_found"] += 1
                elif best and score(best) >= 0.92:
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
        if msg and d.get("_plain"):
            cr = _crossref_fields(msg)
            stats["matched"] += 1
            raw = norm_text(d["_raw"])
            ct = norm_text(re.sub(r"\[(version|v)\s*\d[^\]]*\]", "", cr["title"]))
            if ct and ct not in raw and not titles_match(d.get("title", ""), cr["title"]):
                out.append(Finding(L, "mismatch-title", "warning", "Title differs from Crossref", d.get("title", ""), cr["title"]))
            if cr["years"] and not any(y in d["_raw"] for y in cr["years"]):
                if cr["type"] in ("software", "dataset", "component"):
                    out.append(Finding(L, "version-year", "info", "Software or data: the year differs from Crossref's record, "
                                       "which is normal for a later version; cite the version you used", year(d), "/".join(cr["years"])))
                else:
                    out.append(Finding(L, "mismatch-year", "warning", "Year differs from Crossref", year(d), "/".join(cr["years"])))
            if cr["authors"] and cr["authors"][0] and not any(a and (a in raw or family_core(a) in raw) for a in cr["authors"][:3]):
                out.append(Finding(L, "mismatch-first-author", "warning", "First author differs from Crossref",
                                   ((d.get("author") or [{}])[0].get("family") or ""), cr["first_author"]))
            _notices(msg, L, doi, out)
            msg = None
        if msg:
            cr = _crossref_fields(msg)
            stats["matched"] += 1
            checks = [
                ("title", d.get("title", ""), cr["title"], titles_match),
                # online-first and print years often differ by one; either is correct
                ("year", year(d), "/".join(cr["years"]) or cr["year"],
                 lambda a, b: not a or not b or a in b.split("/")),
                # Cochrane reviews use the year as volume and the issue number as their own "volume"
                # some publishers deposit "Volume 17" for 17
                ("volume", str(d.get("volume", "")), cr["volume"],
                 lambda a, b, j=cr["journal"]: not a or not b or norm_text(a) == norm_text(b)
                 or norm_text(re.sub(r"(?i)^vol(ume)?\.?\s*", "", b)) == norm_text(a)
                 or ("cochrane" in j.lower() and re.fullmatch(r"(19|20)\d{2}", b) is not None)),
                ("issue", str(d.get("issue", "")), cr["issue"],
                 lambda a, b: not a or not b or norm_text(a) == norm_text(b)),
                # Hindawi and others deposit pages 1-15 for an article number that is also the DOI's end
                ("first page", _first_page(d.get("page", "")), _first_page(cr["page"]),
                 lambda a, b, dd=doi: not a or not b or a == b or (b == "1" and len(a) >= 4 and dd.endswith(a.lower()))),
                ("last page", _last_page(d.get("page", "")), _last_page(cr["page"]),
                 lambda a, b: not a or not b or a == b or _first_page(d.get("page", "")) != _first_page(cr["page"])),
                # consortia are often listed first in Crossref; accept a match among the first authors
                ("first author", ((d.get("author") or [{}])[0].get("family") or ""), cr["first_author"],
                 lambda a, b, au=cr["authors"]: not a or not b or norm_text(a) == norm_text(b) or norm_text(a) in au
                 or family_core(a) in {family_core(x) for x in au if x}),
            ]
            for name, mine, theirs, ok in checks:
                if not ok(mine, theirs):
                    out.append(Finding(L, f"mismatch-{name.replace(' ', '-')}", "warning",
                                       f"{name.capitalize()} differs from Crossref", str(mine), str(theirs)))
                elif not mine and theirs and name in ("volume", "issue", "first page"):
                    out.append(Finding(L, f"fill-{name.replace(' ', '-')}", "info",
                                       f"Missing {name}; Crossref has it", "", str(theirs)))
            _notices(msg, L, doi, out)
        time.sleep(pause)
    return out, stats


JUNK_SUFFIX = re.compile(r"/(?:figures?|tables?|metrics|bibtex|full|abstract|pdf|fulltext|references|s\d+|\d{1,6})(?:/\d+)?$", re.I)


def dedupe(findings: list[Finding]) -> list[Finding]:
    """One problem, one line: "Issue has extra text" already covers "Issue differs from Crossref"."""
    odd = {(f.ref, f.code[4:], f.found) for f in findings if f.code.startswith("odd-")}
    # "Missing issue" (offline) and "Missing issue; Crossref has it" (online) are the same gap
    fill_of = {"missing-page": "fill-first-page", "missing-issue": "fill-issue", "missing-volume": "fill-volume"}
    fills = {(f.ref, f.code): f for f in findings if f.code.startswith("fill-")}
    rank = {"error": 0, "warning": 1, "info": 2}
    swapped = {f.ref for f in findings if f.code == "author-swapped"}
    seen, out = set(), []
    for f in findings:
        if f.code.startswith("mismatch-") and (f.ref, f.code[9:], f.found) in odd:
            continue
        if f.code == "mismatch-first-author" and f.ref in swapped:
            continue  # the swapped names are the reason the first author differs
        fill = fills.get((f.ref, fill_of.get(f.code, "")))
        if fill:
            if rank.get(f.severity, 2) < rank.get(fill.severity, 2):
                fill.severity = f.severity
            continue
        k = (f.ref, f.code, f.found, f.suggested)
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


def _repair_doi(doi: str, fetch: Fetch):
    """DOIs copied from article pages often end in /figures/5, /metrics, /bibtex ... Try without the suffix."""
    cand = doi
    for _ in range(2):
        m = JUNK_SUFFIX.search(cand)
        if not m:
            return None
        cand = cand[:m.start()]
        if not DOI_RE.match(cand):
            return None
        res = fetch(f"https://api.crossref.org/works/{cand}")
        if res and res.get("message"):
            return cand, res["message"]
    return None


def _notices(msg: dict, L: str, doi: str, out: list) -> None:
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
    mine = {label(r) for r in ms.refs} | {"(citation)"}
    affected = len({f.ref for f in findings if f.severity != "info" and f.ref in mine})
    lines = [
        f"# SubmitReady reference report: {title}",
        "",
        f"- References: **{len(ms.refs)}**" + (f" (from {ms.citations} live citations)" if ms.citations else ""),
        f"- Findings: {n.get('error', 0)} errors, {n.get('warning', 0)} warnings, {n.get('info', 0)} notes",
        f"- References needing attention: {affected} of {len(ms.refs)}",
    ]
    if stats:
        lines.append(f"- Online check: {stats['matched']} matched in Crossref, {stats['not_found']} not found")
    lines += ["", f"Nothing was changed. Each suggestion is for you to accept or reject in {manager_steps(ms.manager)['library']}.", ""]
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
    library = load(args.library).refs if args.library else ms.library
    findings = audit(ms, library)
    stats = None
    if args.cmd == "verify":
        try:
            more, stats = verify(ms, http_fetch(args.email))
        except Exception as e:  # network refused, offline, ...
            print(f"Online check failed: {e}. Showing offline audit only.", file=sys.stderr)
            more = []
        findings += more
    findings = dedupe(findings)
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
