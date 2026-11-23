"""LaTeX support shared by lock.py, prepare.py, refs.py and check.py.

A LaTeX manuscript is a main .tex file, the files it \\input or \\includes,
and its .bib files. Everything here is plain text processing; nothing calls
a TeX engine.

What the text lock treats as CONTENT (locked):
  the title, the abstract, every paragraph of the body, section headings,
  captions, table cells, math, footnotes.
What it treats as LAYOUT (free to change):
  the preamble, comments, whitespace (TeX ignores runs of spaces), \\label,
  spacing and page-break commands, font-size and bold/italic wrappers,
  figure placement options, environment markers, table rules, the heading
  LEVEL (\\section vs \\subsection), citation commands, the bibliography.
"""
from __future__ import annotations

import re
from pathlib import Path

CITE_RE = re.compile(r"\\(?:[a-zA-Z]*cite[a-zA-Z]*|footcite|textcite|parencite|autocite)\*?"
                     r"(?:\s*\[[^\]]*\]){0,2}\s*\{([^}]*)\}")
SECTION_CMD = re.compile(r"\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?\s*(?:\[[^\]]*\])?\s*\{")
LAYOUT_CMDS = [
    r"\\label\s*\{[^}]*\}", r"\\[vh]space\*?\s*\{[^}]*\}", r"\\(?:newpage|clearpage|cleardoublepage|pagebreak|"
    r"linebreak|nolinebreak|newline|centering|raggedright|raggedleft|noindent|indent|medskip|bigskip|smallskip|"
    r"tiny|scriptsize|footnotesize|small|normalsize|large|Large|LARGE|huge|Huge|linenumbers|nolinenumbers|"
    r"doublespacing|singlespacing|onehalfspacing|maketitle|FloatBarrier|par|hline|toprule|midrule|bottomrule|"
    r"centerline|protect)(?![a-zA-Z])", r"\\cline\s*\{[^}]*\}", r"\\cmidrule(?:\([^)]*\))?\s*\{[^}]*\}",
    r"\\setstretch\s*\{[^}]*\}", r"\\linespread\s*\{[^}]*\}", r"\\thispagestyle\s*\{[^}]*\}",
    r"\\pagestyle\s*\{[^}]*\}", r"\\addcontentsline\s*\{[^}]*\}\s*\{[^}]*\}\s*\{[^}]*\}",
]
WRAPPERS = ["textbf", "textit", "emph", "textrm", "textsf", "texttt", "textsl", "textsc", "underline", "textup",
            "textmd", "mbox"]
BIB_RE = [re.compile(r"\\bibliographystyle\s*\{[^}]*\}"), re.compile(r"\\bibliography\s*\{[^}]*\}"),
          re.compile(r"\\printbibliography(?:\[[^\]]*\])?"), re.compile(r"\\addbibresource\s*(?:\[[^\]]*\])?\{[^}]*\}"),
          re.compile(r"\\begin\{thebibliography\}.*?\\end\{thebibliography\}", re.S)]


def strip_comments(text: str) -> str:
    out = []
    for line in text.splitlines():
        m = re.search(r"(?<!\\)%", line)
        out.append(line[:m.start()] if m else line)
    return "\n".join(out)


def read_project(tex: Path, _seen: set | None = None) -> str:
    """Main file with \\input / \\include expanded and comments removed."""
    _seen = _seen or set()
    tex = tex.resolve()
    if tex in _seen:
        return ""
    _seen.add(tex)
    text = strip_comments(tex.read_text(encoding="utf-8", errors="replace"))

    def expand(m):
        name = m.group(2).strip()
        p = (tex.parent / name)
        if not p.suffix:
            p = p.with_suffix(".tex")
        return read_project(p, _seen) if p.exists() else m.group(0)

    return re.sub(r"\\(input|include)\s*\{([^}]*)\}", expand, text)


def split_document(full: str) -> tuple[str, str]:
    m = re.search(r"\\begin\{document\}", full)
    e = re.search(r"\\end\{document\}", full)
    if not m:
        return "", full
    return full[:m.start()], full[m.end():e.start() if e else len(full)]


def balanced_arg(text: str, start: int) -> tuple[str, int]:
    """text[start] == '{' -> (inside, index after the closing brace)."""
    depth, i = 0, start
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
        i += 1
    return text[start + 1:], len(text)


def command_arg(text: str, name: str) -> str | None:
    m = re.search(rf"\\{name}\*?\s*(?:\[[^\]]*\])?\s*\{{", text)
    if not m:
        return None
    return balanced_arg(text, m.end() - 1)[0]


def unwrap(text: str, names: list[str]) -> str:
    """\\textbf{x} -> x for the given commands, nested braces respected."""
    pat = re.compile(r"\\(" + "|".join(names) + r")\s*\{")
    while True:
        m = pat.search(text)
        if not m:
            return text
        inner, end = balanced_arg(text, m.end() - 1)
        text = text[:m.start()] + inner + text[end:]


CITE_TOKEN = "\u27e6CITE\u27e7"


def lock_units(tex: Path) -> tuple[list[str], list[str]]:
    """Paragraph units for the text lock, and the citation commands found (allowed to change)."""
    full = read_project(tex)
    preamble, body = split_document(full)
    units_text = []
    title = command_arg(preamble, "title") or command_arg(body, "title")
    if title:
        units_text.append(title)
    for rx in BIB_RE:
        body = rx.sub("\n\n", body)
    body = re.sub(r"\\title\s*\{", r"\\TITLEPLACEHOLDER{", body)
    if "\\TITLEPLACEHOLDER{" in body:
        i = body.index("\\TITLEPLACEHOLDER{")
        _, end = balanced_arg(body, i + len("\\TITLEPLACEHOLDER"))
        body = body[:i] + body[end:]
    cites = [m.group(0) for m in CITE_RE.finditer(body)]
    body = CITE_RE.sub(CITE_TOKEN, body)
    # headings and captions become units of their own (their LEVEL or position may change)
    body = _replace_cmd_with_unit(body, "caption")
    body = _replace_sections_with_units(body)
    body = re.sub(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", r"[graphic \1]", body)
    for pat in LAYOUT_CMDS:
        body = re.sub(pat, " ", body)
    body = re.sub(r"\\(begin|end)\s*\{[^}]*\}(\s*\[[^\]]*\])?(\s*\{[^}]*\})?", "\n\n", body)
    body = unwrap(body, WRAPPERS)
    for block in re.split(r"\n\s*\n", body):
        t = re.sub(r"\s+", " ", block).strip()
        if t and t not in ("\\\\",):
            units_text.append(t)
    return units_text, cites


def _replace_cmd_with_unit(body: str, name: str) -> str:
    pat = re.compile(rf"\\{name}\*?\s*(?:\[[^\]]*\])?\s*\{{")
    out, pos = [], 0
    for m in pat.finditer(body):
        if m.start() < pos:
            continue
        inner, end = balanced_arg(body, m.end() - 1)
        out.append(body[pos:m.start()])
        out.append(f"\n\n{inner}\n\n")
        pos = end
    out.append(body[pos:])
    return "".join(out)


def _replace_sections_with_units(body: str) -> str:
    out, pos = [], 0
    for m in SECTION_CMD.finditer(body):
        if m.start() < pos:
            continue
        inner, end = balanced_arg(body, m.end() - 1)
        out.append(body[pos:m.start()])
        out.append(f"\n\n{inner}\n\n")
        pos = end
    out.append(body[pos:])
    return "".join(out)


# ---------------------------------------------------------------- plain text for measuring
def detex(s: str) -> str:
    s = CITE_RE.sub("[1]", s)
    s = re.sub(r"\$\$.*?\$\$|\\\[.*?\\\]", " EQUATION ", s, flags=re.S)
    s = re.sub(r"(?<!\\)\$[^$]*\$", " x ", s)
    s = unwrap(s, WRAPPERS + ["textsuperscript", "textsubscript"])
    s = re.sub(r"\\(?:ref|eqref|autoref|cref|Cref|pageref)\s*\{[^}]*\}", "1", s)
    s = re.sub(r"\\[a-zA-Z@]+\*?(\s*\[[^\]]*\])?", " ", s)
    s = s.replace("{", " ").replace("}", " ").replace("~", " ").replace("\\\\", " ")
    return re.sub(r"\s+", " ", s).strip()


def bib_files(tex: Path) -> list[Path]:
    full = read_project(tex)
    names = []
    for m in re.finditer(r"\\bibliography\s*\{([^}]*)\}", full):
        names += [n.strip() for n in m.group(1).split(",")]
    for m in re.finditer(r"\\addbibresource\s*(?:\[[^\]]*\])?\{([^}]*)\}", full):
        names.append(m.group(1).strip())
    out = []
    for n in names:
        p = tex.parent / n
        if not p.suffix:
            p = p.with_suffix(".bib")
        if p.exists():
            out.append(p)
    return out


def cited_keys(tex: Path) -> list[str]:
    _, body = split_document(read_project(tex))
    keys = []
    for m in CITE_RE.finditer(body):
        for k in m.group(1).split(","):
            k = k.strip()
            if k and k not in keys:
                keys.append(k)
    return keys


def citation_style(tex: Path) -> str:
    pre, body = split_document(read_project(tex))
    allx = pre + body
    m = re.search(r"\\usepackage\s*\[([^\]]*)\]\s*\{biblatex\}", allx)
    if m:
        opts = m.group(1)
        if re.search(r"style\s*=\s*(authoryear|apa|chicago|harvard)", opts):
            return "author-date"
        if re.search(r"style\s*=\s*(numeric|ieee|nature|vancouver|science)", opts):
            return "numeric, square brackets"
    nat = re.search(r"\\usepackage\s*(\[([^\]]*)\])?\s*\{natbib\}", allx)
    bst = re.search(r"\\bibliographystyle\s*\{([^}]*)\}", allx)
    style = bst.group(1) if bst else ""
    if nat:
        opts = nat.group(2) or ""
        if "numbers" in opts:
            return "numeric, parentheses" if "round" in opts else "numeric, square brackets"
        return "author-date"
    if re.search(r"^(plain|unsrt|abbrv|ieeetr|alpha|vancouver|elsarticle-num|naturemag|acm)$", style):
        return "numeric, square brackets"
    if re.search(r"(apa|harv|chicago|authordate|agu)", style):
        return "author-date"
    return "unknown"
