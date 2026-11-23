#!/usr/bin/env python3
"""SubmitReady submission form sheet.

Journal websites ask you to type the title, abstract, keywords, funding, competing
interests and more into a form. This script copies each of them, word for word,
out of the manuscript into one sheet you can paste from.

Nothing is written, shortened or reworded. A field that is not in the manuscript
is marked "not found", never filled in.

    python form.py manuscript.docx --out submission_form.md
    python form.py main.tex --out submission_form.md
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prepare  # noqa: E402

STATEMENTS = [
    ("funding", "Funding / financial disclosure"),
    ("competing", "Competing interests / conflicts of interest"),
    ("data", "Data availability"),
    ("ethics", "Ethics statement"),
    ("contributions", "Author contributions"),
    ("acknowledgements", "Acknowledgements"),
    ("ai", "Use of AI tools"),
]
SHORT_TITLE = re.compile(r"^(running|short) (title|head)\s*[:.]\s*(.+)$", re.I)


def _inline_section(text: str) -> tuple[str, str] | None:
    return prepare.inline_statement(text)


def build_docx(path: Path) -> dict:
    _, _, paras = prepare.read_paragraphs(path)
    facts = prepare.analyze(path)
    out: dict = {"file": path.name, "title": paras[0]["raw"] if paras else "", "short_title": "",
                 "title_page": [], "abstract": [], "keywords": "", "sections": {}, "facts": facts}
    # title page: everything between the title and the first section heading (or inline "Abstract:")
    i = 1
    while i < len(paras):
        p = paras[i]
        if (p["heading"] and p["section"]) or re.match(r"^(abstract|key\s*-?\s*words?)\s*[:.]", p["text"], re.I):
            break
        m = SHORT_TITLE.match(p["raw"])
        if m:
            out["short_title"] = m.group(3).strip()
        else:
            out["title_page"].append(p["raw"])
        i += 1
    current = None
    for p in paras[i:]:
        if p["table"] or p["bib"]:
            continue
        inline = re.match(r"^(abstract|key\s*-?\s*words?)\s*[:.]\s*(.+)$", p["raw"], re.I)
        if inline:
            if inline.group(1).lower().startswith("abs"):
                out["abstract"].append(inline.group(2).strip())
                current = "abstract"
            else:
                out["keywords"] = inline.group(2).strip()
                current = None
            continue
        sec_inline = _inline_section(p["raw"])
        if sec_inline and not p["heading"]:
            out["sections"].setdefault(sec_inline[0], []).append(sec_inline[1].strip())
            current = sec_inline[0]
            continue
        if p["heading"]:
            current = p["section"]
            continue
        if prepare.CAPTION_RE.match(p["text"]):
            current = None
            continue
        if current == "abstract":
            out["abstract"].append(p["raw"])
        elif current == "keywords" and not out["keywords"]:
            out["keywords"] = p["raw"]
        elif current in dict(STATEMENTS) or current in ("declarations",):
            out["sections"].setdefault(current, []).append(p["raw"])
    return out


def build_tex(path: Path) -> dict:
    import latex
    full = latex.read_project(path)
    pre, body = latex.split_document(full)
    facts = prepare.analyze(path)
    title = latex.command_arg(full, "title") or ""
    out: dict = {"file": path.name, "title": latex.detex(title).strip(), "short_title": "",
                 "title_page": [], "abstract": [], "keywords": "", "sections": {}, "facts": facts}
    st = latex.command_arg(full, "shorttitle") or latex.command_arg(full, "runningtitle") or ""
    out["short_title"] = latex.detex(st).strip()
    for cmd in ("author", "affil", "affiliation", "address", "email"):
        for m in re.finditer(rf"\\{cmd}\s*(\[[^\]]*\])?\s*\{{", full):
            arg, _ = latex.balanced_arg(full, m.end() - 1)
            t = latex.detex(arg).strip()
            if t:
                out["title_page"].append(t)
    ab = latex.abstract_text(body)
    if ab:
        out["abstract"] = [latex.detex(x).strip() for x in re.split(r"\n\s*\n", ab) if x.strip()]
    kw = latex.command_arg(full, "keywords")
    if kw is None:
        m = re.search(r"Keywords?\s*[:.]?\}?\s*[:.]?\s*(.+)", latex.detex(body))
        kw = m.group(1) if m else ""
    out["keywords"] = latex.detex(kw).strip()
    heads = list(re.finditer(r"\\(?:sub)*section\*?\s*\{([^{}]*)\}", body))
    for n, h in enumerate(heads):
        sec = prepare._section_of(latex.detex(h.group(1)))
        if sec in dict(STATEMENTS):
            end = heads[n + 1].start() if n + 1 < len(heads) else len(body)
            chunk = re.split(r"\\bibliography\s*\{|\\printbibliography|\\begin\{thebibliography\}|\\end\{document\}", body[h.end():end])[0]
            chunk = re.sub(r"\\(bibliographystyle|label|clearpage|newpage|vspace\*?|noindent)\b(\s*\{[^{}]*\})?", "", chunk)
            parts = [latex.detex(x).strip() for x in re.split(r"\n\s*\n", chunk) if latex.detex(x).strip()]
            if parts:
                out["sections"].setdefault(sec, []).extend(parts)
    return out


def build(path: Path) -> dict:
    return build_tex(path) if path.suffix.lower() == ".tex" else build_docx(path)


def _box(text: str) -> list[str]:
    fence = "````" if "```" in text else "```"
    return [fence + "text", text, fence]


def _count(text: str) -> str:
    return f"{len(text):,} characters, {prepare.words(text):,} words"


def markdown(d: dict) -> str:
    f = d["facts"]
    nf = "_Not found in the manuscript. The journal's form may still ask for it._"
    L = [f"# Submission form sheet: {d['file']}", "",
         "Copied word for word from your manuscript, ready to paste into the journal's submission website. "
         "SubmitReady did not write or change any of it. Check each box before you paste it.", ""]

    def field(name, text, show_count=True):
        L.append(f"## {name}")
        L.append("")
        if not text:
            L.append(nf)
        else:
            if show_count:
                L.append(_count(text))
                L.append("")
            L.extend(_box(text))
        L.append("")

    field("Title", d["title"])
    field("Short title / running head", d["short_title"])
    field("Authors, affiliations and correspondence (as written on the title page)", "\n".join(d["title_page"]), False)
    field("Abstract", "\n\n".join(d["abstract"]))
    kws = [k for k in f.get("keywords", [])] if not d["keywords"] else \
        [k.strip(" .") for k in re.split(r"[;,\u2022|]", d["keywords"]) if k.strip(" .")]
    L.append("## Keywords")
    L.append("")
    if kws:
        L.append(f"{len(kws)} keywords. One per line, for forms that take them one at a time:")
        L.append("")
        L.extend(_box("\n".join(kws)))
    else:
        L.append(nf)
    L.append("")
    for key, name in STATEMENTS:
        field(name, "\n\n".join(d["sections"].get(key, [])))
    L += ["## Numbers the form may ask for", "",
          "| Item | Count |", "| --- | --- |",
          f"| Title | {f.get('title_chars', 0):,} characters, {f.get('title_words', 0):,} words |",
          f"| Abstract | {f.get('abstract_words', 0):,} words |",
          f"| Main text (Introduction to end of Discussion/Conclusion) | {f.get('main_text_words', 0):,} words |",
          f"| Figures | {len(f.get('figures', []))} |",
          f"| Tables | {len(f.get('tables', []))} |",
          f"| References | {f.get('references', 0)} |",
          f"| Supplementary items mentioned | {len(f.get('supplementary_mentioned', []))} |", ""]
    return "\n".join(L)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="SubmitReady submission form sheet")
    ap.add_argument("manuscript", type=Path)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    md = markdown(build(args.manuscript))
    if args.out:
        args.out.write_text(md, encoding="utf-8")
        print(f"Saved {args.out}")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
