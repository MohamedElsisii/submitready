#!/usr/bin/env python3
"""SubmitReady approved changes.

Two kinds of change are possible, and only after the author approves each one:

- move_out: take a whole section (for example Funding) out of the submission copy,
  word for word, because the journal wants it in the submission form instead.
  The text stays in submission_form.md.
- relabel: swap supplementary labels exactly as listed, for example "Table S1" -> "S1 Table".
  Ranges and lists ("Table S1-S3", "Table S1, S2") are left for the author.

    python changes.py plan  <copy.docx> <original.lock.json> <requirements.json> [--supp DIR] --out plan.json
    (the author sets "approved": true on the changes they want)
    python changes.py apply <copy.docx> plan.json <original.lock.json> --out <copy_changed.docx>

`apply` checks its own result with the text lock and deletes the output if anything
other than the approved changes differs. The original manuscript is never touched.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lock as lockmod  # noqa: E402
import prepare  # noqa: E402

W = lockmod.W
MOVABLE = {"funding": "Funding", "competing": "Competing interests", "data": "Data availability",
           "contributions": "Author contributions", "ethics": "Ethics", "ai": "AI use",
           "acknowledgements": "Acknowledgements"}
LABEL_WORDS = r"Table|Figure|Fig\.|Fig|Code|Data|Dataset|File|Appendix|Video|Movie|Text|Note|Audio"


def q(tag: str) -> str:
    return f"{{{W}}}{tag}"


# ---------------------------------------------------------------- lock-aligned paragraphs
def _lock_paragraphs(root) -> list[tuple[int, object, str]]:
    """(lock index, w:p element, lock text) for body paragraphs, exactly as the lock counts them."""
    ex = lockmod.Extractor()
    out, idx = [], 0
    for p in lockmod._paragraphs(root, "body"):
        in_bib = ex.in_bibliography(p)
        text, _ = ex.paragraph_text(p)
        if in_bib:
            continue
        text = lockmod.normalize(text)
        if not text.strip():
            continue
        out.append((idx, p, text))
        idx += 1
    return out


def _same_as_lock(units: list[tuple[int, object, str]], lock: dict) -> bool:
    body = [u for u in lock["units"] if u["part"] == "body"]
    return [t for _, _, t in units] == [u["text"] for u in body]


# ---------------------------------------------------------------- plan
def plan(copy: Path, lock_path: Path, req_path: Path, supp: Path | None = None) -> dict:
    if copy.suffix.lower() != ".docx":
        raise SystemExit("Approved changes work on Word files. For LaTeX, the to-do list shows what to change.")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    req = json.loads(req_path.read_text(encoding="utf-8"))
    root = etree.fromstring(zipfile.ZipFile(copy).read("word/document.xml"))
    units = _lock_paragraphs(root)
    if not _same_as_lock(units, lock):
        raise SystemExit("This copy's text differs from the lock. Make the plan on the copy right after /prepare, "
                         "or lock the manuscript again after your own edits.")
    # same document, two parses: match paragraphs by their XML path
    el_index = {el.getroottree().getpath(el): (i, t) for i, el, t in units}
    facts = prepare.analyze(copy)
    results = {r["id"]: r for r in prepare.check(facts, req)}
    _, _, paras = prepare.read_paragraphs(copy)
    changes = []
    taken: set[int] = set()

    # 1. sections the journal wants in the submission form, not in the file
    for rule in req["requirements"]:
        c = rule.get("check", {})
        sec = c.get("section")
        if c.get("type") != "section_absent" or sec not in MOVABLE or results.get(rule["id"], {}).get("status") != "FIX":
            continue
        picked, problems = [], []
        k = 0
        while k < len(paras):
            p = paras[k]
            inline = prepare.inline_statement(p["text"]) if not p["heading"] else None
            if (p["heading"] and p["section"] == sec) or (inline and inline[0] == sec):
                block = [p]
                if p["heading"]:
                    k += 1
                    while k < len(paras) and not paras[k]["heading"] and not prepare.inline_statement(paras[k]["text"]):
                        block.append(paras[k])
                        k += 1
                else:
                    k += 1
                if any(b["table"] or b["bib"] for b in block):
                    problems.append(f"\"{p['text'][:40]}\" contains a table or references; move it by hand")
                    continue
                for b in block:
                    path = b["el"].getroottree().getpath(b["el"])
                    if path in el_index and el_index[path][0] not in taken:
                        i, t = el_index[path]
                        taken.add(i)
                        picked.append({"index": i, "text": t})
                continue
            k += 1
        if picked:
            changes.append({"id": f"move-{sec}", "kind": "move_out", "section": sec,
                            "title": f"Move the {MOVABLE[sec]} section out of the submission copy",
                            "reason": rule["rule"], "quote": rule.get("quote", ""),
                            "paragraphs": picked, "notes": problems, "approved": False})

    # 2. supplementary labels in the journal's style
    for rule in req["requirements"]:
        c = rule.get("check", {})
        if c.get("type") != "supp_label_style" or results.get(rule["id"], {}).get("status") != "FIX":
            continue
        target = c["value"]  # "S1 Table", "Table S1" or "Online Resource 1"
        texts = [t for _, _, t in units]
        pairs = set()
        esm: dict[tuple[str, int], int] = {}
        if target == "Online Resource 1":
            # one sequence in the order of first mention: Table S1 -> Online Resource 1, Code S1 -> Online Resource 2 ...
            rx_any = re.compile(rf"(?<![\w-])(?:(?:Supplementary|Additional)\s+)?"
                                rf"(?:({LABEL_WORDS})\s?S(\d+)|S(\d+)\s({LABEL_WORDS}))(?![\w])")
            for m in rx_any.finditer("\n".join(texts)):
                key = (_kind(m.group(1) or m.group(4)), int(m.group(2) or m.group(3)))
                esm.setdefault(key, len(esm) + 1)
                pairs.add((m.group(0), f"Online Resource {esm[key]}"))
        elif target == "S1 Table":
            for m in re.finditer(rf"(?<![\w-])({LABEL_WORDS})\s?S(\d+)(?![\w])", "\n".join(texts)):
                w = "Fig" if m.group(1) == "Fig." else m.group(1)
                pairs.add((m.group(0), f"S{m.group(2)} {w}"))
        else:
            for m in re.finditer(rf"(?<![\w-])S(\d+)\s({LABEL_WORDS})(?![\w])", "\n".join(texts)):
                pairs.add((m.group(0), f"{m.group(2)} S{m.group(1)}"))
        pairs = sorted(pairs, key=lambda p: (p[0].split()[0], int(re.search(r"\d+", p[0]).group())))
        if esm:
            pairs = sorted(pairs, key=lambda p: int(p[1].rsplit(" ", 1)[1]))
        if not pairs:
            continue
        rx = lockmod.relabel_regex([list(p) for p in pairs])
        n = sum(len(rx.findall(t)) for t in texts)
        # mentions the swap leaves alone (ranges, lists, plurals): the author fixes these
        loose = re.compile(rf"\b(?:{LABEL_WORDS})s?\s?S\d+\s*(?:[-\u2013\u2014,&]|and)\s*S?\d+|\b(?:Tables|Figures|Files)\s+S\d+")
        left = sorted({m.group(0) for t in texts for m in loose.finditer(t)})
        renames = []
        if supp and supp.is_dir():
            for f in sorted(supp.iterdir()):
                if not f.is_file():
                    continue
                if esm:
                    m = re.search(rf"(?:({LABEL_WORDS})[\s_]?S(\d+)|S(\d+)[\s_]({LABEL_WORDS}))", f.stem)
                    key = (_kind(m.group(1) or m.group(4)), int(m.group(2) or m.group(3))) if m else None
                    if key in esm:
                        renames.append([f.name, f"ESM_{esm[key]}{f.suffix.lower()}"])
                    continue
                new = lockmod.relabel_text(f.stem, [list(p) for p in pairs]) + f.suffix
                if new != f.name:
                    renames.append([f.name, new])
        changes.append({"id": "relabel-supp", "kind": "relabel",
                        "title": f"Change supplementary labels to the \"{target}\" style",
                        "reason": rule["rule"], "quote": rule.get("quote", ""),
                        "pairs": [list(p) for p in pairs], "mentions": n, "left_for_you": left,
                        "file_renames": renames, "approved": False})
    return {"tool": "SubmitReady changes", "version": lockmod.VERSION, "copy": copy.name,
            "lock": lock_path.name, "journal": req["journal"], "changes": changes}


def _kind(word: str) -> str:
    w = word.rstrip(".").title()
    return {"Fig": "Figure"}.get(w, w)


def plan_markdown(pl: dict) -> str:
    L = [f"# Changes SubmitReady can make for you: {pl['copy']} for {pl['journal']}", "",
         "Nothing below happens unless you approve it. Your original file is never touched.", ""]
    if not pl["changes"]:
        L.append("Nothing to change.")
    for n, c in enumerate(pl["changes"], 1):
        L += [f"## {n}. {c['title']}", "", f"Why: {c['reason']}" + (f" (\"{c['quote']}\")" if c.get("quote") else ""), ""]
        if c["kind"] == "move_out":
            L.append("These paragraphs leave the submission copy. They stay, word for word, in submission_form.md:")
            L.append("")
            L += [f"> {p['text'][:300]}" for p in c["paragraphs"]]
            L += [f"- {x}" for x in c.get("notes", [])]
        else:
            L.append(f"{c['mentions']} mentions change. Each swap, exactly:")
            L.append("")
            L += [f"- {a} -> {b}" for a, b in c["pairs"]]
            if c["left_for_you"]:
                L += ["", "Left for you to change by hand (ranges and lists): " + "; ".join(c["left_for_you"])]
            if c["file_renames"]:
                L += ["", "Supplementary files are renamed in the package only (your originals keep their names):"]
                L += [f"- {a} -> {b}" for a, b in c["file_renames"]]
        L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------- apply
def _eligible_text_nodes(p, field_stack: list) -> list:
    """w:t nodes of a paragraph that are the author's own text (not citations, not references)."""
    nodes = []

    def in_citation_sdt(el):
        node = el.getparent()
        while node is not None and node is not p:
            if node.tag == q("sdt") and lockmod.CITATION_SDT.match(lockmod._sdt_tag(node)):
                return True
            node = node.getparent()
        return False

    def walk(el):
        for ch in el:
            if not isinstance(ch.tag, str) or lockmod._skip_subtree(ch):
                continue
            if ch.tag == q("fldChar"):
                kind = ch.get(q("fldCharType"))
                if kind == "begin":
                    field_stack.append({"instr": "", "phase": "instr"})
                elif kind == "separate" and field_stack:
                    field_stack[-1]["phase"] = "result"
                elif kind == "end" and field_stack:
                    field_stack.pop()
                continue
            if ch.tag == q("instrText"):
                if field_stack and field_stack[-1]["phase"] == "instr":
                    field_stack[-1]["instr"] += ch.text or ""
                continue
            if ch.tag == q("t"):
                busy = any(f["phase"] == "result" and (lockmod.CITATION_FIELD.search(f["instr"]) or
                                                       lockmod.BIBLIOGRAPHY_FIELD.search(f["instr"])) for f in field_stack)
                if not busy and not in_citation_sdt(ch):
                    nodes.append(ch)
                continue
            walk(ch)

    walk(p)
    return nodes


def _replace_across_runs(nodes: list, rx, mapping: dict) -> int:
    text = "".join(n.text or "" for n in nodes)
    spans = [(m.start(1), m.end(1), mapping[m.group(1)]) for m in rx.finditer(text)]
    if not spans:
        return 0
    starts, pos = [], 0
    for n in nodes:
        starts.append(pos)
        pos += len(n.text or "")

    def locate(off):
        for k in range(len(nodes) - 1, -1, -1):
            if starts[k] <= off:
                return k
        return 0

    for s, e, rep in reversed(spans):
        a, b = locate(s), locate(e - 1)
        ta = nodes[a].text or ""
        if a == b:
            nodes[a].text = ta[:s - starts[a]] + rep + ta[e - starts[a]:]
        else:
            nodes[a].text = ta[:s - starts[a]] + rep
            for k in range(a + 1, b):
                nodes[k].text = ""
            tb = nodes[b].text or ""
            nodes[b].text = tb[e - starts[b]:]
        for k in range(a, b + 1):
            nodes[k].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    return len(spans)


def apply(copy: Path, plan_path: Path, lock_path: Path, out: Path) -> dict:
    pl = json.loads(plan_path.read_text(encoding="utf-8"))
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    approved = [c for c in pl["changes"] if c.get("approved") is True]
    with zipfile.ZipFile(copy) as z:
        root = etree.fromstring(z.read("word/document.xml"))
        units = _lock_paragraphs(root)
        if not _same_as_lock(units, lock):
            raise SystemExit("This copy's text differs from the lock; make a new plan first.")
        by_index = {i: (el, t) for i, el, t in units}
        removed = swapped = 0
        pairs = [list(p) for c in approved if c["kind"] == "relabel" for p in c["pairs"]]
        if pairs:
            rx = lockmod.relabel_regex(pairs)
            mapping = {a: b for a, b in pairs}
            field_stack: list = []
            for p in lockmod._paragraphs(root, "body"):
                swapped += _replace_across_runs(_eligible_text_nodes(p, field_stack), rx, mapping)
        for c in approved:
            if c["kind"] != "move_out":
                continue
            for para in c["paragraphs"]:
                el, t = by_index.get(para["index"], (None, None))
                if el is None or t != lockmod.normalize(para["text"]):
                    raise SystemExit(f"Paragraph {para['index'] + 1} is not the one in the plan; nothing was changed.")
                if el.getparent() is not None:
                    el.getparent().remove(el)
                    removed += 1
        data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zo:
            for item in z.infolist():
                zo.writestr(item, data if item.filename == "word/document.xml" else z.read(item.filename))
    r = lockmod.verify(lock_path, out, lockmod.load_approved(plan_path))
    if r["status"] == 2:
        out.unlink(missing_ok=True)
        raise SystemExit("The result differed from the original in ways you did not approve, so it was deleted. "
                         "Please report this as a bug.")
    return {"removed_paragraphs": removed, "swapped_labels": swapped, "lock": r}


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="SubmitReady approved changes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("plan")
    a.add_argument("copy", type=Path)
    a.add_argument("lock", type=Path)
    a.add_argument("requirements", type=Path)
    a.add_argument("--supp", type=Path)
    a.add_argument("--out", type=Path, required=True)
    b = sub.add_parser("apply")
    b.add_argument("copy", type=Path)
    b.add_argument("plan", type=Path)
    b.add_argument("lock", type=Path)
    b.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "plan":
        pl = plan(args.copy, args.lock, args.requirements, args.supp)
        args.out.write_text(json.dumps(pl, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        md = plan_markdown(pl)
        args.out.with_suffix(".md").write_text(md, encoding="utf-8")
        print(md)
        return 0
    if args.copy.resolve() == args.out.resolve():
        raise SystemExit("Write the changed file to a new name; the copy you give stays as it is.")
    kept = prepare.keep_previous(args.out)
    if kept:
        print(kept)
    r = apply(args.copy, args.plan, args.lock, args.out)
    print(f"Moved {r['removed_paragraphs']} paragraphs out and swapped {r['swapped_labels']} labels. "
          f"Lock: {r['lock']['sentences_unchanged']:,} of {r['lock']['sentences_locked']:,} sentences unchanged, "
          f"{r['lock']['sentences_approved']:,} changed only as approved. Saved {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
