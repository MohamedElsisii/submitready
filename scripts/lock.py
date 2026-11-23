#!/usr/bin/env python3
"""SubmitReady text lock.

Fingerprints every paragraph of a Word manuscript (body, tables, captions,
text boxes, equations, footnotes, endnotes) and later proves whether any of
that text changed.

Allowed without failing the lock:
  * citation markers produced by a reference manager (Mendeley Cite, Zotero,
    Mendeley Desktop, EndNote), which change when the citation style changes;
  * the reference-manager bibliography, which /refs checks separately;
  * paragraphs that moved but whose text is identical.

Everything else (a changed, missing or added character) is reported.

Works on Word (.docx) and LaTeX (.tex, with its \\input files; see latex.py for
what counts as layout in LaTeX).

Usage:
  python scripts/lock.py create manuscript.docx --out manuscript.lock.json
  python scripts/lock.py verify manuscript.lock.json formatted.docx [--report report.md]

Exit codes for verify: 0 = text unchanged, 1 = only additions to review,
2 = text changed or missing, 3 = input refused.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
import unicodedata
import zipfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent))

VERSION = "0.2.0"

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"


def q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


CITE_TOKEN = "\u27e6CITE\u27e7"  # stands in for a live citation marker
CITATION_SDT = re.compile(r"^(MENDELEY_CITATION|CSL_CITATION|ZOTERO_ITEM)", re.I)
BIBLIOGRAPHY_SDT = re.compile(r"^(MENDELEY_BIBLIOGRAPHY|ZOTERO_BIBL|CSL_BIBLIOGRAPHY)", re.I)
CITATION_FIELD = re.compile(r"ADDIN\s+(CSL_CITATION|ZOTERO_ITEM|EN\.CITE|Mendeley Citation)", re.I)
BIBLIOGRAPHY_FIELD = re.compile(
    r"ADDIN\s+(Mendeley Bibliography|ZOTERO_BIBL|EN\.REFLIST|CSL_BIBLIOGRAPHY)", re.I
)
PARTS = [("body", "word/document.xml"), ("footnote", "word/footnotes.xml"), ("endnote", "word/endnotes.xml")]


class Refused(Exception):
    """The input cannot be locked safely; the user must act first."""


@dataclass
class Unit:
    part: str
    index: int
    text: str
    sha256: str
    sentences: int
    citations: list[str] = field(default_factory=list)


def normalize(text: str) -> str:
    """Unicode NFC only. Spacing and punctuation stay exact on purpose."""
    return unicodedata.normalize("NFC", text)


def count_sentences(text: str) -> int:
    t = text.replace(CITE_TOKEN, " ").strip()
    if not t:
        return 0
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(\[\"'\u201c])", t)
    return len([p for p in parts if p.strip()])


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sdt_tag(sdt) -> str:
    tag = sdt.find(f"{q(W, 'sdtPr')}/{q(W, 'tag')}")
    return tag.get(q(W, "val"), "") if tag is not None else ""


def _skip_subtree(el) -> bool:
    """Subtrees whose text must not be read as part of the current paragraph."""
    if el.tag == q(MC, "Fallback"):  # duplicate of mc:Choice content
        return True
    if el.tag == q(W, "txbxContent"):  # text box paragraphs are units of their own
        return True
    if el.tag in (q(W, "delText"), q(W, "del"), q(W, "moveFrom")):  # deleted text
        return True
    return False


class Extractor:
    """Walks a WordprocessingML part in document order, one unit per paragraph."""

    def __init__(self):
        self.field_stack: list[dict] = []  # complex fields can span paragraphs

    def paragraph_text(self, p) -> tuple[str, list[str]]:
        out: list[str] = []
        citations: list[str] = []
        cite_buf: dict = {}  # keyed by the sdt element itself (id() of lxml proxies is not stable)

        def citation_sdt_of(el):
            node = el.getparent()
            while node is not None and node is not p:
                if node.tag == q(W, "sdt") and CITATION_SDT.match(_sdt_tag(node)):
                    return node
                node = node.getparent()
            return None

        def walk(el):
            for child in el:
                if not isinstance(child.tag, str) or _skip_subtree(child):
                    continue
                tag = child.tag
                if tag == q(W, "fldChar"):
                    kind = child.get(q(W, "fldCharType"))
                    if kind == "begin":
                        self.field_stack.append({"instr": "", "phase": "instr", "buf": []})
                    elif kind == "separate" and self.field_stack:
                        self.field_stack[-1]["phase"] = "result"
                    elif kind == "end" and self.field_stack:
                        f = self.field_stack.pop()
                        if CITATION_FIELD.search(f["instr"]):
                            citations.append("".join(f["buf"]))
                            out.append(CITE_TOKEN)
                    continue
                if tag == q(W, "instrText"):
                    if self.field_stack and self.field_stack[-1]["phase"] == "instr":
                        self.field_stack[-1]["instr"] += child.text or ""
                    continue
                in_cite_field = any(
                    f["phase"] == "result" and CITATION_FIELD.search(f["instr"]) for f in self.field_stack
                )
                in_bib_field = any(
                    f["phase"] == "result" and BIBLIOGRAPHY_FIELD.search(f["instr"]) for f in self.field_stack
                )
                text = None
                if tag in (q(W, "t"), q(M, "t")):
                    text = child.text or ""
                elif tag == q(W, "tab"):
                    text = "\t"
                elif tag in (q(W, "br"), q(W, "cr")):
                    text = "\n"
                elif tag == q(W, "noBreakHyphen"):
                    text = "\u2011"
                elif tag == q(W, "softHyphen"):
                    text = ""
                if text is not None:
                    if in_bib_field:
                        continue
                    if in_cite_field:
                        self.field_stack[-1]["buf"].append(text)
                        continue
                    sdt = citation_sdt_of(child)
                    if sdt is not None:
                        key = sdt
                        if key not in cite_buf:
                            cite_buf[key] = []
                            citations.append("")  # placeholder, filled below
                            out.append(CITE_TOKEN)
                            cite_buf[key].append(len(citations) - 1)
                        cite_buf[key].append(text)
                        continue
                    out.append(text)
                    continue
                walk(child)

        walk(p)
        for key, buf in cite_buf.items():
            citations[buf[0]] = "".join(buf[1:])
        return "".join(out), citations

    def in_bibliography(self, p) -> bool:
        node = p.getparent()
        while node is not None:
            if node.tag == q(W, "sdt") and BIBLIOGRAPHY_SDT.match(_sdt_tag(node)):
                return True
            node = node.getparent()
        return any(BIBLIOGRAPHY_FIELD.search(f["instr"]) for f in self.field_stack)


def _paragraphs(root, part: str):
    """Yield w:p elements in document order, skipping duplicated or non-content ones."""
    for p in root.iter(q(W, "p")):
        node = p.getparent()
        skip = False
        while node is not None:
            if node.tag in (q(MC, "Fallback"), q(W, "del")):
                skip = True
                break
            if part != "body" and node.tag in (q(W, "footnote"), q(W, "endnote")):
                if node.get(q(W, "type")) in ("separator", "continuationSeparator", "continuationNotice"):
                    skip = True
                break
            node = node.getparent()
        if not skip:
            yield p


def check_refusals(z: zipfile.ZipFile) -> list[str]:
    problems = []
    xml = z.read("word/document.xml")
    n_ins = xml.count(b"<w:ins ")
    n_del = xml.count(b"<w:del ")
    if n_ins or n_del:
        problems.append(f"{n_ins} tracked insertions and {n_del} tracked deletions")
    if "word/comments.xml" in z.namelist():
        n_c = z.read("word/comments.xml").count(b"<w:comment ")
        if n_c:
            problems.append(f"{n_c} comments")
    return problems


def extract_tex(tex_path: Path) -> dict:
    import latex

    texts, cites = latex.lock_units(tex_path)
    units: list[Unit] = []
    ci = 0
    for i, t in enumerate(texts):
        t = normalize(t.replace(latex.CITE_TOKEN, CITE_TOKEN))
        n = t.count(CITE_TOKEN)
        units.append(Unit("body", i, t, sha(t), count_sentences(t), cites[ci:ci + n]))
        ci += n
    bib = sum(len(re.findall(r"^\s*@\w+\s*\{", p.read_text(encoding="utf-8", errors="replace"), re.M))
              for p in latex.bib_files(tex_path))
    return {"units": units, "bibliography_paragraphs": bib, "refusals": []}


def extract(docx_path: Path) -> dict:
    if docx_path.suffix.lower() == ".tex":
        return extract_tex(docx_path)
    units: list[Unit] = []
    bibliography_paragraphs = 0
    with zipfile.ZipFile(docx_path) as z:
        refusals = check_refusals(z)
        names = set(z.namelist())
        for part, name in PARTS:
            if name not in names:
                continue
            root = etree.fromstring(z.read(name))
            ex = Extractor()
            idx = 0
            for p in _paragraphs(root, part):
                in_bib = ex.in_bibliography(p)
                text, cites = ex.paragraph_text(p)
                if in_bib:
                    if text.strip():
                        bibliography_paragraphs += 1
                    continue
                text = normalize(text)
                if not text.strip():
                    continue
                units.append(Unit(part, idx, text, sha(text), count_sentences(text), cites))
                idx += 1
    return {"units": units, "bibliography_paragraphs": bibliography_paragraphs, "refusals": refusals}


def create(docx: Path, out: Path, allow_tracked: bool = False) -> dict:
    data = extract(docx)
    if data["refusals"] and not allow_tracked:
        raise Refused(
            "The manuscript still has " + " and ".join(data["refusals"])
            + ". Resolve them in Word (accept or reject changes, delete comments) and run again."
        )
    units = data["units"]
    lock = {
        "tool": "SubmitReady lock",
        "version": VERSION,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_file": docx.name,
        "source_sha256": hashlib.sha256(docx.read_bytes()).hexdigest(),
        "paragraphs": len(units),
        "sentences": sum(u.sentences for u in units),
        "citations": sum(len(u.citations) for u in units),
        "bibliography_paragraphs_excluded": data["bibliography_paragraphs"],
        "units": [asdict(u) for u in units],
    }
    out.write_text(json.dumps(lock, ensure_ascii=False, indent=1), encoding="utf-8")
    return lock


def _word_diff(a: str, b: str) -> str:
    aw, bw = re.findall(r"\S+|\s+", a), re.findall(r"\S+|\s+", b)
    sm = difflib.SequenceMatcher(None, aw, bw, autojunk=False)
    parts = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            seg = "".join(aw[i1:i2])
            parts.append(seg if len(seg) < 80 else seg[:35] + " ... " + seg[-35:])
        else:
            if i2 > i1:
                parts.append("[-" + "".join(aw[i1:i2]) + "-]")
            if j2 > j1:
                parts.append("{+" + "".join(bw[j1:j2]) + "+}")
    return "".join(parts).replace(CITE_TOKEN, "[cite]")


def _visible(s: str) -> str:
    return s.replace(CITE_TOKEN, "[cite]").replace("\t", "\\t").replace("\n", "\\n")


def verify(lock_path: Path, docx: Path) -> dict:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    old = lock["units"]
    new_data = extract(docx)
    new = [asdict(u) for u in new_data["units"]]

    old_counts = Counter(u["sha256"] for u in old)
    new_counts = Counter(u["sha256"] for u in new)

    # Exact matches (by content, regardless of position)
    matched_old: set[int] = set()
    matched_new: set[int] = set()
    remaining = dict(old_counts)
    pos_old = defaultdict(list)
    for i, u in enumerate(old):
        pos_old[u["sha256"]].append(i)
    for j, u in enumerate(new):
        h = u["sha256"]
        if remaining.get(h, 0) > 0:
            i = pos_old[h].pop(0)
            remaining[h] -= 1
            matched_old.add(i)
            matched_new.add(j)

    unmatched_old = [i for i in range(len(old)) if i not in matched_old]
    unmatched_new = [j for j in range(len(new)) if j not in matched_new]

    # Pair leftovers by similarity: changed paragraphs
    changed = []
    used_new: set[int] = set()
    for i in unmatched_old:
        best, best_j = 0.0, None
        for j in unmatched_new:
            if j in used_new:
                continue
            r = difflib.SequenceMatcher(None, old[i]["text"], new[j]["text"], autojunk=False).ratio()
            if r > best:
                best, best_j = r, j
        if best_j is not None and best >= 0.5:
            used_new.add(best_j)
            a, b = old[i]["text"], new[best_j]["text"]
            kind = "whitespace only" if re.sub(r"\s+", "", a) == re.sub(r"\s+", "", b) else "text"
            changed.append({"old_index": i, "new_index": best_j, "kind": kind, "diff": _word_diff(a, b)})
    changed_old = {c["old_index"] for c in changed}
    missing = [{"old_index": i, "text": _visible(old[i]["text"])} for i in unmatched_old if i not in changed_old]
    added = [{"new_index": j, "text": _visible(new[j]["text"])} for j in unmatched_new if j not in used_new]

    # Moved: matched paragraphs whose relative order changed
    seq_old_for_new = []
    tmp_pos = defaultdict(list)
    for i, u in enumerate(old):
        if i in matched_old:
            tmp_pos[u["sha256"]].append(i)
    for j in sorted(matched_new):
        seq_old_for_new.append(tmp_pos[new[j]["sha256"]].pop(0))
    lis = _longest_increasing_subsequence(seq_old_for_new)
    moved = len(seq_old_for_new) - len(lis)

    # Citation markers (allowed to change)
    old_cites = [c for u in old for c in u["citations"]]
    new_cites = [c for u in new for c in u["citations"]]
    cite_changes = sum(1 for a, b in zip(old_cites, new_cites) if a != b)

    unchanged_sentences = sum(old[i]["sentences"] for i in matched_old)
    version_note = ""
    if lock.get("version") != VERSION:
        version_note = (f"This lock was made with SubmitReady lock {lock.get('version')}, and this is {VERSION}. "
                        "If results look odd, create the lock again from the ORIGINAL manuscript.")
    status = 0
    if added:
        status = 1
    if changed or missing:
        status = 2
    return {
        "lock_file": lock_path.name,
        "source_file": lock.get("source_file"),
        "checked_file": docx.name,
        "checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "paragraphs_locked": len(old),
        "paragraphs_unchanged": len(matched_old),
        "sentences_locked": lock["sentences"],
        "sentences_unchanged": unchanged_sentences,
        "moved_paragraphs": moved,
        "changed": changed,
        "missing": missing,
        "added": added,
        "citations_locked": len(old_cites),
        "citations_found": len(new_cites),
        "citation_markers_changed": cite_changes,
        "version_note": version_note,
        "status": status,
    }


def _longest_increasing_subsequence(seq: list[int]) -> list[int]:
    import bisect

    tails: list[int] = []
    for x in seq:
        k = bisect.bisect_left(tails, x)
        if k == len(tails):
            tails.append(x)
        else:
            tails[k] = x
    return tails


def report_markdown(r: dict) -> str:
    verdict = {
        0: "PASS: no locked text changed.",
        1: "REVIEW: no locked text changed, but new text was added. Check the additions below.",
        2: "STOP: locked text changed. See the exact changes below.",
    }[r["status"]]
    lines = [
        "# SubmitReady change report",
        "",
        f"- Original: `{r['source_file']}`",
        f"- Checked: `{r['checked_file']}` on {r['checked']}",
        "",
        f"**{verdict}**",
        "",
    ]
    if r.get("version_note"):
        lines += [f"Note: {r['version_note']}", ""]
    lines += [
        f"- Sentences unchanged: **{r['sentences_unchanged']:,} of {r['sentences_locked']:,}**",
        f"- Paragraphs unchanged: {r['paragraphs_unchanged']:,} of {r['paragraphs_locked']:,}",
        f"- Paragraphs moved (text identical): {r['moved_paragraphs']}",
        f"- Citation markers: {r['citations_found']} found, {r['citations_locked']} locked, "
        f"{r['citation_markers_changed']} restyled (allowed)",
        "",
    ]
    if r["changed"]:
        lines += ["## Changed text", "", "Removed text is shown as `[-...-]`, added text as `{+...+}`.", ""]
        for c in r["changed"]:
            lines.append(f"- Paragraph {c['old_index'] + 1} ({c['kind']}): {c['diff']}")
        lines.append("")
    if r["missing"]:
        lines += ["## Missing paragraphs", ""]
        for m in r["missing"]:
            lines.append(f"- Paragraph {m['old_index'] + 1}: {m['text'][:200]}")
        lines.append("")
    if r["added"]:
        lines += ["## Added paragraphs (not in the original)", ""]
        for a in r["added"]:
            lines.append(f"- {a['text'][:200]}")
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    # Windows terminals and pipes default to a legacy code page; manuscripts contain Unicode.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="SubmitReady text lock")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="fingerprint a manuscript")
    c.add_argument("docx", type=Path)
    c.add_argument("--out", type=Path)
    c.add_argument("--allow-tracked", action="store_true", help="lock even with tracked changes or comments")
    v = sub.add_parser("verify", help="check a file against a lock")
    v.add_argument("lock", type=Path)
    v.add_argument("docx", type=Path)
    v.add_argument("--report", type=Path, help="write a Markdown report here")
    v.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "create":
            out = args.out or args.docx.with_suffix(".lock.json")
            lock = create(args.docx, out, args.allow_tracked)
            print(
                f"Locked {lock['sentences']:,} sentences in {lock['paragraphs']:,} paragraphs "
                f"({lock['citations']} live citations, {lock['bibliography_paragraphs_excluded']} "
                f"bibliography entries left to /refs). Lock saved to {out}"
            )
            return 0
        r = verify(args.lock, args.docx)
        md = report_markdown(r)
        if args.report:
            args.report.write_text(md, encoding="utf-8")
        print(json.dumps(r, ensure_ascii=False, indent=1) if args.json else md)
        return r["status"]
    except Refused as e:
        print(f"Refused: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
