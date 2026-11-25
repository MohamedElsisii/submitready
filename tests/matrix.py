#!/usr/bin/env python3
"""Big robustness test: many real articles x 4 citation modes x Word quirks.

    python tests/matrix.py <folder of JATS .xml> <work folder> [--limit N] [--libreoffice]

For each openly licensed article (JATS XML, for example from Europe PMC) it builds Word
manuscripts with Zotero, EndNote, numbered and author-date citations, then makes variants
that often break tools: text boxes, equations, footnotes, a landscape section, headings typed
as bold text instead of Word styles, Unicode, merged table cells, tracked changes, comments,
and (with --libreoffice) a LibreOffice round trip.

Every file goes through the real pipeline: lock, analyze, check against several journals,
format, lock verify of the formatted copy, and the reference audit. The report lists:
  CRASH    a script raised an error
  LOCK     the formatted copy changed locked text (must never happen)
  REFUSE   tracked changes or comments were not refused (they must be)
  MISS     a fact SubmitReady read differs from the article's own XML (abstract length,
           keywords, references, figures, live citations)

Files are written to the work folder only. Never commit them: they are built from real papers.
"""
from __future__ import annotations

import argparse
import copy
import json
import random
import re
import shutil
import subprocess
import sys
import traceback
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import jats_to_docx as jd  # noqa: E402
from lxml import etree  # noqa: E402

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
q = lambda t: f"{{{W}}}{t}"  # noqa: E731
JOURNALS = ["plos.2026-11-24.json", "springer.2026-11-24.json", "spandidos.2026-11-25.json"]
MODES = ["zotero", "endnote", "numbered", "authoryear"]


# ---------------------------------------------------------------- truth from the XML
def truth(xml: Path) -> dict:
    root = etree.parse(str(xml)).getroot()
    abs_el = jd.main_abstract(root)
    words = len(" ".join(jd.text(p) for p in abs_el.iter("p")).split()) if abs_el is not None else 0
    return {"abstract_words": words, "keywords": len(root.findall(".//kwd")),
            "references": len(root.findall(".//back//ref")), "figures": len(root.findall(".//fig"))}


# ---------------------------------------------------------------- variants
def _edit(src: Path, dst: Path, fn) -> Path:
    with zipfile.ZipFile(src) as z:
        files = {n: z.read(n) for n in z.namelist()}
    root = etree.fromstring(files["word/document.xml"])
    extra = fn(root, files) or {}
    files["word/document.xml"] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    files.update(extra)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in files.items():
            z.writestr(n, b)
    return dst


def _body_paras(root):
    return [p for p in root.find(q("body")).iter(q("p")) if "".join(t.text or "" for t in p.iter(q("t"))).strip()]


def _new_p(text: str):
    p = etree.Element(q("p"))
    r = etree.SubElement(p, q("r"))
    t = etree.SubElement(r, q("t"))
    t.text = text
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    return p


def v_textbox(root, files):
    paras = _body_paras(root)
    p = paras[len(paras) // 2]
    box = etree.fromstring(
        f'<w:r xmlns:w="{W}" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
        '<w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml"><v:textbox><w:txbxContent>'
        f'<w:p><w:r><w:t>Box text: a note placed in a text box by the author.</w:t></w:r></w:p>'
        '</w:txbxContent></v:textbox></v:shape></w:pict></w:r>')
    p.append(box)


def v_equation(root, files):
    p = _body_paras(root)[min(10, len(_body_paras(root)) - 1)]
    eq = etree.fromstring(f'<m:oMath xmlns:m="{M}"><m:r><m:t>E=m</m:t></m:r><m:sSup><m:e><m:r><m:t>c</m:t></m:r></m:e>'
                          '<m:sup><m:r><m:t>2</m:t></m:r></m:sup></m:sSup></m:oMath>')
    p.append(eq)


def v_footnote(root, files):
    fn_xml = (f'<w:footnotes xmlns:w="{W}"><w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
              '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>'
              '<w:footnote w:id="1"><w:p><w:r><w:t xml:space="preserve">A footnote added for testing.</w:t></w:r></w:p></w:footnote></w:footnotes>')
    p = _body_paras(root)[min(6, len(_body_paras(root)) - 1)]
    r = etree.SubElement(p, q("r"))
    rpr = etree.SubElement(r, q("rPr"))
    etree.SubElement(rpr, q("vertAlign")).set(q("val"), "superscript")
    etree.SubElement(r, q("footnoteReference")).set(q("id"), "1")
    ct = files["[Content_Types].xml"].decode()
    if "footnotes+xml" not in ct:
        ct = ct.replace("</Types>", '<Override PartName="/word/footnotes.xml" ContentType="application/'
                        'vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/></Types>')
    rels = files["word/_rels/document.xml.rels"].decode()
    if "footnotes.xml" not in rels:
        rels = rels.replace("</Relationships>", '<Relationship Id="rIdFn1" Type="http://schemas.openxmlformats.org/'
                            'officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/></Relationships>')
    return {"word/footnotes.xml": fn_xml.encode(), "[Content_Types].xml": ct.encode(),
            "word/_rels/document.xml.rels": rels.encode()}


def v_sections(root, files):
    paras = _body_paras(root)
    p = paras[len(paras) // 2]
    ppr = p.find(q("pPr"))
    if ppr is None:
        ppr = etree.Element(q("pPr"))
        p.insert(0, ppr)
    sect = etree.SubElement(ppr, q("sectPr"))
    sz = etree.SubElement(sect, q("pgSz"))
    sz.set(q("w"), "16838")
    sz.set(q("h"), "11906")
    sz.set(q("orient"), "landscape")


def v_plain_headings(root, files):
    for p in root.iter(q("p")):
        st = p.find(f"{q('pPr')}/{q('pStyle')}")
        if st is not None and st.get(q("val"), "").lower().startswith("heading"):
            st.getparent().remove(st)
            for r in p.iter(q("r")):
                rpr = r.find(q("rPr"))
                if rpr is None:
                    rpr = etree.Element(q("rPr"))
                    r.insert(0, rpr)
                etree.SubElement(rpr, q("b"))


def v_unicode(root, files):
    paras = _body_paras(root)
    p = paras[len(paras) // 2]
    p.addnext(_new_p("Units and scripts: 37 °C, 5 µM, α-helix, β-sheet, p < 0.05, "
                     "‘quotes’ “double”, ملخص عربي."))


def v_merged_table(root, files):
    paras = _body_paras(root)
    tbl = etree.fromstring(
        f'<w:tbl xmlns:w="{W}"><w:tblPr/><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>'
        '<w:tr><w:tc><w:p><w:r><w:t>Gene</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr>'
        '<w:tr><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>TP53</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>1.2</w:t></w:r></w:p></w:tc></w:tr>'
        '<w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc><w:tc><w:p><w:r><w:t>3.4</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
    paras[len(paras) // 3].addnext(tbl)


def v_tracked(root, files):
    p = _body_paras(root)[min(7, len(_body_paras(root)) - 1)]
    ins = etree.SubElement(p, q("ins"))
    ins.set(q("id"), "901")
    ins.set(q("author"), "Tester")
    r = etree.SubElement(ins, q("r"))
    etree.SubElement(r, q("t")).text = " inserted words"


def v_comment(root, files):
    p = _body_paras(root)[min(7, len(_body_paras(root)) - 1)]
    p.insert(len(p) - 1 if len(p) else 0, etree.Element(q("commentRangeStart")))
    p[-2].set(q("id"), "0")
    e = etree.SubElement(p, q("commentRangeEnd"))
    e.set(q("id"), "0")
    r = etree.SubElement(p, q("r"))
    etree.SubElement(r, q("commentReference")).set(q("id"), "0")
    com = f'<w:comments xmlns:w="{W}"><w:comment w:id="0" w:author="Tester"><w:p><w:r><w:t>Check this.</w:t></w:r></w:p></w:comment></w:comments>'
    ct = files["[Content_Types].xml"].decode().replace(
        "</Types>", '<Override PartName="/word/comments.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/></Types>')
    rels = files["word/_rels/document.xml.rels"].decode().replace(
        "</Relationships>", '<Relationship Id="rIdCm1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/comments" Target="comments.xml"/></Relationships>')
    return {"word/comments.xml": com.encode(), "[Content_Types].xml": ct.encode(),
            "word/_rels/document.xml.rels": rels.encode()}


VARIANTS = {"base": None, "textbox": v_textbox, "equation": v_equation, "footnote": v_footnote,
            "sections": v_sections, "plain_headings": v_plain_headings, "unicode": v_unicode,
            "merged_table": v_merged_table, "tracked": v_tracked, "comment": v_comment}
MUST_REFUSE = {"tracked", "comment"}


def libreoffice(src: Path, outdir: Path) -> Path | None:
    tmp = outdir / "_lo"
    tmp.mkdir(exist_ok=True)
    r = subprocess.run(["soffice", "--headless", "--convert-to", "docx:MS Word 2007 XML", "--outdir", str(tmp), str(src)],
                       capture_output=True, text=True, timeout=180)
    out = tmp / src.name
    if r.returncode or not out.exists():
        return None
    dst = src.with_name(src.stem.replace("_base", "_libreoffice") + ".docx")
    shutil.move(str(out), dst)
    return dst


# ---------------------------------------------------------------- pipeline
def run(cmd: list[str], timeout: int = 300) -> tuple[int, str, str]:
    r = subprocess.run([sys.executable, *cmd], capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def crashed(rc: int, err: str) -> bool:
    return "Traceback (most recent call last)" in err


def pipeline(docx: Path, variant: str, mode: str, tr: dict, out: Path) -> list[dict]:
    probs = []

    def add(kind, step, detail):
        probs.append({"file": docx.name, "variant": variant, "mode": mode, "kind": kind, "step": step, "detail": detail[:400]})

    lock = out / (docx.stem + ".lock.json")
    rc, so, se = run([str(SCRIPTS / "lock.py"), "create", str(docx), "--out", str(lock)])
    if crashed(rc, se):
        add("CRASH", "lock create", se.strip().splitlines()[-1])
        return probs
    if variant in MUST_REFUSE:
        if rc != 3:
            add("REFUSE", "lock create", f"exit {rc}: {variant} was not refused")
        return probs
    if rc != 0:
        add("CRASH", "lock create", f"exit {rc}: {(so + se).strip()[-300:]}")
        return probs

    rc, so, se = run([str(SCRIPTS / "prepare.py"), "analyze", str(docx)])
    if crashed(rc, se):
        add("CRASH", "analyze", se.strip().splitlines()[-1])
    else:
        try:
            f = json.loads(so)
            if tr["abstract_words"] >= 30 and abs(f["abstract_words"] - tr["abstract_words"]) > max(15, 0.15 * tr["abstract_words"]):
                add("MISS", "abstract", f"read {f['abstract_words']} words, XML has {tr['abstract_words']}")
            if tr["keywords"] and len(f["keywords"]) != tr["keywords"]:
                add("MISS", "keywords", f"read {len(f['keywords'])}, XML has {tr['keywords']}")
            if tr["references"] and abs(f["references"] - tr["references"]) > max(1, 0.05 * tr["references"]):
                add("MISS", "references", f"read {f['references']}, XML has {tr['references']}")
            live = zipfile.ZipFile(docx).read("word/document.xml").count(b"ADDIN ZOTERO_ITEM") + \
                zipfile.ZipFile(docx).read("word/document.xml").count(b"ADDIN EN.CITE")
            if live and "typed" in str(f.get("citation_format", "")):
                add("MISS", "citation format", f"live {mode} citations reported as: {f['citation_format']}")
            if variant == "plain_headings" and len(f.get("section_where", [])) < 2:
                add("MISS", "sections", f"headings typed as bold text: only {f.get('section_where')} found")
        except Exception as e:  # noqa: BLE001
            add("CRASH", "analyze", f"bad JSON: {e}")

    for j in JOURNALS:
        req = ROOT / "journals" / "requirements" / j
        rc, so, se = run([str(SCRIPTS / "prepare.py"), "check", str(docx), str(req), "--json"])
        if crashed(rc, se):
            add("CRASH", f"check {j}", se.strip().splitlines()[-1])
            continue
        fmt = out / f"{docx.stem}__{j.split('.')[0]}.docx"
        rc, so, se = run([str(SCRIPTS / "prepare.py"), "format", str(docx), str(req), "--out", str(fmt)])
        if crashed(rc, se):
            add("CRASH", f"format {j}", se.strip().splitlines()[-1])
            continue
        if fmt.exists():
            rc, so, se = run([str(SCRIPTS / "lock.py"), "verify", str(lock), str(fmt)])
            if crashed(rc, se):
                add("CRASH", f"verify {j}", se.strip().splitlines()[-1])
            elif rc == 2:
                add("LOCK", f"verify {j}", (so + se).strip()[-400:])
            fmt.unlink()
    rc, so, se = run([str(SCRIPTS / "refs.py"), "audit", str(docx), "--json"])
    if crashed(rc, se):
        add("CRASH", "refs audit", se.strip().splitlines()[-1])
    return probs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xml_dir", type=Path)
    ap.add_argument("work", type=Path)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--libreoffice", action="store_true")
    ap.add_argument("--variants", default=",".join(VARIANTS))
    ap.add_argument("--modes", default=",".join(MODES))
    ap.add_argument("--jobs", type=int, default=2)
    a = ap.parse_args(argv)
    a.work.mkdir(parents=True, exist_ok=True)
    xmls = sorted(a.xml_dir.glob("*.xml"))[: a.limit or None]
    variants = a.variants.split(",")
    modes = a.modes.split(",")
    probs, todo = [], []
    for xml in xmls:
        try:
            tr = truth(xml)
        except Exception as e:  # noqa: BLE001
            probs.append({"file": xml.name, "variant": "-", "mode": "-", "kind": "CRASH", "step": "read XML", "detail": str(e)})
            continue
        for mode in modes:
            base = a.work / f"{xml.stem}_{mode}_base.docx"
            try:
                jd.build(xml, base, mode)
            except Exception:  # noqa: BLE001
                probs.append({"file": xml.name, "variant": "-", "mode": mode, "kind": "CRASH", "step": "build",
                              "detail": traceback.format_exc().strip().splitlines()[-1]})
                continue
            for v in variants:
                if v == "base":
                    todo.append((base, v, mode, tr))
                elif v in VARIANTS:
                    dst = a.work / f"{xml.stem}_{mode}_{v}.docx"
                    try:
                        todo.append((_edit(base, dst, VARIANTS[v]), v, mode, tr))
                    except Exception:  # noqa: BLE001
                        probs.append({"file": dst.name, "variant": v, "mode": mode, "kind": "CRASH", "step": "make variant",
                                      "detail": traceback.format_exc().strip().splitlines()[-1]})
            if a.libreoffice and mode in ("zotero", "numbered"):
                lo = libreoffice(base, a.work)
                if lo:
                    todo.append((lo, "libreoffice", mode, tr))
    print(f"{len(todo)} files to test", flush=True)

    def one(job):
        f, v, mode, tr = job
        try:
            return pipeline(f, v, mode, tr, a.work)
        except subprocess.TimeoutExpired as e:
            return [{"file": f.name, "variant": v, "mode": mode, "kind": "CRASH", "step": "timeout", "detail": str(e)[:200]}]

    from concurrent.futures import ThreadPoolExecutor
    n = 0
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        for job, res in zip(todo, ex.map(one, todo)):
            n += 1
            probs += res
            if n % 25 == 0 or res:
                print(f"[{n}/{len(todo)}] {job[0].name}: {len(res)} problems", flush=True)
            (a.work / "matrix.json").write_text(json.dumps(probs, indent=1, ensure_ascii=False), encoding="utf-8")
    (a.work / "matrix.json").write_text(json.dumps(probs, indent=1, ensure_ascii=False), encoding="utf-8")
    kinds: dict[str, int] = {}
    for p in probs:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    print(f"\n{n} files. " + (", ".join(f"{k} {v}" for k, v in sorted(kinds.items())) or "no problems"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
