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
    library: list[Ref] | None = None          # full library when the source carries one (.tex + .bib)
    missing_keys: list[str] = field(default_factory=list)  # cited keys with no library entry
    plain: bool = False                       # references typed as plain text (no reference manager)
    body_text: str = ""                       # manuscript text before the reference list (plain mode)


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
    return out


def _field_instructions(xml: bytes) -> list[str]:
    """The instruction text of every Word field, in document order (fields can span many runs)."""
    from lxml import etree
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    out, stack = [], []
    for el in etree.fromstring(xml).iter(W + "fldChar", W + "instrText", W + "fldSimple"):
        if el.tag == W + "fldSimple":
            out.append(el.get(W + "instr") or "")
        elif el.tag == W + "instrText":
            if stack and stack[-1] is not None:
                stack[-1].append(el.text or "")
        else:
            kind = el.get(W + "fldCharType")
            if kind == "begin":
                stack.append([])
            elif kind == "separate" and stack:
                if stack[-1] is not None:
                    out.append("".join(stack[-1]))
                stack[-1] = None
            elif kind == "end" and stack:
                if stack[-1] is not None:
                    out.append("".join(stack[-1]))
                stack.pop()
    return out


def _endnote_to_csl(rec) -> dict:
    """An EndNote <record> (as stored in EN.CITE fields) to CSL JSON."""
    def t(path):
        e = rec.find(path)
        return "".join(e.itertext()).strip() if e is not None else ""
    authors = []
    for a in rec.findall(".//contributors/authors/author"):
        name = "".join(a.itertext()).strip()
        fam, _, giv = name.partition(",")
        authors.append({"family": fam.strip(), "given": giv.strip()} if giv else {"literal": fam.strip()})
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

    def add(k, d, n):
        if k not in by_key:
            by_key[k] = Ref(k, d, f"{path.name}, citation {n}")
            out.refs.append(by_key[k])
        by_key[k].cited += 1

    for instr in _field_instructions(zipfile.ZipFile(path).read("word/document.xml")):
        z = re.match(r"\s*ADDIN ZOTERO_ITEM\s+CSL_CITATION\s*(\{.*\})\s*$", instr, re.S)
        if z:
            try:
                c = json.loads(z.group(1))
            except ValueError:
                continue
            out.citations += 1
            props = c.get("properties") or {}
            if props.get("plainCitation") and props.get("formattedCitation") and \
                    props.get("plainCitation") != re.sub(r"<[^>]+>", "", props.get("formattedCitation", "")):
                out.manual_overrides.append(f"{props.get('formattedCitation')} edited by hand as {props.get('plainCitation')!r}")
            for item in c.get("citationItems", []):
                d = item.get("itemData") or {}
                add(str(item.get("id") or d.get("id") or d.get("DOI") or d.get("title", "")), d, out.citations)
            continue
        e = re.match(r"\s*ADDIN EN\.CITE\s*(<EndNote>.*</EndNote>)\s*$", instr, re.S)
        if e:
            try:
                x = etree.fromstring(e.group(1).encode("utf-8"))
            except etree.XMLSyntaxError:
                continue
            out.citations += 1
            for cite in x.findall("Cite"):
                rec = cite.find("record")
                d = _endnote_to_csl(rec) if rec is not None else {}
                if not d.get("author") and cite.findtext("Author"):
                    d["author"] = [{"family": cite.findtext("Author").strip()}]
                if not d.get("issued") and (cite.findtext("Year") or "").strip().isdigit():
                    d["issued"] = {"date-parts": [[int(cite.findtext("Year").strip())]]}
                add(cite.findtext("RecNum") or d.get("DOI") or d.get("title", ""), d, out.citations)
    return out


PLAIN_DOI = re.compile(r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?(10\.\d{4,9}/[^\s,;]+)", re.I)


def parse_plain(text: str) -> dict:
    """Best-effort fields from one typed reference (author-year or numbered styles)."""
    t = re.sub(r"^\s*(\[\d+\]|\d+\.)\s*", "", text)
    d: dict = {"_raw": text, "_plain": True}
    m = PLAIN_DOI.search(t)
    if m:
        d["DOI"] = m.group(1).rstrip(".")
    ym = re.search(r"\((\d{4})([a-z]?)\)", t) or re.search(r"(?<![\d/.])((?:19|20)\d{2})([a-z]?)(?![\d/])", t)
    if ym:
        d["issued"] = {"date-parts": [[int(ym.group(1))]]}
        d["_year_label"] = ym.group(1) + ym.group(2)
    # family name = everything before the initials: "Roca Pereira JF," "van Dijk MC," "Smith AB,"
    fm = re.match(r"\s*(.+?)\s+[A-Z\u00C0-\u00DE]{1,4}(?:-[A-Z\u00C0-\u00DE]{1,2})?\.?(?:,|\s*\(|\s+et al|\.\s)", t[:120])
    if not fm:
        fm = re.match(r"\s*([A-Z\u00C0-\u017F][\w\u00C0-\u017F'\-]+)", t)
    # an organisation as author: "R Core Team (2023)", "IUCN (2025)": no comma and no initials before the year
    org = re.match(r"\s*([^,()]+?)\s*\((?:19|20)\d{2}[a-z]?\)", t)
    if org and not re.search(r"\s[A-Z\u00C0-\u00DE]{1,3}\.?$", org.group(1).strip()):
        d["author"] = [{"literal": org.group(1).strip(), "family": org.group(1).strip()}]
    elif fm:
        d["author"] = [{"family": fm.group(1).strip(" ,")}]
    if ym and re.match(r"\(\d{4}", t[ym.start():]):
        rest = t[ym.end():].strip(" .")
        d["title"] = re.split(r"(?<=[a-z0-9\)])\.\s", rest, maxsplit=1)[0].strip()
    else:
        parts = re.split(r"(?<=[a-z0-9\)])\.\s", t)
        d["title"] = parts[1].strip() if len(parts) > 1 else t
    return d


def read_docx_plain(path: Path) -> ManuscriptRefs:
    import docx as pydocx

    paras = [p for p in pydocx.Document(path).paragraphs]
    texts = [p.text.strip() for p in paras]
    start = None
    for i, t in enumerate(texts):
        if re.fullmatch(r"(?:\d+\.?\s*)?(references|bibliography|literature cited)\s*:?", t, re.I):
            start = i
    out = ManuscriptRefs(plain=True)
    if start is None:
        return out
    # body text before the reference list, table cells included (reviews often cite inside tables)
    doc = pydocx.Document(path)
    ref_el = paras[start]._p
    chunks = []
    for el in doc.element.body.iterchildren():
        if el is ref_el:
            break
        chunks.append("".join(t.text or "" for t in el.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")))
    out.body_text = " ".join(chunks)
    n = 0
    for p, t in zip(paras[start + 1:], texts[start + 1:]):
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
            fields[name] = " ".join(value.replace("{", "").replace("}", "").split())
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
    by_key = {r.key: r for r in lib}
    ms = ManuscriptRefs(library=lib)
    _, body = latex.split_document(latex.read_project(path))
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
    return ms


def read_csl_json(path: Path) -> list[Ref]:
    items = json.loads(path.read_text(encoding="utf-8"))
    return [Ref(str(d.get("id", i)), d, f"{path.name}, entry {i}") for i, d in enumerate(items, 1)]


def load(path: Path) -> ManuscriptRefs:
    suf = path.suffix.lower()
    if suf == ".docx":
        return read_docx(path)
    if suf == ".tex":
        return read_tex(path)
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
        t = norm_text(r.data.get("_raw") or r.data.get("title", ""))
        if doi and doi in seen_doi:
            add(Finding(L, "duplicate", "error", f"Same DOI as {seen_doi[doi]}", doi))
        elif t and t in seen_title:
            add(Finding(L, "duplicate", "error", f"Same title as {seen_title[t]}"))
        if doi:
            seen_doi.setdefault(doi, L)
        if t:
            seen_title.setdefault(t, L)
    if ms.plain and ms.body_text:
        out += match_plain_citations(ms)
    for k in ms.missing_keys:
        add(Finding(f"\\cite{{{k}}}", "missing-key", "error",
                    "Cited in the manuscript but not found in any .bib file; it will print as '?'", k))
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
            if cr["authors"] and cr["authors"][0] and not any(a and a in raw for a in cr["authors"][:3]):
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
                ("volume", str(d.get("volume", "")), cr["volume"],
                 lambda a, b, j=cr["journal"]: not a or not b or norm_text(a) == norm_text(b)
                 or ("cochrane" in j.lower() and re.fullmatch(r"(19|20)\d{2}", b) is not None)),
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
    seen, out = set(), []
    for f in findings:
        if f.code.startswith("mismatch-") and (f.ref, f.code[9:], f.found) in odd:
            continue
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
