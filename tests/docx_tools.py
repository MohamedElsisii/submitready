"""Helpers for building and mutating test .docx files (no real manuscripts needed)."""
from __future__ import annotations

import base64
import json
import random
import shutil
import zipfile
from pathlib import Path

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
NS = {"w": W, "m": M}


def q(tag: str) -> str:
    ns, t = tag.split(":")
    return f"{{{NS[ns]}}}{t}"


def read_doc(path: Path):
    with zipfile.ZipFile(path) as z:
        return etree.fromstring(z.read("word/document.xml"))


def write_doc(src: Path, dst: Path, root) -> None:
    """Copy src to dst, replacing word/document.xml with root."""
    data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            zout.writestr(item, data if item.filename == "word/document.xml" else zin.read(item.filename))


def _sdt_tag(sdt) -> str:
    t = sdt.find(f"{q('w:sdtPr')}/{q('w:tag')}")
    return t.get(q("w:val"), "") if t is not None else ""


def text_nodes(root, where: str = "locked"):
    """w:t / m:t nodes. where = 'locked' (normal text), 'citation' or 'bibliography'.

    Citations and bibliographies are recognised both as content controls (Mendeley Cite, Zotero
    with content controls) and as Word fields (Zotero, EndNote), which can span several runs and
    paragraphs."""
    import lock as _lock
    out = []
    fields: list[dict] = []
    for el in root.iter():
        if el.tag == q("w:fldChar"):
            kind = el.get(q("w:fldCharType"))
            if kind == "begin":
                fields.append({"instr": "", "phase": "instr"})
            elif kind == "separate" and fields:
                fields[-1]["phase"] = "result"
            elif kind == "end" and fields:
                fields.pop()
            continue
        if el.tag == q("w:instrText"):
            if fields and fields[-1]["phase"] == "instr":
                fields[-1]["instr"] += el.text or ""
            continue
        if el.tag not in (q("w:t"), q("m:t")) or not (el.text or "").strip():
            continue
        kind = "locked"
        for f in fields:
            if f["phase"] == "result" and _lock.BIBLIOGRAPHY_FIELD.search(f["instr"]):
                kind = "bibliography"
            elif f["phase"] == "result" and _lock.CITATION_FIELD.search(f["instr"]):
                kind = "citation"
        node = el.getparent()
        while kind == "locked" and node is not None:
            if node.tag == q("w:sdt"):
                tag = _sdt_tag(node).upper()
                if tag.startswith(("MENDELEY_CITATION", "CSL_CITATION", "ZOTERO_ITEM")):
                    kind = "citation"
                elif tag.startswith(("MENDELEY_BIBLIOGRAPHY", "ZOTERO_BIBL", "CSL_BIBLIOGRAPHY")):
                    kind = "bibliography"
                break
            node = node.getparent()
        if kind == where:
            out.append(el)
    return out


def mutate_one_char(text: str, rng: random.Random) -> str:
    """Return text with exactly one small edit: substitute, delete, insert, or swap case."""
    i = rng.randrange(len(text))
    op = rng.choice(["sub", "del", "ins", "case"])
    ch = text[i]
    if op == "case" and ch.isalpha() and ch.swapcase() != ch:
        return text[:i] + ch.swapcase() + text[i + 1:]
    if op == "del" and len(text) > 1:
        return text[:i] + text[i + 1:]
    if op == "ins":
        return text[:i] + rng.choice("abcxyz019.,") + text[i:]
    pool = "0123456789" if ch.isdigit() else "abcdefghijklmnopqrstuvwxyz"
    new = rng.choice([c for c in pool if c != ch.lower()])
    return text[:i] + new + text[i + 1:]


def plant_edit(src: Path, dst: Path, rng: random.Random, where: str = "locked") -> tuple[str, str]:
    root = read_doc(src)
    nodes = text_nodes(root, where)
    node = rng.choice(nodes)
    before = node.text
    after = before
    while after == before:
        after = mutate_one_char(before, rng)
    node.text = after
    write_doc(src, dst, root)
    return before, after


def make_sample(path: Path) -> Path:
    """A small synthetic manuscript with a table, an equation, a Mendeley-style
    citation and a Mendeley-style bibliography block."""
    import docx

    d = docx.Document()
    d.add_paragraph("A synthetic manuscript for testing the text lock")
    d.add_paragraph("Abstract")
    p = d.add_paragraph("Breast cancer has distinct molecular subtypes ")
    _add_citation(p._p, "[1]", "Obeagu")
    p.add_run(". Luminal tumors account for 60% of cases.")
    d.add_paragraph("Methods")
    d.add_paragraph("We trained 33 models on 4,329 samples with a 0.05 threshold.")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "Gene"
    t.cell(0, 1).text = "log2FC"
    t.cell(1, 0).text = "SEPTIN2"
    t.cell(1, 1).text = "1.84"
    eq = d.add_paragraph("The score is ")
    _add_math(eq._p, "R2=1-SSres/SStot")
    d.add_paragraph("Results")
    d.add_paragraph("Fourteen genes overlapped between observed and predicted sets.")
    d.add_paragraph("References")
    _add_bibliography(d, ["[1] Obeagu EI. Breast cancer: a review. Medicine. 2024;103:e36905."])
    d.save(path)
    return path


def _el(tag, **attrs):
    e = etree.Element(q(tag))
    for k, v in attrs.items():
        e.set(q(k), v)
    return e


def _add_citation(p, text: str, author: str):
    payload = base64.b64encode(json.dumps({"citationItems": [{"itemData": {"author": [{"family": author}]}}]}).encode()).decode()
    sdt = _el("w:sdt")
    pr = _el("w:sdtPr")
    pr.append(_el("w:tag", **{"w:val": "MENDELEY_CITATION_v3_" + payload}))
    sdt.append(pr)
    content = _el("w:sdtContent")
    r = _el("w:r")
    t = _el("w:t")
    t.text = text
    r.append(t)
    content.append(r)
    sdt.append(content)
    p.append(sdt)


def _add_math(p, text: str):
    omath = etree.SubElement(p, q("m:oMath"))
    r = etree.SubElement(omath, q("m:r"))
    t = etree.SubElement(r, q("m:t"))
    t.text = text


def _add_bibliography(d, entries):
    body = d.element.body
    sdt = _el("w:sdt")
    pr = _el("w:sdtPr")
    pr.append(_el("w:tag", **{"w:val": "MENDELEY_BIBLIOGRAPHY"}))
    sdt.append(pr)
    content = _el("w:sdtContent")
    for e in entries:
        p = _el("w:p")
        r = _el("w:r")
        t = _el("w:t")
        t.text = e
        r.append(t)
        p.append(r)
        content.append(p)
    sdt.append(content)
    body.insert(len(body) - 1, sdt)  # before sectPr


def copy(src: Path, dst: Path) -> Path:
    shutil.copy(src, dst)
    return dst
