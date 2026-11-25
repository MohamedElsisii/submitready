#!/usr/bin/env python3
"""SubmitReady prepare: measure a manuscript and check it against a journal's requirements.

The requirements file is written fresh for each run from the journal's LIVE author
guidelines (every rule carries the quoted text, its source URL and the date checked).
This script only measures the manuscript and compares; it never guesses a rule.

Commands:
  analyze  manuscript.docx                       -> facts about the manuscript (JSON)
  check    manuscript.docx requirements.json     -> checklist (Markdown / JSON)
  format   manuscript.docx requirements.json --out formatted.docx
           applies only safe layout fixes (line numbers, line spacing); text is never touched.
           Run scripts/lock.py verify afterwards to prove it.
"""
from __future__ import annotations

import argparse
import base64
import copy
import json
import re
import shutil
import sys
import zipfile
from collections import Counter
from pathlib import Path

from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent))

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}


def q(tag: str) -> str:
    return f"{{{W}}}{tag}"


SECTIONS = [
    ("abstract", r"abstract"),
    ("keywords", r"key\s*-?\s*words?"),
    ("introduction", r"introduction|background"),
    ("methods", r"(materials? and )?methods?|methodology|materials and methods"),
    ("results", r"results?( and discussion)?"),
    ("discussion", r"discussion"),
    ("conclusion", r"conclusions?|concluding remarks"),
    ("abbreviations", r"abbreviations"),
    ("acknowledgements", r"acknowledge?ments?"),
    ("funding", r"funding( statement| information| sources?)?|financial disclosure"),
    ("competing", r"(competing|conflicts?( of)?) interests?|disclosure statement|declaration of (competing )?interests?|disclosures?"),
    ("data", r"data (and code )?availability( statement)?|availability of data.*"),
    ("ethics", r"ethic(s|al)( approval| conduct of research| statement| approval and consent.*)?"),
    ("contributions", r"authors?'?s?'? contributions?( statement)?|credit authorship.*"),
    ("ai", r"(generative )?ai (use |usage )?(statement|disclosure)|use of (generative )?ai.*|writing assistance disclosure|"
           r"declaration of (the use of )?(generative )?(ai|artificial intelligence).*"),
    ("declarations", r"(statements and )?declarations"),
    ("references", r"references|bibliography|literature cited"),
    ("figure_legends", r"figure (and table )?(legends|captions)|figure legends"),
    ("supplementary", r"supplementary( material| information| files?)?( legends)?|supporting information"),
    ("highlights", r"(article )?highlights"),
    ("plain_summary", r"plain language summary"),
]
LEADING_SECTION = re.compile(r"^(?:[\dIVX]+(?:\.\d+)*\.?\s*)?(introduction|background|methods?|materials and methods|"
                             r"results?|discussion|conclusions?)\s*(?::|\s+and\s|\s*[-\u2013]\s)", re.I)
SECTION_RE = [(k, re.compile(rf"^(?:[\dIVX]+(?:\.\d+)*\.?\s*)?(?:{p})\s*:?$", re.I)) for k, p in SECTIONS]
END_OF_MAIN = {"abbreviations", "acknowledgements", "funding", "competing", "data", "ethics", "contributions",
               "ai", "declarations", "references", "figure_legends", "supplementary", "highlights"}
CAPTION_RE = re.compile(r"^(Figure|Fig\.?|Table)\s*(S?)(\d+)\b", re.I)
SUPP_RE = re.compile(r"\b(?:(Table|Figure|Fig\.?|Code|Data|File|Appendix|Video|Dataset)\s*S(\d+)|S(\d+)\s+(Table|Fig|Figure|File|Appendix|Data|Code))\b")


# ---------------------------------------------------------------- reading
def _text(el) -> str:
    out = []
    for n in el.iter(q("t"), q("tab"), q("br")):
        if n.tag == q("t"):
            out.append(n.text or "")
        else:
            out.append(" ")
    return "".join(out)


def _is_bold(p, styles_bold: set[str]) -> bool:
    runs = [r for r in p.iter(q("r")) if (_text(r) or "").strip()]
    if not runs:
        return False
    ps = p.find(f"{q('pPr')}/{q('pStyle')}")
    pstyle = ps.get(q("val")) if ps is not None else ""
    for r in runs:
        b = r.find(f"{q('rPr')}/{q('b')}")
        bold = b is not None and b.get(q("val"), "true") not in ("0", "false")
        if not bold and pstyle not in styles_bold:
            return False
    return True


def _style_name_map(z) -> tuple[dict, set]:
    names, bold = {}, set()
    if "word/styles.xml" in z.namelist():
        st = etree.fromstring(z.read("word/styles.xml"))
        for s in st.iter(q("style")):
            sid = s.get(q("styleId"))
            n = s.find(q("name"))
            names[sid] = (n.get(q("val")) if n is not None else sid) or sid
            b = s.find(f"{q('rPr')}/{q('b')}")
            if b is not None and b.get(q("val"), "true") not in ("0", "false"):
                bold.add(sid)
    return names, bold


INLINE_STATEMENT = re.compile(
    r"^(funding( statement| information)?|financial disclosure|(competing|conflicts?( of)?) interests?|"
    r"data (and code )?availability( statement)?|authors?['\u2019]?s?['\u2019]? contributions?|acknowledge?ments?)\s*[:.]\s*(.+)$",
    re.I)


def inline_statement(text: str) -> tuple[str, str] | None:
    """'Funding: This work was ...' in one paragraph -> ("funding", "This work was ...")."""
    m = INLINE_STATEMENT.match(text.strip())
    if not m:
        return None
    sec = _section_of(m.group(1))
    return (sec, m.group(m.lastindex).strip()) if sec else None


def _section_of(text: str) -> str | None:
    t = re.sub(r"\s+", " ", text).strip().replace("\u2019", "'").replace("\u2018", "'")
    for k, rx in SECTION_RE:
        if rx.match(t):
            return k
    # a heading that starts with a main section word: "Background and rationale", "Methods: participants"
    m = LEADING_SECTION.match(t)
    if m:
        word = m.group(1).lower()
        return "introduction" if word in ("introduction", "background") else \
            "methods" if word.startswith(("method", "materials")) else \
            "results" if word.startswith("result") else "discussion" if word == "discussion" else "conclusion"
    return None


def words(s: str) -> int:
    return len(re.findall(r"[^\s]+", s))


def _citation_format(root) -> tuple[str, list[str]]:
    """Read the visible text of live citation content controls (Mendeley Cite, Zotero CSL)."""
    samples = []
    for sdt in root.iter(q("sdt")):
        tag = sdt.find(f"{q('sdtPr')}/{q('tag')}")
        val = tag.get(q("val"), "") if tag is not None else ""
        if not re.match(r"(MENDELEY_CITATION|CSL_CITATION|ZOTERO_ITEM)", val, re.I):
            continue
        text = _text(sdt).strip()
        if not text and val.startswith("MENDELEY_CITATION_v3_"):
            try:
                b = val[len("MENDELEY_CITATION_v3_"):]
                d = json.loads(base64.b64decode(b + "=" * (-len(b) % 4)))
                text = (d.get("manualOverride") or {}).get("citeprocText", "")
            except Exception:
                pass
        if text:
            samples.append(text)
        if len(samples) >= 20:
            break
    s = " ".join(samples)
    if not samples:
        # typed citations (no reference manager): look at the plain text
        body = "".join(t.text or "" for t in root.iter(q("t")))
        ay = re.findall(r"\([A-Z][^()]{0,60}?(?:et al\.?|and [A-Z]\w+)?,? (?:19|20)\d{2}[a-z]?[;)]", body)
        nb = re.findall(r"\[\d+(?:[-\u2013,]\s*\d+)*\]", body)
        if len(ay) >= 3 and len(ay) >= len(nb):
            return "author-date (typed, not live citations)", [a.strip("(;)") for a in ay[:5]]
        if len(nb) >= 3:
            return "numeric, square brackets (typed, not live citations)", nb[:5]
        return "unknown (no live citations found)", []
    if re.search(r"\[\d", s):
        return "numeric, square brackets", samples[:5]
    if re.search(r"\(\d", s) or re.search(r"<sup>|^\d", s):
        return "numeric, parentheses", samples[:5]
    if re.search(r"\(\D+,? \d{4}", s):
        return "author-date", samples[:5]
    return "unknown", samples[:5]


def analyze_tex(path: Path) -> dict:
    import latex

    full = latex.read_project(path)
    pre, body = latex.split_document(full)
    title = latex.detex(latex.command_arg(pre, "title") or latex.command_arg(body, "title") or "")
    ab = latex.abstract_text(body)
    abstract = latex.detex(ab) if ab else ""
    kw = latex.command_arg(body, "keywords") or latex.command_arg(pre, "keywords")
    if kw is None:
        m = re.search(r"\\begin\{keywords?\}(.*?)\\end\{keywords?\}", body, re.S) or \
            re.search(r"Keywords?\W*:\W*(.*)", body)
        kw = m.group(1) if m else ""
    keywords = [k.strip(" .") for k in re.split(r"[;,\u2022|]|\\sep", latex.detex(kw)) if k.strip(" .")]
    # sections and main text
    order, main_words = [], 0
    floats = list(re.finditer(r"\\begin\{(figure|table)\*?\}(.*?)\\end\{\1\*?\}", body, re.S))
    nofloat = re.sub(r"\\begin\{(figure|table)\*?\}.*?\\end\{\1\*?\}", " ", body, flags=re.S)
    nofloat = re.sub(r"\\begin\{abstract\}.*?\\end\{abstract\}", " ", nofloat, flags=re.S)
    if ab:
        nofloat = nofloat.replace(ab, " ", 1)
    parts = re.split(r"(\\(?:section|chapter)\*?\s*(?:\[[^\]]*\])?\s*\{[^}]*\})", nofloat)
    current, started, ended = "front", False, False
    if ab:
        order.append("abstract")
    if keywords:
        order.append("keywords")
    for chunk in parts:
        m = re.match(r"\\(?:section|chapter)\*?\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", chunk)
        if m:
            sec = _section_of(latex.detex(m.group(1)))
            current = sec or "other"
            if sec and sec not in order:
                order.append(sec)
            if sec in ("introduction", "methods", "results", "discussion") and not ended:
                started = True
            if sec in END_OF_MAIN and started:
                ended = True
            continue
        if re.search(r"\\bibliography\s*\{|\\printbibliography|\\begin\{thebibliography\}", chunk):
            if "references" not in order:
                order.append("references")
            if started:
                ended = True
            chunk = re.split(r"\\bibliography\s*\{|\\printbibliography|\\begin\{thebibliography\}", chunk)[0]
        if started and not ended:
            main_words += words(latex.detex(chunk))
    figs = [f for f in floats if f.group(1) == "figure" and "\\caption" in f.group(2)]
    tabs = [f for f in floats if f.group(1) == "table" and "\\caption" in f.group(2)]
    end_marker = len(body) * 0.85
    leg = re.search(r"\\section\*?\s*\{\s*(figure|table)s?\s*(and tables?\s*)?(legends|captions)?", body, re.I)
    placement = Counter()
    for f in figs + tabs:
        where = "end" if f.start() > end_marker or (leg and f.start() > leg.start()) else "in_text"
        placement[f"{f.group(1)}_{where}"] += 1
    text = latex.detex(body)
    supp = sorted({(m[0] or m[3] or "").title() + " S" + (m[1] or m[2]) for m in SUPP_RE.findall(text)})
    supp_style = Counter("S1 Table" if m[2] else "Table S1" for m in SUPP_RE.findall(text))
    allx = pre + body
    has_lineno, lineno_opts = latex.has_package(pre, "lineno")
    line_numbers = has_lineno and bool(re.search(r"\\linenumbers\b", allx) or re.search(r"running|pagewise", lineno_opts))
    spacing = 1.0
    if re.search(r"\\doublespacing\b", allx):
        spacing = 2.0
    elif re.search(r"\\onehalfspacing\b", allx):
        spacing = 1.5
    m = re.search(r"\\(?:setstretch|linespread)\s*\{([\d.]+)\}", allx)
    if m:
        spacing = round(float(m.group(1)), 2)
    return {
        "file": path.name,
        "title": title,
        "title_chars": len(title),
        "title_words": words(title),
        "abstract_words": words(abstract),
        "keywords": keywords,
        "main_text_words": main_words,
        "sections_found": order,
        "headings": [latex.detex(h) for h in re.findall(r"\\(?:sub)*section\*?\s*\{([^}]*)\}", body)],
        "figures": list(range(1, len(figs) + 1)),
        "tables": list(range(1, len(tabs) + 1)),
        "captions_placement": dict(placement),
        "figures_embedded": len(re.findall(r"\\includegraphics", body)),
        "supplementary_items": supp,
        "supplementary_label_style": supp_style.most_common(1)[0][0] if supp_style else None,
        "line_numbers": line_numbers,
        "page_numbers": not re.search(r"\\pagestyle\s*\{empty\}", allx),
        "line_spacing": spacing,
        "footnotes": len(re.findall(r"\\footnote\s*\{", body)),
        "citation_format": latex.citation_style(path),
        "citation_samples": [m.group(0) for m in latex.CITE_RE.finditer(body)][:5],
        "references": len(latex.cited_keys(path)),
        "format": "latex",
        **text_style_facts(latex.detex(body), [latex.detex(b) for b in re.split(r"\n\s*\n", body)]),
    }


def read_paragraphs(path: Path):
    """Paragraphs of a .docx in reading order, with heading/section/table/bibliography flags.

    Each item: i, text (spaces tidied), raw (exact text), heading, section, table, bib, style, el.
    Returns (zipfile, xml root, paragraphs)."""
    z = zipfile.ZipFile(path)
    xml_bytes = z.read("word/document.xml")
    root = etree.fromstring(xml_bytes)
    names, styles_bold = _style_name_map(z)
    body = root.find(q("body"))
    paras = []
    for el in body.iter(q("p")):
        in_table = any(a.tag == q("tbl") for a in el.iterancestors())
        in_bib = any(a.tag == q("sdt") and "BIBLIOGRAPHY" in (a.findtext(f"{q('sdtPr')}/{q('tag')}") or "" or "")
                     for a in el.iterancestors())
        tag_el = None
        for a in el.iterancestors(q("sdt")):
            tag_el = a.find(f"{q('sdtPr')}/{q('tag')}")
            if tag_el is not None and "BIBLIOGRAPHY" in (tag_el.get(q("val")) or ""):
                in_bib = True
        text = re.sub(r"[ \t\u00a0]+", " ", _text(el)).strip()
        if not text:
            continue
        ps = el.find(f"{q('pPr')}/{q('pStyle')}")
        style = names.get(ps.get(q("val")), ps.get(q("val"))) if ps is not None else "Normal"
        sec = _section_of(text) if len(text) < 120 else None
        is_heading = False
        if not in_table and not in_bib and len(text) < 150 and not CAPTION_RE.match(text):
            if style.lower().startswith(("heading", "title")) or sec:
                is_heading = True
            elif _is_bold(el, styles_bold) and not text.endswith((".", ";", ",")) and words(text) <= 15:
                is_heading = True
        paras.append({"i": len(paras), "text": text, "heading": is_heading, "section": sec if is_heading else None,
                      "table": in_table, "bib": in_bib, "style": style, "raw": _text(el).strip(), "el": el})

    return z, root, paras


def analyze(path: Path) -> dict:
    if path.suffix.lower() == ".tex":
        return analyze_tex(path)
    z, root, paras = read_paragraphs(path)
    xml = z.read("word/document.xml").decode("utf-8")
    import refs as _refs
    manager = _refs.detect_manager(xml)
    body = root.find(q("body"))

    # title = first non-empty paragraph
    title = paras[0]["text"] if paras else ""

    # walk sections
    current = "front"
    order, seen = [], set()
    abstract_parts, kw_text = [], ""
    main_words, main_started, main_ended = 0, False, False
    captions = []
    where: dict[str, list[str]] = {}
    for p in paras:
        t = p["text"]
        inline = re.match(r"^(abstract|key\s*-?\s*words?)\s*[:.]\s*(.+)$", t, re.I)
        if p["heading"] and not p["section"] and current in ("abstract", "keywords") and \
                str(p.get("style", "")).lower().startswith("heading"):
            current = "other"  # an unnamed Word heading ends the abstract
            continue
        if p["heading"] and p["section"]:
            current = p["section"]
            where.setdefault(current, []).append(t)
            if current not in seen:
                seen.add(current)
                order.append(current)
            if current in ("introduction", "methods", "results", "discussion") and not main_ended:
                main_started = True
            if current in END_OF_MAIN and main_started:
                main_ended = True
            continue
        if inline and not p["table"]:
            key = "abstract" if inline.group(1).lower().startswith("abs") else "keywords"
            if key not in seen:
                seen.add(key)
                order.append(key)
            if key == "abstract":
                abstract_parts.append(inline.group(2))
            else:
                kw_text = inline.group(2)
            continue
        st = inline_statement(t) if not p["heading"] and not p["table"] and not p["bib"] else None
        if st:
            where.setdefault(st[0], []).append(" ".join(t.split()[:6]))
            if st[0] not in seen:
                seen.add(st[0])
                order.append(st[0])
            current = st[0]
            if current in END_OF_MAIN and main_started:
                main_ended = True
            continue
        m = CAPTION_RE.match(t)
        if m and not p["table"]:
            captions.append({"i": p["i"], "kind": "table" if m.group(1).lower().startswith("t") else "figure",
                             "supp": bool(m.group(2)), "n": int(m.group(3)), "section": current})
            continue
        if p["heading"] or p["table"] or p["bib"]:
            continue
        if current == "abstract":
            abstract_parts.append(t)
        elif current == "keywords" and not kw_text:
            kw_text = t
        elif main_started and not main_ended:
            main_words += words(t)

    keywords = [k.strip(" .") for k in re.split(r"[;,\u2022|]", kw_text) if k.strip(" .")]
    figs = sorted({c["n"] for c in captions if c["kind"] == "figure" and not c["supp"]})
    tabs = sorted({c["n"] for c in captions if c["kind"] == "table" and not c["supp"]})
    first_mention = {}
    for p in paras:
        if p["heading"] or CAPTION_RE.match(p["text"]):
            continue
        for kind, n in re.findall(r"\b(Fig(?:ure)?s?\.?|Tables?)\s*(\d+)", p["text"]):
            key = ("table" if kind.lower().startswith("t") else "figure", int(n))
            first_mention.setdefault(key, p["i"])
    placement = Counter()
    caption_labels: dict[str, list[str]] = {}
    seen_caps = Counter()
    for c in captions:
        if c["supp"]:
            continue
        seen_caps[(c["kind"], c["n"])] += 1
        pos = "end" if c["section"] in ("figure_legends", "references", "supplementary") or c["i"] > len(paras) * 0.85 else "in_text"
        placement[f"{c['kind']}_{pos}"] += 1
        caption_labels.setdefault(f"{c['kind']}_{pos}", []).append(f"{c['kind'].title()} {c['n']}")
    caption_duplicates = [f"{k.title()} {n}" for (k, n), v in sorted(seen_caps.items()) if v > 1]
    supp = sorted({(m[0] or m[3] or "").title() + " S" + (m[1] or m[2]) for m in SUPP_RE.findall(" ".join(p["text"] for p in paras))})
    supp_style = Counter("S1 Table" if m[2] else "Table S1" for m in SUPP_RE.findall(" ".join(p["text"] for p in paras)))

    sect_prs = list(root.iter(q("sectPr")))
    line_numbers = any(s.find(q("lnNumType")) is not None for s in sect_prs)
    page_numbers = False
    for n in z.namelist():
        if re.match(r"word/footer\d*\.xml|word/header\d*\.xml", n):
            x = z.read(n).decode("utf-8", "ignore")
            if re.search(r"instr[^>]*>\s*PAGE\b|w:instr=\"\s*PAGE", x):
                page_numbers = True
    spacing = Counter()
    for p in body.iter(q("p")):
        sp = p.find(f"{q('pPr')}/{q('spacing')}")
        if sp is not None and sp.get(q("line")) and sp.get(q("lineRule"), "auto") == "auto":
            spacing[round(int(sp.get(q("line"))) / 240, 2)] += 1
    default_spacing = _default_line_spacing(z)
    n_paras = sum(1 for _ in body.iter(q("p")))
    explicit = sum(spacing.values())
    if default_spacing is not None:
        spacing[default_spacing] += max(0, n_paras - explicit)
    line_spacing = spacing.most_common(1)[0][0] if spacing else None
    footnotes = len(re.findall(r"<w:footnoteReference ", xml))
    images = len(re.findall(r"<w:drawing>|<w:pict>", xml))
    cite_fmt, cite_samples = _citation_format(root)
    refs = sum(1 for p in paras if p["bib"])
    if not refs and "references" in seen:
        in_refs = False
        for p in paras:
            if p["heading"]:
                in_refs = p["section"] == "references"
                continue
            if in_refs:
                refs += 1

    return {
        "file": path.name,
        "title": title,
        "title_chars": len(title),
        "title_words": words(title),
        "abstract_words": words(" ".join(abstract_parts)),
        "keywords": keywords,
        "main_text_words": main_words,
        "sections_found": order,
        "headings": [p["text"] for p in paras if p["heading"]],
        "figures": figs,
        "tables": tabs,
        "captions_placement": dict(placement),
        "caption_duplicates": caption_duplicates,
        "reference_manager": manager,
        "caption_labels": caption_labels,
        "section_where": where,
        "supp_mentions": sum(supp_style.values()),
        "figures_embedded": images,
        "supplementary_items": supp,
        "supplementary_label_style": supp_style.most_common(1)[0][0] if supp_style else None,
        "line_numbers": line_numbers,
        "page_numbers": page_numbers,
        "line_spacing": line_spacing,
        "footnotes": footnotes,
        "citation_format": cite_fmt,
        "citation_samples": cite_samples,
        "references": refs,
        **text_style_facts(" ".join(p["text"] for p in paras if not p["bib"]), [p["text"] for p in paras if not p["bib"]]),
    }


P_NO_ZERO = re.compile(r"\b[pP]\s*(?:-?value\s*)?[<=>\u2264\u2265]\s*\.\d+")
P_ANY = re.compile(r"\b[pP]\s*(?:-?value\s*)?[<=>\u2264\u2265]\s*0?\.\d")
NUM_CITE = r"\[\d{1,3}(?:\s*[-\u2013,]\s*\d{1,3})*\]"
CITE_BEFORE = re.compile(rf"\w\s?{NUM_CITE}[.,;:]")      # "... text [12]."
CITE_AFTER = re.compile(rf"\w[.,;:]\s?{NUM_CITE}")         # "... text.[12]" or "text. [12]"


def text_style_facts(text: str, paragraphs: list[str]) -> dict:
    """House-style facts some journals check: a leading zero on p-values (P = 0.03, not P = .03),
    where numbered citations sit against punctuation, and how many references carry an annotation
    (a "* ..." or "** ..." line right after a typed "[6] Author ..." line, as Epigenomics and other
    Future Science journals ask). Measured paragraph by paragraph, so two paragraphs never join."""
    no_zero, before, after, samples, notes = [], 0, 0, [], 0
    for i, t in enumerate(paragraphs):
        no_zero += [m.group(0) for m in P_NO_ZERO.finditer(t)]
        before += len(CITE_BEFORE.findall(t))
        hits = [t[max(0, m.start() - 12):m.end()].strip() for m in CITE_AFTER.finditer(t)]
        after += len(hits)
        samples += hits
        if re.match(r"\s*\*{1,2}\s*\w", t) and i and re.match(r"\s*(\[\d{1,3}\]|\d{1,3}\.)\s", paragraphs[i - 1]):
            notes += 1
    return {
        "p_values": sum(len(P_ANY.findall(t)) for t in paragraphs),
        "p_values_without_zero": no_zero,
        "citations_before_punctuation": before,
        "citations_after_punctuation": after,
        "citation_punctuation_samples": samples[:3],
        "reference_annotations": notes,
    }


def _default_line_spacing(z) -> float | None:
    if "word/styles.xml" not in z.namelist():
        return None
    st = etree.fromstring(z.read("word/styles.xml"))
    val = None
    d = st.find(f"{q('docDefaults')}/{q('pPrDefault')}/{q('pPr')}/{q('spacing')}")
    if d is not None and d.get(q("line")):
        val = int(d.get(q("line"))) / 240
    for s in st.iter(q("style")):
        if s.get(q("styleId")) == "Normal":
            sp = s.find(f"{q('pPr')}/{q('spacing')}")
            if sp is not None and sp.get(q("line")):
                val = int(sp.get(q("line"))) / 240
    return round(val, 2) if val else 1.0


# ---------------------------------------------------------------- checking
def check(facts: dict, req: dict) -> list[dict]:
    """Check types: max_chars, max_words, keywords, section_present, section_absent, sections_order,
    line_numbers, page_numbers, line_spacing, footnotes, figures_embedded,
    captions_placement (value "in_text" or "end"; optional kind "figure" or "table", default both),
    citation_format, supp_label_style, max_count, min_count, p_leading_zero,
    citation_punctuation (value "before" or "after"), figure_files, manual."""
    out = []
    for r in req["requirements"]:
        c = r.get("check", {"type": "manual"})
        t = c["type"]
        status, found = "CHECK", ""
        try:
            if t == "max_chars":
                v = facts[f"{c['target']}_chars"]
                status, found = ("PASS" if v <= c["value"] else "FIX"), f"{v} characters"
            elif t == "max_words":
                key = {"abstract": "abstract_words", "main": "main_text_words", "title": "title_words"}[c["target"]]
                v = facts[key]
                status, found = ("PASS" if v <= c["value"] else "FIX"), f"{v:,} words"
                if v > c["value"]:
                    found += f" ({v - c['value']:,} over)"
            elif t == "keywords":
                n = len(facts["keywords"])
                ok = c.get("min", 0) <= n <= c.get("max", 999)
                status, found = ("PASS" if ok else "FIX"), f"{n} keywords"
            elif t == "section_present":
                ok = c["section"] in facts["sections_found"]
                status, found = ("PASS" if ok else "FIX"), "found" if ok else "not found"
                if not ok and c["section"] == "supplementary" and not facts.get("supplementary_items"):
                    status, found = "PASS", "no supplementary items are cited, so none is needed"
            elif t == "section_absent":
                bad = c["section"] in facts["sections_found"]
                status, found = ("FIX" if bad else "PASS"), "present in the file" if bad else "not in the file"
            elif t == "sections_order":
                seq = [s for s in facts["sections_found"] if s in c["order"]]
                expected = [s for s in c["order"] if s in seq]
                missing = [s for s in c["order"] if s not in seq and s not in c.get("optional", [])]
                ok = seq == expected and not missing
                status = "PASS" if ok else "FIX"
                found = " > ".join(seq) + (f"; missing: {', '.join(missing)}" if missing else "")
            elif t == "line_numbers":
                ok = facts["line_numbers"] == c.get("value", True)
                status, found = ("PASS" if ok else "AUTO"), "on" if facts["line_numbers"] else "off"
            elif t == "page_numbers":
                ok = facts["page_numbers"] == c.get("value", True)
                status, found = ("PASS" if ok else "FIX"), "present" if facts["page_numbers"] else "not found"
            elif t == "line_spacing":
                ok = facts["line_spacing"] == c["value"]
                status, found = ("PASS" if ok else "AUTO"), f"{facts['line_spacing']}"
            elif t == "footnotes":
                ok = facts["footnotes"] == 0 or c.get("allowed", False)
                status, found = ("PASS" if ok else "FIX"), f"{facts['footnotes']} footnotes"
            elif t == "figures_embedded":
                ok = (facts["figures_embedded"] > 0) == c.get("allowed", False) or facts["figures_embedded"] == 0
                status, found = ("PASS" if ok else "FIX"), f"{facts['figures_embedded']} images inside the file"
            elif t == "captions_placement":
                kind = c.get("kind")  # "figure", "table", or None for both
                pl = {k: v for k, v in facts["captions_placement"].items() if not kind or k.startswith(kind + "_")}
                want = c["value"]
                good = sum(v for k, v in pl.items() if k.endswith("_" + want))
                ok = good > 0 and sum(pl.values()) == good
                none_of_kind = kind and not facts.get({"figure": "figures", "table": "tables"}[kind], [1])
                found = ", ".join(f"{v} {k.split('_', 1)[0]} caption{'s' if v > 1 else ''} { {'end': 'at the end', 'in_text': 'in the text'}[k.split('_', 1)[1]] }"
                                  for k, v in sorted(pl.items())) or "no captions found"
                dups = [d for d in facts.get("caption_duplicates", []) if not kind or d.lower().startswith(kind)]
                if dups:
                    where = {"end": "at the end", "in_text": "in the text"}
                    other = "in_text" if want == "end" else "end"
                    verb = "appears" if len(dups) == 1 else "appear"
                    found += (f"; {', '.join(dups)} {verb} twice: keep the copy {where[want]}, "
                              f"delete the one {where[other]}")
                status = "PASS" if ok else "FIX"
                if not pl:
                    # nothing to place: no tables at all is fine; captions we could not find need a look
                    status, found = ("PASS", f"no {kind}s in the manuscript") if none_of_kind else ("CHECK", found)
            elif t == "citation_format":
                ok = facts["citation_format"].startswith(c["value"])
                status = "PASS" if ok else "STYLE"
                found = f"{facts['citation_format']} e.g. {', '.join(facts['citation_samples'][:3])}"
            elif t == "supp_label_style":
                s = facts["supplementary_label_style"]
                ok = s is None or s == c["value"]
                status, found = ("PASS" if ok else "FIX"), f"{s or 'none found'}"
            elif t == "max_count":
                if c["target"] == "display_items":  # figures and tables together
                    v = len(facts["figures"]) + len(facts["tables"])
                else:
                    x = facts[c["target"]]
                    v = x if isinstance(x, int) else len(x)
                status, found = ("PASS" if v <= c["value"] else "FIX"), f"{v}"
            elif t == "min_count":
                x = facts[c["target"]]
                v = x if isinstance(x, int) else len(x)
                status, found = ("PASS" if v >= c["value"] else "FIX"), f"{v}"
            elif t == "p_leading_zero":
                bad = facts["p_values_without_zero"]
                status = "FIX" if bad else "PASS"
                found = (f"{len(bad)} without a zero, e.g. {', '.join(bad[:3])}" if bad
                         else f"{facts['p_values']} p-values, all with a zero")
            elif t == "citation_punctuation":
                want = c["value"]  # "before": text [1]. ; "after": text.[1]
                wrong = facts["citations_after_punctuation" if want == "before" else "citations_before_punctuation"]
                status = "PASS" if not wrong else "FIX"
                found = (f"{wrong} citation{'s' if wrong != 1 else ''} on the other side of the punctuation"
                         + (f", e.g. {', '.join(facts['citation_punctuation_samples'])}"
                            if want == "before" and facts["citation_punctuation_samples"] else ""))
            elif t == "manual":
                status = "CHECK"
        except KeyError as e:
            status, found = "CHECK", f"could not measure ({e})"
        out.append({"id": r["id"], "rule": r["rule"], "status": status, "found": found,
                    "quote": r.get("quote", ""), "source": req["sources"][r.get("source", 0)],
                    "location": _location(c, facts) if status in ("FIX", "AUTO") else ""})
    return out


def _location(c: dict, facts: dict) -> str:
    """Where in the file the author should look: text they can find with Ctrl+F."""
    t = c.get("type")
    heads = facts.get("section_where", {})
    if t == "section_absent":
        hs = heads.get(c["section"], [])
        return ("Find " + " and ".join(f'"{h}"' for h in hs)) if hs else ""
    if t == "section_present":
        return "No such section was found; add one"
    if t == "captions_placement":
        kind, want = c.get("kind"), c["value"]
        wrong = [lab for k, labs in facts.get("caption_labels", {}).items()
                 if (not kind or k.startswith(kind + "_")) and not k.endswith("_" + want) for lab in labs]
        place = {"end": "at the end", "in_text": "in the text"}
        return f"Now {place['in_text' if want == 'end' else 'end']}: {', '.join(wrong)}" if wrong else ""
    if t == "supp_label_style":
        n = facts.get("supp_mentions", 0)
        return f"{n} mentions in the text and captions" if n else ""
    if t in ("max_words", "max_chars"):
        return {"abstract": 'Section "Abstract"', "title": "The title", "main": "Introduction to end of Discussion"}.get(c.get("target"), "")
    if t == "keywords":
        return 'The "Keywords" line'
    if t == "footnotes":
        return "Footnotes (References > Next Footnote in Word)"
    if t == "figures_embedded":
        return f"{facts.get('figures_embedded', 0)} images inside the Word file"
    return ""


STATUS_TEXT = {
    "PASS": "Pass",
    "FIX": "You fix",
    "AUTO": "Auto-fix available",
    "STYLE": "Change style in your reference manager",
    "CHECK": "Check yourself",
}


def checklist_markdown(facts: dict, req: dict, results: list[dict]) -> str:
    n = Counter(r["status"] for r in results)
    lines = [
        f"# SubmitReady checklist: {facts['file']} for {req['journal']} ({req.get('article_type', '')})",
        "",
        "Rules come from the journal's live guidelines, read on "
        + ", ".join(sorted({s['checked'] for s in req['sources']})) + ":",
    ]
    lines += [f"- [{s.get('name', s['url'])}]({s['url']})" for s in req["sources"]]
    lines += ["", f"**{n.get('PASS', 0)} pass, {n.get('FIX', 0)} for you to fix, {n.get('AUTO', 0)} auto-fixable, "
              f"{n.get('STYLE', 0)} reference-style, {n.get('CHECK', 0)} to check yourself.**", "",
              "| # | Rule | Status | Found | Guideline says |", "| --- | --- | --- | --- | --- |"]
    order = {"FIX": 0, "STYLE": 1, "AUTO": 2, "CHECK": 3, "PASS": 4}
    for i, r in enumerate(sorted(results, key=lambda r: order[r["status"]]), 1):
        esc = lambda s: (s or "").replace("|", "\\|").replace("\n", " ")
        said = f"\"{esc(r['quote'])[:160]}\"" if r["quote"] else "(paraphrased; see source)"
        lines.append(f"| {i} | {esc(r['rule'])} | {STATUS_TEXT[r['status']]} | {esc(r['found'])} | {said} |")
    lines += ["", "## Manuscript facts", "",
              f"- Title: {facts['title_chars']} characters, {facts.get('title_words', 0)} words",
              f"- Abstract: {facts['abstract_words']} words; keywords: {len(facts['keywords'])}",
              f"- Main text (Introduction to end of Discussion/Conclusion, without headings, captions and tables): {facts['main_text_words']:,} words",
              f"- Sections in order: {' > '.join(facts['sections_found'])}",
              f"- Figures: {len(facts['figures'])}, tables: {len(facts['tables'])}, images inside the file: {facts['figures_embedded']}",
              f"- Supplementary items mentioned: {len(facts['supplementary_items'])} ({facts['supplementary_label_style'] or 'none'} style)",
              f"- Line numbers: {'on' if facts['line_numbers'] else 'off'}; page numbers: {'yes' if facts['page_numbers'] else 'no'}; line spacing: {facts['line_spacing']}",
              f"- Citations: {facts['citation_format']}"
              + (f", inserted with {facts['reference_manager']}" if facts.get("reference_manager") else "")
              + f"; references: {facts['references']}",
              ""]
    if req.get("csl_style"):
        import refs as _refs
        mgr = facts.get("reference_manager", "")
        steps = _refs.manager_steps(mgr)
        how = steps["style"].format(style=req["csl_style"]["title"], journal=req["journal"])
        lines += [f"Reference style{' (' + mgr + ')' if mgr else ''}: {how} Then {steps['refresh']}. "
                  f"(CSL id `{req['csl_style']['id']}`, [source]({req['csl_style']['source']}))", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- safe formatting
def format_tex(src: Path, req: dict, out: Path) -> list[str]:
    """Layout-only fixes for LaTeX: edits the preamble and layout switches, never body text.
    Writes the new main file next to the original so \\input files and figures still resolve."""
    wanted = {r["check"]["type"]: r["check"] for r in req["requirements"] if "check" in r}
    text = src.read_text(encoding="utf-8")
    done = []
    m = re.search(r"\\begin\{document\}", text)
    if not m:
        return ["No \\begin{document} found; nothing changed"]
    pre, rest = text[:m.start()], text[m.start():]
    if "line_numbers" in wanted and wanted["line_numbers"].get("value", True):
        import latex

        if not latex.has_package(latex.strip_comments(pre), "lineno")[0]:
            pre += "\\usepackage{lineno} % added by SubmitReady\n"
        if not re.search(r"^[^%\n]*\\linenumbers\b", rest, re.M):
            rest = rest.replace("\\begin{document}", "\\begin{document}\n\\linenumbers % added by SubmitReady", 1)
        done.append("Turned on line numbers (lineno package)")
    elif "line_numbers" in wanted and re.search(r"^[^%\n]*\\linenumbers\b", rest, re.M):
        rest = re.sub(r"^([^%\n]*)\\linenumbers\b", r"\1% \\linenumbers % turned off by SubmitReady", rest, count=1, flags=re.M)
        done.append("Turned off line numbers (commented out \\linenumbers)")
    if "line_spacing" in wanted:
        v = wanted["line_spacing"]["value"]
        cmd = {1.0: "\\singlespacing", 1.5: "\\onehalfspacing", 2.0: "\\doublespacing"}.get(v)
        if cmd:
            rest = re.sub(r"\\(singlespacing|onehalfspacing|doublespacing)\b", "", rest)
            pre = re.sub(r"\\(singlespacing|onehalfspacing|doublespacing)\b", "", pre)
            import latex

            if not latex.has_package(latex.strip_comments(pre), "setspace")[0]:
                pre += "\\usepackage{setspace} % added by SubmitReady\n"
            pre += cmd + " % added by SubmitReady\n"
            done.append(f"Set line spacing to {v}")
    out.write_text(pre + rest, encoding="utf-8")
    return done


def keep_previous(out: Path) -> str:
    """Never overwrite an output silently: an existing file is renamed with the time, and kept."""
    if not out.exists():
        return ""
    from datetime import datetime
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    kept = out.with_name(f"{out.stem}.previous-{stamp}{out.suffix}")
    out.rename(kept)
    return f"An older {out.name} was already there; it was kept as {kept.name}"


def format_docx(src: Path, req: dict, out: Path) -> list[str]:
    """Apply layout-only fixes requested by the requirements. Never touches text runs."""
    if src.suffix.lower() == ".tex":
        return format_tex(src, req, out)
    wanted = {r["check"]["type"]: r["check"] for r in req["requirements"] if "check" in r}
    done = []
    shutil.copy(src, out)
    with zipfile.ZipFile(src) as zin:
        doc = etree.fromstring(zin.read("word/document.xml"))
        styles = etree.fromstring(zin.read("word/styles.xml")) if "word/styles.xml" in zin.namelist() else None
        if "line_numbers" in wanted and wanted["line_numbers"].get("value", True):
            added = 0
            for s in doc.iter(q("sectPr")):
                if s.find(q("lnNumType")) is None:
                    ln = etree.Element(q("lnNumType"))
                    ln.set(q("countBy"), "1")
                    ln.set(q("restart"), "continuous")
                    # schema order: lnNumType comes after pgMar/paperSrc/pgBorders
                    anchor = None
                    for name in ("pgBorders", "paperSrc", "pgMar", "pgSz"):
                        anchor = s.find(q(name))
                        if anchor is not None:
                            break
                    if anchor is not None:
                        anchor.addnext(ln)
                    else:
                        s.insert(0, ln)
                    added += 1
            if added:
                done.append("Turned on continuous line numbers")
        elif "line_numbers" in wanted:                       # the journal asks for no line numbers
            removed = 0
            for s in doc.iter(q("sectPr")):
                for ln in s.findall(q("lnNumType")):
                    s.remove(ln)
                    removed += 1
            if removed:
                done.append("Turned off line numbers")
        if "line_spacing" in wanted and styles is not None:
            target = int(round(wanted["line_spacing"]["value"] * 240))
            for sp in doc.iter(q("spacing")):
                if sp.get(q("line")) and sp.get(q("lineRule"), "auto") == "auto":
                    sp.set(q("line"), str(target))
            for s in styles.iter(q("style")):
                if s.get(q("styleId")) == "Normal":
                    ppr = s.find(q("pPr"))
                    if ppr is None:
                        ppr = etree.SubElement(s, q("pPr"))
                    sp = ppr.find(q("spacing"))
                    if sp is None:
                        sp = etree.SubElement(ppr, q("spacing"))
                    sp.set(q("line"), str(target))
                    sp.set(q("lineRule"), "auto")
            done.append(f"Set line spacing to {wanted['line_spacing']['value']}")
        data = {"word/document.xml": etree.tostring(doc, xml_declaration=True, encoding="UTF-8", standalone=True)}
        if styles is not None:
            data["word/styles.xml"] = etree.tostring(styles, xml_declaration=True, encoding="UTF-8", standalone=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                zout.writestr(item, data.get(item.filename, zin.read(item.filename)))
    return done


def main(argv=None) -> int:
    # Windows terminals and pipes default to a legacy code page; manuscripts contain Unicode.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="SubmitReady prepare")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze")
    a.add_argument("docx", type=Path)
    c = sub.add_parser("check")
    c.add_argument("docx", type=Path)
    c.add_argument("requirements", type=Path)
    c.add_argument("--report", type=Path)
    c.add_argument("--json", action="store_true")
    f = sub.add_parser("format")
    f.add_argument("docx", type=Path)
    f.add_argument("requirements", type=Path)
    f.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    if args.cmd == "analyze":
        print(json.dumps(analyze(args.docx), ensure_ascii=False, indent=1))
        return 0
    req = json.loads(args.requirements.read_text(encoding="utf-8"))
    if args.cmd == "check":
        facts = analyze(args.docx)
        res = check(facts, req)
        md = checklist_markdown(facts, req, res)
        if args.report:
            args.report.write_text(md, encoding="utf-8")
        print(json.dumps(res, ensure_ascii=False, indent=1) if args.json else md)
        return 1 if any(r["status"] in ("FIX", "STYLE", "AUTO") for r in res) else 0
    if args.out.resolve() == args.docx.resolve():
        raise SystemExit("Write the formatted copy to a new name; your manuscript stays as it is.")
    kept = keep_previous(args.out)
    if kept:
        print(kept)
    done = format_docx(args.docx, req, args.out)
    print("\n".join(done) or "Nothing to change.")
    print(f"Saved {args.out}. Now run: python scripts/lock.py verify <lock.json> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
