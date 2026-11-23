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
    ("contributions", r"author('?s)? contributions?|credit authorship.*"),
    ("ai", r"(generative )?ai (use |usage )?(statement|disclosure)|use of (generative )?ai.*|writing assistance disclosure"),
    ("declarations", r"declarations"),
    ("references", r"references|bibliography|literature cited"),
    ("figure_legends", r"figure (and table )?(legends|captions)|figure legends"),
    ("supplementary", r"supplementary( material| information| files?)?( legends)?|supporting information"),
    ("highlights", r"(article )?highlights"),
    ("plain_summary", r"plain language summary"),
]
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


def _section_of(text: str) -> str | None:
    t = re.sub(r"\s+", " ", text).strip()
    for k, rx in SECTION_RE:
        if rx.match(t):
            return k
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
        return "unknown (no live citations found)", []
    if re.search(r"\[\d", s):
        return "numeric, square brackets", samples[:5]
    if re.search(r"\(\d", s) or re.search(r"<sup>|^\d", s):
        return "numeric, parentheses", samples[:5]
    if re.search(r"\(\D+,? \d{4}", s):
        return "author-date", samples[:5]
    return "unknown", samples[:5]


def analyze(path: Path) -> dict:
    z = zipfile.ZipFile(path)
    xml_bytes = z.read("word/document.xml")
    xml = xml_bytes.decode("utf-8")
    root = etree.fromstring(xml_bytes)
    names, styles_bold = _style_name_map(z)
    body = root.find(q("body"))

    paras = []  # (index, text, is_heading, section, in_table, style)
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
                      "table": in_table, "bib": in_bib, "style": style})

    # title = first non-empty paragraph
    title = paras[0]["text"] if paras else ""

    # walk sections
    current = "front"
    order, seen = [], set()
    abstract_parts, kw_text = [], ""
    main_words, main_started, main_ended = 0, False, False
    captions = []
    for p in paras:
        t = p["text"]
        inline = re.match(r"^(abstract|key\s*-?\s*words?)\s*[:.]\s*(.+)$", t, re.I)
        if p["heading"] and p["section"]:
            current = p["section"]
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
    for c in captions:
        if c["supp"]:
            continue
        if c["section"] in ("figure_legends", "references", "supplementary") or c["i"] > len(paras) * 0.85:
            placement["end"] += 1
        else:
            placement["in_text"] += 1
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
        "abstract_words": words(" ".join(abstract_parts)),
        "keywords": keywords,
        "main_text_words": main_words,
        "sections_found": order,
        "headings": [p["text"] for p in paras if p["heading"]],
        "figures": figs,
        "tables": tabs,
        "captions_placement": dict(placement),
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
                key = {"abstract": "abstract_words", "main": "main_text_words"}[c["target"]]
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
                pl = facts["captions_placement"]
                want = c["value"]
                ok = pl.get(want, 0) > 0 and sum(v for k, v in pl.items() if k != want) == 0
                status, found = ("PASS" if ok else "FIX"), ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in pl.items()) or "no captions found"
            elif t == "citation_format":
                ok = facts["citation_format"].startswith(c["value"])
                status = "PASS" if ok else "STYLE"
                found = f"{facts['citation_format']} e.g. {', '.join(facts['citation_samples'][:3])}"
            elif t == "supp_label_style":
                s = facts["supplementary_label_style"]
                ok = s is None or s == c["value"]
                status, found = ("PASS" if ok else "FIX"), f"{s or 'none found'}"
            elif t == "max_count":
                v = len(facts[c["target"]])
                status, found = ("PASS" if v <= c["value"] else "FIX"), f"{v}"
            elif t == "manual":
                status = "CHECK"
        except KeyError as e:
            status, found = "CHECK", f"could not measure ({e})"
        out.append({"id": r["id"], "rule": r["rule"], "status": status, "found": found,
                    "quote": r.get("quote", ""), "source": req["sources"][r.get("source", 0)]})
    return out


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
              f"- Title: {facts['title_chars']} characters",
              f"- Abstract: {facts['abstract_words']} words; keywords: {len(facts['keywords'])}",
              f"- Main text (Introduction to end of Discussion/Conclusion, without headings, captions and tables): {facts['main_text_words']:,} words",
              f"- Sections in order: {' > '.join(facts['sections_found'])}",
              f"- Figures: {len(facts['figures'])}, tables: {len(facts['tables'])}, images inside the file: {facts['figures_embedded']}",
              f"- Supplementary items mentioned: {len(facts['supplementary_items'])} ({facts['supplementary_label_style'] or 'none'} style)",
              f"- Line numbers: {'on' if facts['line_numbers'] else 'off'}; page numbers: {'yes' if facts['page_numbers'] else 'no'}; line spacing: {facts['line_spacing']}",
              f"- Citations: {facts['citation_format']}; references: {facts['references']}",
              ""]
    if req.get("csl_style"):
        lines += [f"Reference style: choose **{req['csl_style']['title']}** in your reference manager "
                  f"(CSL id `{req['csl_style']['id']}`, [source]({req['csl_style']['source']})).", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- safe formatting
def format_docx(src: Path, req: dict, out: Path) -> list[str]:
    """Apply layout-only fixes requested by the requirements. Never touches text runs."""
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
    done = format_docx(args.docx, req, args.out)
    print("\n".join(done) or "Nothing to change.")
    print(f"Saved {args.out}. Now run: python scripts/lock.py verify <lock.json> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
