#!/usr/bin/env python3
"""Build a realistic test manuscript (.docx) from an open-access article's JATS XML.

For testing SubmitReady on real-sized papers. Use only openly licensed articles (for example
CC BY preprints from Europe PMC) and never commit the output (keep test files in tests/private/, which git ignores).

Citation modes:
  zotero    live Zotero citations (Word fields with CSL JSON) and a live Zotero bibliography
  endnote   live EndNote citations (Word fields with EndNote XML) and a live EndNote bibliography
  numbered  typed citations like [1] and a typed numbered reference list
  authoryear typed citations like (Smith et al. 2020) and a typed alphabetical reference list

    python scripts/jats_to_docx.py article.xml out.docx --mode zotero
"""
from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path

import docx
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from lxml import etree

XLINK = "{http://www.w3.org/1999/xlink}"


def main_abstract(root):
    """The article's main abstract. Graphical abstracts, highlights and key points are left out."""
    abstracts = root.findall(".//article-meta/abstract") or root.findall(".//abstract")
    for a in abstracts:
        if not a.get("abstract-type") or a.get("abstract-type").lower() in ("abstract", "structured", "summary"):
            return a
    return abstracts[0] if abstracts else None


def text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


# ---------------------------------------------------------------- references
def parse_refs(root) -> dict:
    refs = {}
    for n, r in enumerate(root.findall(".//back//ref"), 1):
        c = next((x for x in (r.find("element-citation"), r.find("mixed-citation"), r.find("citation")) if x is not None), r)
        names = []
        for nm in c.findall(".//name"):
            names.append({"family": text(nm.find("surname")), "given": text(nm.find("given-names"))})
        if not names:
            for col in c.findall(".//collab"):
                names.append({"literal": text(col)})
        doi = next((text(p) for p in c.findall(".//pub-id") if p.get("pub-id-type") == "doi"), "")
        m = re.search(r"(19|20)\d{2}", text(c.find("year")) or text(c))
        item = {"id": f"r{n}", "type": "article-journal", "author": names,
                "title": text(c.find("article-title")) or text(c.find("chapter-title")) or text(c.find("source")),
                "container-title": text(c.find("source")) if c.find("article-title") is not None else "",
                "volume": text(c.find("volume")), "issue": text(c.find("issue")),
                "page": "-".join(x for x in (text(c.find("fpage")), text(c.find("lpage"))) if x),
                "DOI": doi, "issued": {"date-parts": [[int(m.group(0))]]} if m else {},
                "_raw": text(c)}
        refs[r.get("id")] = {k: v for k, v in item.items() if v not in ("", [], {})}
        refs[r.get("id")]["_n"] = n
    return refs


def _surname(a: dict) -> str:
    return a.get("family") or a.get("literal") or "Anon"


def author_year(it: dict) -> str:
    a = it.get("author", [])
    y = str(it.get("issued", {}).get("date-parts", [[""]])[0][0])
    if not a:
        return f"Anon {y}"
    if len(a) == 1:
        return f"{_surname(a[0])} {y}"
    if len(a) == 2:
        return f"{_surname(a[0])} and {_surname(a[1])} {y}"
    return f"{_surname(a[0])} et al. {y}"


def ref_entry(it: dict, numbered: bool) -> str:
    au = ", ".join(f"{_surname(a)} {''.join(w[0] for w in (a.get('given') or '').replace('-', ' ').split())}".strip()
                   for a in it.get("author", [])[:6])
    if len(it.get("author", [])) > 6:
        au += " et al"
    y = str(it.get("issued", {}).get("date-parts", [[""]])[0][0])
    parts = [f"{au} ({y}) {it.get('title', '')}."]
    src = it.get("container-title", "")
    if src:
        vol = it.get("volume", "") + (f"({it['issue']})" if it.get("issue") else "")
        parts.append(f"{src} {vol}" + (f":{it['page']}" if it.get("page") else "") + ".")
    if it.get("DOI"):
        parts.append(f"https://doi.org/{it['DOI']}")
    s = " ".join(p for p in parts if p.strip())
    return f"{it['_n']}. {s}" if numbered else s


# ---------------------------------------------------------------- Word fields
def _run(p, t: str, bold=False):
    r = p.add_run(t)
    r.bold = bold
    return r


def _fld(p, kind: str, instr: str | None = None):
    r = OxmlElement("w:r")
    if kind == "instr":
        it = OxmlElement("w:instrText")
        it.set(qn("xml:space"), "preserve")
        it.text = instr
        r.append(it)
    else:
        fc = OxmlElement("w:fldChar")
        fc.set(qn("w:fldCharType"), kind)
        r.append(fc)
    p._p.append(r)


def field_citation(p, mode: str, items: list[dict], shown: str):
    if mode == "zotero":
        # real Zotero stores formattedCitation as RTF: non-ASCII as \uc0\uN{} (seen in Zotero 10 files)
        rtf = "".join(ch if ord(ch) < 128 else f"\\uc0\\u{ord(ch)}{{}}" for ch in shown)
        data = {"citationID": f"c{abs(hash(shown)) % 10**8}", "properties": {"formattedCitation": rtf, "plainCitation": shown, "noteIndex": 0},
                "citationItems": [{"id": it["_n"], "itemData": {k: v for k, v in it.items() if not k.startswith("_")}} for it in items],
                "schema": "https://github.com/citation-style-language/schema/raw/master/csl-citation.json"}
        instr = f" ADDIN ZOTERO_ITEM CSL_CITATION {json.dumps(data, ensure_ascii=False)} "
    else:
        cites = "".join(
            f"<Cite><Author>{html.escape(_surname((it.get('author') or [{}])[0]))}</Author>"
            f"<Year>{it.get('issued', {}).get('date-parts', [['']])[0][0]}</Year><RecNum>{it['_n']}</RecNum>"
            f"<DisplayText>{html.escape(shown)}</DisplayText><record><rec-number>{it['_n']}</rec-number>"
            f"<ref-type name=\"Journal Article\">17</ref-type><contributors><authors>"
            + "".join(f"<author>{html.escape(_surname(a))}, {html.escape(a.get('given', ''))}</author>" for a in it.get("author", []))
            + f"</authors></contributors><titles><title>{html.escape(it.get('title', ''))}</title>"
            f"<secondary-title>{html.escape(it.get('container-title', ''))}</secondary-title></titles>"
            f"<periodical><full-title>{html.escape(it.get('container-title', ''))}</full-title></periodical>"
            f"<pages>{html.escape(it.get('page', ''))}</pages><volume>{html.escape(it.get('volume', ''))}</volume>"
            f"<number>{html.escape(it.get('issue', ''))}</number>"
            f"<dates><year>{it.get('issued', {}).get('date-parts', [['']])[0][0]}</year></dates>"
            f"<electronic-resource-num>{html.escape(it.get('DOI', ''))}</electronic-resource-num></record></Cite>"
            for it in items)
        instr = f" ADDIN EN.CITE <EndNote>{cites}</EndNote>"
    _fld(p, "begin")
    _fld(p, "instr", instr)
    _fld(p, "separate")
    _run(p, shown)
    _fld(p, "end")


# ---------------------------------------------------------------- build
def build(xml: Path, out: Path, mode: str) -> dict:
    root = etree.parse(str(xml)).getroot()
    refs = parse_refs(root)
    order = sorted(refs.values(), key=lambda it: it["_n"])
    if mode == "authoryear":
        order = sorted(refs.values(), key=lambda it: (author_year(it).lower(), it["_n"]))
    d = docx.Document()
    d.add_paragraph(text(root.find(".//title-group/article-title")))
    auths = [f"{text(c.find('.//given-names'))} {text(c.find('.//surname'))}".strip()
             for c in root.findall(".//contrib-group/contrib[@contrib-type='author']")]
    if auths:
        d.add_paragraph(", ".join(a for a in auths if a))
    for aff in root.findall(".//aff")[:6]:
        d.add_paragraph(text(aff))
    d.add_heading("Abstract", 1)
    main_abs = main_abstract(root)
    for ap in (main_abs.findall(".//p") if main_abs is not None else []) or [main_abs]:
        if ap is not None and text(ap):
            d.add_paragraph(text(ap))
    kw = [text(k) for k in root.findall(".//kwd")]
    if kw:
        d.add_paragraph("Keywords: " + "; ".join(kw))

    cited_n = 0

    def add_par(pel):
        nonlocal cited_n
        p = d.add_paragraph()
        buf: list[str] = []
        later: list = []  # tables found inside the paragraph go right after it

        def flush():
            s = re.sub(r"\s+", " ", "".join(buf))
            if s:
                _run(p, s)
            buf.clear()

        skip: set = set()

        def walk(el, top=False):
            nonlocal cited_n
            if not top:
                tag = el.tag if isinstance(el.tag, str) else ""
                if id(el) in skip:
                    return
                if tag == "xref" and el.get("ref-type") == "bibr":
                    its = [refs[i] for i in (el.get("rid") or "").split() if i in refs]
                    # a range "5–7" is written as two xrefs joined by a dash: one citation of 5, 6 and 7
                    nxt = el.getnext()
                    if its and nxt is not None and (el.tail or "").strip() in ("\u2013", "-") and \
                            nxt.tag == "xref" and nxt.get("ref-type") == "bibr":
                        end = [refs[i] for i in (nxt.get("rid") or "").split() if i in refs]
                        if end:
                            lo, hi = its[0]["_n"], end[-1]["_n"]
                            its = [it for it in refs.values() if lo <= it["_n"] <= hi]
                            skip.add(id(nxt))
                            el.tail, nxt_tail = "", nxt.tail
                            el = el.__copy__()
                            el.tail = nxt_tail
                    if its:
                        flush()
                        if mode == "authoryear":
                            shown = "(" + "; ".join(author_year(it) for it in its) + ")"
                        elif len(its) > 2 and [it["_n"] for it in its] == list(range(its[0]["_n"], its[-1]["_n"] + 1)):
                            shown = f"[{its[0]['_n']}\u2013{its[-1]['_n']}]"
                        else:
                            shown = "[" + ",".join(str(it["_n"]) for it in its) + "]"
                        if mode in ("zotero", "endnote"):
                            field_citation(p, mode, its, shown)
                        else:
                            _run(p, shown)
                        cited_n += 1
                    else:
                        buf.append(text(el))
                    buf.append(el.tail or "")
                    return
                if tag in ("table-wrap", "fig"):
                    if tag == "table-wrap":
                        later.append(el)
                    buf.append(el.tail or "")
                    return
            buf.append(el.text or "")
            for ch in el:
                walk(ch)
            if not top:
                buf.append(el.tail or "")

        walk(pel, top=True)
        flush()
        for tw in later:
            add_table(tw)

    def add_sec(sec, level):
        t = sec.find("title")
        if t is not None and text(t):
            d.add_heading(text(t), min(level, 3))
        for ch in sec:
            tag = ch.tag if isinstance(ch.tag, str) else ""
            if tag == "p":
                add_par(ch)
            elif tag == "sec":
                add_sec(ch, level + 1)
            elif tag == "table-wrap":
                add_table(ch)

    tn = [0]

    def add_table(tw):
        tn[0] += 1
        cap = text(tw.find("caption")) or text(tw.find("label"))
        rest = re.sub(r"^Table\s*\d+\.?\s*", "", cap)  # outside the f-string: Python 3.10 refuses a backslash there
        d.add_paragraph(f"Table {tn[0]}. {rest}".strip())
        rows = tw.findall(".//tr")
        if rows:
            ncol = max(len(r.findall("td")) + len(r.findall("th")) for r in rows) or 1
            t = d.add_table(rows=0, cols=ncol)
            for r in rows[:40]:
                cells = t.add_row().cells
                for i, c in enumerate((r.findall("th") + r.findall("td"))[:ncol]):
                    cells[i].text = text(c)

    body = root.find(".//body")
    for ch in body:
        tag = ch.tag if isinstance(ch.tag, str) else ""
        if tag == "sec":
            if re.match(r"supplementary", text(ch.find("title")), re.I):
                continue
            add_sec(ch, 1)
        elif tag == "p":
            add_par(ch)
    # tables kept apart in <floats-group> (or in boxes) go after the main text, as many authors place them
    placed = set()
    for tw in root.findall(".//floats-group/table-wrap") + root.findall(".//boxed-text//table-wrap"):
        if id(tw) not in placed:
            placed.add(id(tw))
            add_table(tw)
    back = root.find(".//back")
    if back is not None:
        for ack in back.findall("ack"):
            d.add_heading("Acknowledgements", 1)
            for p in ack.findall(".//p"):
                d.add_paragraph(text(p))
        for sec in back.findall("sec"):
            add_sec(sec, 1)
    # references
    d.add_heading("References", 1)
    entries = [ref_entry(it, mode != "authoryear") for it in order]
    if mode in ("zotero", "endnote"):
        instr = (" ADDIN ZOTERO_BIBL {\"uncited\":[],\"omitted\":[],\"custom\":[]} CSL_BIBLIOGRAPHY "
                 if mode == "zotero" else " ADDIN EN.REFLIST ")
        for i, e in enumerate(entries):
            p = d.add_paragraph()
            if i == 0:
                _fld(p, "begin")
                _fld(p, "instr", instr)
                _fld(p, "separate")
            _run(p, e)
            if i == len(entries) - 1:
                _fld(p, "end")
    else:
        for e in entries:
            d.add_paragraph(e)
    # figure legends at the end
    figs = root.findall(".//fig")
    if figs:
        d.add_heading("Figure legends", 1)
        for n, f in enumerate(figs, 1):
            cap = re.sub(r"^(Figure|Fig\.?)\s*\d+[.:]?\s*", "", text(f.find("caption")))
            d.add_paragraph(f"Figure {n}. {cap}")
    d.save(out)
    return {"references": len(refs), "citations": cited_n, "figures": len(figs), "tables": tn[0]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("xml", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--mode", choices=["zotero", "endnote", "numbered", "authoryear"], default="numbered")
    a = ap.parse_args()
    print(build(a.xml, a.out, a.mode))
