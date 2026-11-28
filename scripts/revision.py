#!/usr/bin/env python3
"""SubmitReady revision helper: from a decision letter to a response table.

It splits the letter into numbered comments, word for word, and builds a Word table with
an empty "Response" column and an empty "Where changed" column for you to fill in.
It never writes a response for you, and it never changes the manuscript.

    python revision.py split letter.docx --out comments.json
    python revision.py table comments.json --out response.docx
    python revision.py changes original.docx revised.docx --comments comments.json --out changes.md
    python revision.py status response.docx

split    Finds the reviewer and editor sections and their comments (numbered, lettered or
         bulleted). Lines the journal leaves for your answer ("Author response:") are dropped.
         Instructions about how to answer (tracked changes, page and line numbers) are kept.
table    One table per reviewer: comment ID, the comment as written, Response, Where changed.
changes  Lists the paragraphs you changed, added or removed in the revised manuscript, and for
         each comment the changed paragraphs that share its key terms (a hint, not a decision).
status   Reads the filled-in response table back and lists comments still without a response
         or without a place where you changed the text.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

PLACEHOLDER = re.compile(r"^\s*(author'?s?'? response( and action taken)?|response to reviewer|"
                         r"page and line number\(?s?\)? for changes made|reply)\s*:?\s*$", re.I)
# "Reviewer 1", "Referee: 2", "Reviewer #1 (Recommendations for the authors):" (eLife)
REVIEWER = re.compile(r"^\s*(?:reviewer|referee)\s*(?:#|no\.?|:)?\s*(\d+)\s*(?:\([^)]{0,60}\))?\s*:?\s*$", re.I)
# a consolidated list from the editors ("Essential revisions:" in eLife letters)
EDITOR_LIST = re.compile(r"^\s*(essential|required|summary of) revisions?\s*:?\s*$", re.I)
EDITOR = re.compile(r"^\s*(?:(?:associate|academic|handling|managing|section|guest)\s+)?editor(?:'s)?"
                    r"(?:\s+comments?(?:\s+to\s+(?:the\s+)?authors?)?)?\s*:?\s*(\d+)?\s*:?\s*$", re.I)
BLOCK_START = re.compile(r"^\s*(?:referee\(s\)'?\s+comments|comments to (?:the )?authors?|reviewers?'? comments)\b.*:?\s*$", re.I)
LABEL = re.compile(r"^\s*(general|specific|minor|major|additional)\s+(?:comments?|recommendations?|points?|issues?)\s*:?\s*$"
                   r"|^\s*([A-Z][\w,&/ -]{2,40}):\s*$", re.I)    # "Figures and legends:", "Essential text edits:"
NUM = re.compile(r"^\s*\(?(\d{1,2}|(?:i|ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii))[.)]\s+(.*)$")
LETTER = re.compile(r"^\s*([A-H])[.)]\s+(.*)$")
BULLET = re.compile(r"^\s*(?:[-•–*]|\(\w\))\s*(\S.*)$")
CITED_REF = re.compile(r"^\s*[A-Z][\w'\-]+,\s+(?:[A-Z]\.\s*)+,.*\((?:19|20)\d{2}[a-z]?\)")   # "Saxena, N., ... (2013)."
SECTION_LABEL = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s*$")          # "2.9" or "4." on its own line
STOP = re.compile(r"(post-acceptance services|scientific editing service|if you are considering reworking|"
                  r"look forward to receiving your revision|^\s*sincerely,?\s*$)", re.I)
# Offers the journal asks you to answer (open access, videos): optional rows, one per "Author response:" line
SERVICES = re.compile(r"post-acceptance services", re.I)
HOW_TO = re.compile(r"(track(ed)? changes|highlight the changes|page and line|line numbers?|point[- ]by[- ]point|"
                    r"two versions|response (letter|document)|reply to the (referee|reviewer)|"
                    r"explain(ing)? the changes|marked[- ]up|clean (copy|version))", re.I)
SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


# ---------------------------------------------------------------- reading
def read_lines(path: Path) -> list[str]:
    if path.suffix.lower() == ".docx":
        from docx import Document
        d = Document(str(path))
        lines: list[str] = []
        for p in d.paragraphs:
            lines += p.text.split("\n")
        for t in d.tables:                                  # some letters put comments in a table
            for row in t.rows:
                for cell in row.cells:
                    lines += cell.text.split("\n")
        return lines
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


# ---------------------------------------------------------------- splitting
def _blocks(lines: list[str]) -> tuple[list[str], list[dict]]:
    """Cut the letter into a preamble and reviewer/editor blocks."""
    pre: list[str] = []
    blocks: list[dict] = []
    cur = None
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        m = REVIEWER.match(line)
        e = EDITOR.match(line) if not m else None
        if m:
            cur = {"who": f"Reviewer {m.group(1)}", "lines": []}
            blocks.append(cur)
            continue
        if EDITOR_LIST.match(line):
            cur = {"who": "Editor", "lines": []}
            blocks.append(cur)
            continue
        if e and len(line) < 70:
            n = e.group(1)
            if cur is not None and cur["who"].startswith("Editor") and not cur["lines"]:
                cur["who"] = f"Editor {n}" if n else cur["who"]   # "Editor Comments to Author:" then "Associate Editor: 1"
            else:
                cur = {"who": f"Editor {n}" if n else "Editor", "lines": []}
                blocks.append(cur)
            continue
        if cur is None:
            if BLOCK_START.match(line):
                continue
            pre.append(line)
            continue
        if cur is not None and re.match(r"^\s*(\[editors?['\u2019]?\s+note|thank you for (submitting|sending|your)|dear\s)", line, re.I):
            cur = None   # the editor's letter again (a second round), not a reviewer's point
            pre.append(line)
            continue
        if SERVICES.search(line) and cur is not None:
            cur = {"who": "Editor", "services": True, "lines": []}
            blocks.append(cur)
            continue
        if STOP.search(line):
            cur = None
            continue
        if BLOCK_START.match(line) and not cur["lines"]:
            continue
        cur["lines"].append(line)
    return pre, blocks


def _service_items(block: dict) -> list[dict]:
    """Each offer ends at its own "Author response:" line. Text after the last one is not an offer."""
    items, cur = [], []
    for ln in block["lines"]:
        if PLACEHOLDER.match(ln):
            if cur:
                items.append(cur)
            cur = []
            continue
        cur.append(re.sub(r"^\s*[\u2022\-\u2013]\s*", "", ln).strip())
    return [{"id": f"E.S{n}", "who": "Editor", "group": "Optional: post-acceptance services", "where_in_paper": "",
             "comment": "\n".join(it).strip(), "response": "", "where_changed": "", "optional": True}
            for n, it in enumerate(items, 1) if "\n".join(it).strip()]


def _comments_in_block(block: dict) -> list[dict]:
    if block.get("services"):
        return _service_items(block)
    lines = [ln for ln in block["lines"] if not PLACEHOLDER.match(ln)]
    has_num = any(NUM.match(ln) or LETTER.match(ln) for ln in lines)
    has_bullet = any(BULLET.match(ln) for ln in lines)
    out: list[dict] = []
    general: list[str] = []
    group = ""                      # "General", "Specific", ...
    num = ""                        # current number, for lettered sub-items
    heading = ""                    # "Results 3.1" or "2.9": where the comment points
    listed = False
    cur = None

    def start(cid: str, text: str, where: str = "") -> dict:
        c = {"id": cid, "group": group, "where_in_paper": where, "lines": [text]}
        out.append(c)
        return c

    for i, ln in enumerate(lines):
        lab = LABEL.match(ln)
        if lab:
            group = (lab.group(1) or lab.group(2)).strip().capitalize()
            listed = bool(lab.group(2))      # "Figures and legends:": each paragraph below is its own point
            cur = None
            continue
        if not has_num and not has_bullet and listed and group != "General":
            cur = start(str(len([c for c in out if c["id"] != "G"]) + 1), ln.strip(), heading)
            continue
        s = SECTION_LABEL.match(ln)
        if s and not has_num:
            heading, cur = s.group(1), None
            continue
        if has_num:
            m = NUM.match(ln)
            if m:
                num, text = m.group(1), m.group(2).strip()
                nxt = next((x for x in lines[i + 1:] if x.strip()), "")
                if len(text) < 45 and not re.search(r"[.?!:]$", text) and LETTER.match(nxt):
                    heading, cur = text, None          # "1. Results 3.1" heads lettered items
                    continue
                heading = ""
                cur = start(num, text)
                continue
            m = LETTER.match(ln)
            if m and num:
                cur = start(f"{num}{m.group(1)}", m.group(2).strip(), heading)
                continue
        elif has_bullet:
            m = BULLET.match(ln)
            if m:
                cur = start(str(len([c for c in out if c["id"] != "G"]) + 1), m.group(1).strip(), heading)
                continue
        elif not has_num and not has_bullet and heading:
            if cur is not None and CITED_REF.match(ln):   # a paper the reviewer cites belongs to their comment
                cur["lines"].append(ln.strip())
                continue
            cur = start(str(len(out) + 1), ln.strip(), heading)
            continue
        if cur is None:
            if group == "General" or not out:
                if not has_num and not has_bullet and not group and general and len(ln.split()) >= 25 \
                        and len(general[-1].split()) >= 25:
                    # a review in plain paragraphs: the first is the summary, each later one is a point
                    cur = start(str(len(out) + 1), ln.strip(), heading)
                    continue
                general.append(ln.strip())
                continue
            cur = start(str(len(out) + 1), ln.strip(), heading)
            continue
        if not has_num and not has_bullet and not group and cur is not None and len(ln.split()) >= 25 \
                and len(" ".join(cur["lines"]).split()) >= 25:
            cur = start(str(len([c for c in out if c["id"] != "G"]) + 1), ln.strip(), heading)
            continue
        cur["lines"].append(ln.strip())

    if general:
        text = "\n".join(general)
        if not re.fullmatch(r"\(?\s*there are no comments\.?\s*\)?", text.strip(), re.I):
            out.insert(0, {"id": "G", "group": "General", "where_in_paper": "", "lines": general})
    prefix = "R" + block["who"].split()[-1] if block["who"].startswith("Reviewer") else (
        "E" + (block["who"].split()[-1] if block["who"] != "Editor" else ""))
    return [{"id": f"{prefix}.{c['id']}", "who": block["who"], "group": c["group"],
             "where_in_paper": c["where_in_paper"], "comment": "\n".join(c["lines"]).strip(),
             "response": "", "where_changed": ""} for c in out if "\n".join(c["lines"]).strip()]


def split(path: Path) -> dict:
    lines = read_lines(path)
    pre, blocks = _blocks(lines)
    comments = [c for b in blocks for c in _comments_in_block(b)]
    how = []
    for ln in pre:                       # the letter and any "Comments to Authors" notes before Reviewer 1
        for s in SENT.split(ln):
            if HOW_TO.search(s) and s.strip() not in how:
                how.append(s.strip())
    return {"letter": path.name, "how_to_answer": how,
            "reviewers": sorted({c["who"] for c in comments}, key=lambda w: (w.startswith("Editor"), w)),
            "comments": comments}


# ---------------------------------------------------------------- response table
def table(data: dict, out: Path) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, Cm

    doc = Document()
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Cm(2)
    st = doc.styles["Normal"]
    st.font.name, st.font.size = "Arial", Pt(10)
    doc.add_heading("Response to reviewers", level=1)
    doc.add_paragraph(f"Comments copied word for word from {data['letter']}. "
                      "Write your answer in the Response column and say where the manuscript changed "
                      "(page and line, or the changed sentence) in the last column.")
    if data.get("how_to_answer"):
        doc.add_heading("What the editor asks for", level=2)
        for s in data["how_to_answer"]:
            doc.add_paragraph(s, style="List Bullet")

    def shade(cell, hex_fill):
        tc = cell._tc.get_or_add_tcPr()
        sh = OxmlElement("w:shd")
        sh.set(qn("w:val"), "clear")
        sh.set(qn("w:color"), "auto")
        sh.set(qn("w:fill"), hex_fill)
        tc.append(sh)

    for who in data["reviewers"]:
        rows = [c for c in data["comments"] if c["who"] == who]
        doc.add_heading(who, level=2)
        t = doc.add_table(rows=1, cols=4)
        t.style = "Table Grid"
        widths = (Cm(1.6), Cm(7.0), Cm(6.0), Cm(3.0))
        for cell, txt, w in zip(t.rows[0].cells, ("ID", "Comment", "Response", "Where changed"), widths):
            cell.text = ""
            r = cell.paragraphs[0].add_run(txt)
            r.bold = True
            cell.width = w
            shade(cell, "D9E2F3")
        for c in rows:
            cells = t.add_row().cells
            cells[0].text = c["id"]
            text = c["comment"] if not c["where_in_paper"] else f"[{c['where_in_paper']}] {c['comment']}"
            if c.get("optional"):
                text = f"[Optional: an offer from the journal, not about the paper] {text}"
            cells[1].text = text
            cells[2].text = c.get("response", "")
            cells[3].text = c.get("where_changed", "")
            for cell, w in zip(cells, widths):
                cell.width = w
        doc.add_paragraph()
    doc.save(str(out))


# ---------------------------------------------------------------- reading the table back
WORD = re.compile(r"[a-z0-9]+")
STOPWORDS = set("the a an and or of to in on for with by is are was were be been it this that these those as at from "
                "please also which why how what can could would should their there here its into than then".split())
WHERE_GIVEN = re.compile(r"\b(?:page|line|lines|section|figure|fig|table|paragraph|location)s?\b\.?\s*[:\[]?\s*\d|location\s*:", re.I)
LABEL_ONLY = re.compile(r"^\s*(our |author'?s? )?(response|reply|answer|comment|location|changes?)\s*:?\s*$", re.I)


def _words(t: str) -> set[str]:
    return {w for w in WORD.findall(t.lower()) if w not in STOPWORDS and len(w) > 2}


def status_letter(response: Path, comments: dict) -> dict:
    """A response letter in your own layout: find each comment in it (the wording may differ a little),
    then check that an answer follows it and says where the text changed."""
    paras = [ln.strip() for ln in read_lines(response) if ln.strip()]
    sets = [_words(t) for t in paras]
    found: dict[str, int] = {}
    for c in comments["comments"]:
        cw = _words(c["comment"])
        if len(cw) < 2:
            continue
        need = 0.6 if len(cw) >= 5 else 1.0  # a short comment must be there word for word
        best, at = 0.0, -1
        for i, pw in enumerate(sets):
            if not pw:
                continue
            score = len(cw & pw) / min(len(cw), max(len(pw), 1))
            if score > best:
                best, at = score, i
        if best >= need:
            found[c["id"]] = at
    order = sorted(found.items(), key=lambda x: x[1])
    no_resp, no_where, together = [], [], []
    pending: list[str] = []          # comments quoted back to back, waiting for one shared answer
    for k, (cid, i) in enumerate(order):
        end = order[k + 1][1] if k + 1 < len(order) else len(paras)
        after = [t for t in paras[i + 1:end] if not LABEL_ONLY.match(t) and not LABEL.match(t) and not REVIEWER.match(t)]
        answer = [t for t in after if t.strip(" .")]     # "Corrected." is an answer
        if not answer:
            nxt = order[k + 1][1] if k + 1 < len(order) else None
            if nxt is not None and nxt == i + 1:
                pending.append(cid)       # the next comment follows at once: they may share one answer
                continue
            no_resp += pending + [cid]
            pending = []
            continue
        together += pending
        pending = []
        if not any(WHERE_GIVEN.search(t) for t in paras[i + 1:end]) and not cid.endswith(".G") \
                and not re.search(r"\.S\d+$", cid):
            no_where.append(cid)
    missing = [c["id"] for c in comments["comments"] if c["id"] not in found and not c.get("optional")]
    # a reviewer's opening summary is often thanked rather than quoted: list it apart
    general = [m for m in missing if m.endswith(".G")]
    missing = [m for m in missing if not m.endswith(".G")]
    no_resp += pending
    return {"format": "letter", "comments": len(comments["comments"]), "found": len(found),
            "not_in_letter": missing, "general_not_quoted": general, "no_response": no_resp, "no_where": no_where,
            "answered_together": together}


def status(response_docx: Path) -> dict:
    from docx import Document
    d = Document(str(response_docx))
    rows = []
    for t in d.tables:
        head = [c.text.strip().lower() for c in t.rows[0].cells]
        if head[:2] != ["id", "comment"]:
            continue
        for r in t.rows[1:]:
            cells = [c.text.strip() for c in r.cells]
            rows.append({"id": cells[0], "has_response": bool(cells[2]), "has_where": bool(cells[3])})
    return {"comments": len(rows),
            "no_response": [r["id"] for r in rows if not r["has_response"]],
            "no_where": [r["id"] for r in rows if r["has_response"] and not r["has_where"]
                         and not re.search(r"\.S\d+$", r["id"])]}  # an offer needs an answer, not a change


# ---------------------------------------------------------------- what changed
TERM = re.compile(r"\b(?:(?:Table|Figure|Fig\.?|Section|Supplementary (?:Table|Figure))\s*S?\d+[A-Za-z]?|"
                  r"[A-Z][A-Za-z0-9-]*[A-Z0-9][A-Za-z0-9-]*|\d+(?:\.\d+)+|PMID\s*\d+)\b")
COMMON = {"I", "A", "AI", "OK", "PDF", "DOI", "URL", "NB", "PLEASE", "NOTE", "ID"}


def _terms(text: str) -> set[str]:
    return {t.lower().replace("fig.", "figure").replace(" ", "") for t in TERM.findall(text)
            if t.upper() not in COMMON and len(t) > 1}


def changes(original: Path, revised: Path, comments: dict | None = None) -> dict:
    import lock
    old = [u.text for u in lock.extract(original)["units"]]
    new = [u.text for u in lock.extract(revised)["units"]]
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    rows = []
    for op, a1, a2, b1, b2 in sm.get_opcodes():
        if op == "equal":
            continue
        if op == "replace":
            # pair each revised paragraph with the most similar old one in the block, in order;
            # the rest were added or removed (counting by position pairs unrelated paragraphs)
            i = a1
            for j in range(b1, b2):
                best, at = 0.0, None
                for k in range(i, a2):
                    r = difflib.SequenceMatcher(None, old[k], new[j], autojunk=False).quick_ratio()
                    if r > best:
                        best, at = r, k
                if at is not None and best >= 0.5:
                    rows += [{"kind": "removed", "paragraph": None, "old": old[k], "new": ""} for k in range(i, at)]
                    rows.append({"kind": "changed", "paragraph": j + 1, "old": old[at], "new": new[j]})
                    i = at + 1
                else:
                    rows.append({"kind": "added", "paragraph": j + 1, "old": "", "new": new[j]})
            rows += [{"kind": "removed", "paragraph": None, "old": old[k], "new": ""} for k in range(i, a2)]
        elif op == "insert":
            rows += [{"kind": "added", "paragraph": j + 1, "old": "", "new": new[j]} for j in range(b1, b2)]
        elif op == "delete":
            rows += [{"kind": "removed", "paragraph": None, "old": old[i], "new": ""} for i in range(a1, a2)]
    hints = {}
    if comments:
        # Terms found in many paragraphs (the paper's own topic words) say nothing about one change.
        df: dict[str, int] = {}
        for p in new:
            for t in _terms(p):
                df[t] = df.get(t, 0) + 1
        common = {t for t, n in df.items() if n > max(3, len(new) // 15)}
        for c in [c for c in comments["comments"] if not c.get("optional")]:
            ct = _terms(c["comment"] + " " + c.get("where_in_paper", "")) - common
            if not ct:
                continue
            hit = [i for i, r in enumerate(rows) if ct & _terms(r["new"] or r["old"])]
            if hit:
                hints[c["id"]] = hit[:5]
    if revised.suffix.lower() == ".tex":   # show what the reader sees, not the markup
        import latex
        def plain(t: str) -> str:
            t = re.sub(r"(?<!\\)\$([^$]{1,80})\$", r"\1", t)          # keep short inline math as written
            return latex.detex(t.replace("``", "\u201c").replace("''", "\u201d"))
        for r in rows:
            r["old"], r["new"] = plain(r["old"]), plain(r["new"])
    return {"original": original.name, "revised": revised.name, "rows": rows, "hints": hints}


def changes_markdown(ch: dict, comments: dict | None = None) -> str:
    def short(s, n=160):
        s = s.replace("\n", " ")
        return s if len(s) <= n else s[:n] + "..."
    lines = [f"# What changed: {ch['original']} to {ch['revised']}", "",
             f"{sum(r['kind'] == 'changed' for r in ch['rows'])} paragraphs changed, "
             f"{sum(r['kind'] == 'added' for r in ch['rows'])} added, "
             f"{sum(r['kind'] == 'removed' for r in ch['rows'])} removed. "
             "Paragraph numbers count non-empty paragraphs in the revised file.", "",
             "| # | Kind | Paragraph | Text now |", "| --- | --- | --- | --- |"]
    for i, r in enumerate(ch["rows"], 1):
        lines.append(f"| {i} | {r['kind']} | {r['paragraph'] or '-'} | {short(r['new'] or r['old']).replace('|', '/')} |")
    if comments:
        lines += ["", "## Comments and changes that share key terms", "",
                  "A hint for filling the 'Where changed' column, not a decision. "
                  "A comment with no match may still be answered elsewhere, or not yet.", ""]
        for c in [c for c in comments["comments"] if not c.get("optional")]:
            hit = ch["hints"].get(c["id"])
            lines.append(f"- {c['id']}: " + (", ".join(f"change #{h + 1}" for h in hit) if hit else "no matching change found"))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Turn a decision letter into a response table")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("split")
    s.add_argument("letter", type=Path)
    s.add_argument("--out", type=Path, required=True)
    t = sub.add_parser("table")
    t.add_argument("comments", type=Path)
    t.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("changes")
    c.add_argument("original", type=Path)
    c.add_argument("revised", type=Path)
    c.add_argument("--comments", type=Path)
    c.add_argument("--out", type=Path)
    st = sub.add_parser("status")
    st.add_argument("response", type=Path)
    st.add_argument("--comments", type=Path, help="comments.json from split: needed for a response letter in your own layout")
    a = ap.parse_args(argv)

    if a.cmd == "split":
        data = split(a.letter)
        a.out.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        per = {w: sum(c["who"] == w for c in data["comments"]) for w in data["reviewers"]}
        print(f"{len(data['comments'])} comments: " + ", ".join(f"{w} {n}" for w, n in per.items()))
        return 0
    if a.cmd == "table":
        table(json.loads(a.comments.read_text(encoding="utf-8")), a.out)
        print(f"Response table: {a.out}")
        return 0
    if a.cmd == "changes":
        com = json.loads(a.comments.read_text(encoding="utf-8")) if a.comments else None
        ch = changes(a.original, a.revised, com)
        md = changes_markdown(ch, com)
        if a.out:
            a.out.write_text(md, encoding="utf-8")
            print(f"{len(ch['rows'])} changes. Report: {a.out}")
        else:
            sys.stdout.write(md)
        return 0
    if a.cmd == "status":
        r = status(a.response)
        if not r["comments"]:
            if not a.comments:
                print("This file has no SubmitReady response table. For a response letter in your own layout, "
                      "run again with --comments <comments.json> (made by split) and it is checked comment by comment.")
                return 2
            r = status_letter(a.response, json.loads(a.comments.read_text(encoding="utf-8")))
            print(f"{r['found']} of {r['comments']} comments found in your letter. "
                  f"Not in the letter: {', '.join(r['not_in_letter']) or 'none'}. "
                  f"Without a response: {', '.join(r['no_response']) or 'none'}. "
                  f"Answered but no page, line or section given: {', '.join(r['no_where']) or 'none'}."
                  + (f" General comments not quoted (usually fine if you thank the reviewer): {', '.join(r['general_not_quoted'])}."
                     if r["general_not_quoted"] else "")
                  + (f" Answered together with the comment after them: {', '.join(r['answered_together'])}."
                     if r["answered_together"] else ""))
            return 0 if not (r["not_in_letter"] or r["no_response"]) else 1
        print(f"{r['comments']} comments. Without a response: {', '.join(r['no_response']) or 'none'}. "
              f"Answered but no place given: {', '.join(r['no_where']) or 'none'}.")
        return 0 if not r["no_response"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
