"""LaTeX support: lock, refs, prepare and check on the synthetic LaTeX manuscript."""
from __future__ import annotations

import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check  # noqa: E402
import latex  # noqa: E402
import lock  # noqa: E402
import prepare  # noqa: E402
import refs  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "latex"
PLOS = ROOT / "journals" / "requirements" / "plos-one.2026-11-23.json"


@pytest.fixture
def proj(tmp_path):
    d = tmp_path / "paper"
    shutil.copytree(FIX, d)
    lk = tmp_path / "paper.lock.json"
    lock.create(d / "main.tex", lk)
    return d, lk


def edit(path: Path, old: str, new: str, count: int = 1):
    t = path.read_text(encoding="utf-8")
    assert old in t, old
    path.write_text(t.replace(old, new, count), encoding="utf-8")


def test_identical_passes(proj):
    d, lk = proj
    assert lock.verify(lk, d / "main.tex")["status"] == 0


@pytest.mark.parametrize("file,old,new", [
    ("main.tex", "33 models", "34 models"),
    ("main.tex", "SEPTIN2 & 1.84", "SEPTIN2 & 1.85"),
    ("main.tex", "Fourteen genes", "Fifteen genes"),
    ("main.tex", "Overlap between observed", "Overlap among observed"),
    ("main.tex", "60\\%", "70\\%"),
    ("methods.tex", "SS_{res}", "SS_{reg}"),
    ("methods.tex", "$0.05$", "$0.01$"),
    ("main.tex", "Synthetic Study", "Synthetic Studies"),
])
def test_content_changes_are_caught(proj, file, old, new):
    d, lk = proj
    edit(d / file, old, new)
    assert lock.verify(lk, d / "main.tex")["status"] == 2


@pytest.mark.parametrize("old,new", [
    ("\\section{Methods}", "\\subsection{Methods}"),                     # heading level
    ("\\begin{figure}[htbp]", "\\begin{figure}[t]"),                    # float placement
    ("Luminal tumors account", "Luminal\n   tumors   account"),           # line wrapping
    ("\\textbf{60\\%}", "60\\%"),                                       # bold removed
    ("\\section{Results}", "\\clearpage\n\\section{Results}\\label{sec:r}"),
    ("\\citep{clark2010}", "\\cite{clark2010}"),                  # citation command restyled
    ("\\bibliographystyle{unsrtnat}", "\\bibliographystyle{plainnat}"),
    ("These results agree", "% a comment line\nThese results agree"),
])
def test_layout_changes_pass(proj, old, new):
    d, lk = proj
    edit(d / "main.tex", old, new)
    r = lock.verify(lk, d / "main.tex")
    assert r["status"] == 0, r


def test_moving_figure_to_end_passes(proj):
    d, lk = proj
    t = (d / "main.tex").read_text(encoding="utf-8")
    fig = re.search(r"\\begin\{figure\}.*?\\end\{figure\}\n", t, re.S).group(0)
    t = t.replace(fig, "").replace("\\bibliographystyle", fig + "\n\\bibliographystyle")
    (d / "main.tex").write_text(t, encoding="utf-8")
    r = lock.verify(lk, d / "main.tex")
    assert r["status"] == 0 and r["moved_paragraphs"] >= 1


@pytest.mark.parametrize("seed", range(30))
def test_random_word_edit_is_caught(proj, seed):
    d, lk = proj
    rng = random.Random(seed)
    src = (d / "main.tex").read_text(encoding="utf-8")
    _, body = latex.split_document(src)
    body_start = src.index(body)
    cands = [m for m in re.finditer(r"(?<![\\\w])[A-Za-z0-9]{3,}", body)
             if not re.search(r"\\[a-zA-Z]*$", body[max(0, m.start() - 30):m.start()])
             and "{" not in body[max(0, m.start() - 1):m.start()]
             and not body[:m.start()].rstrip().endswith(("\\cite", "\\citep", "\\citet"))]
    m = rng.choice(cands)
    i = body_start + m.start() + rng.randrange(len(m.group(0)))
    ch = src[i]
    new = "0" if ch.isalpha() else "a"
    (d / "main.tex").write_text(src[:i] + new + src[i + 1:], encoding="utf-8")
    units_before = set(latex.lock_units(FIX / "main.tex")[0])
    units_after = set(latex.lock_units(d / "main.tex")[0])
    if units_before == units_after:
        pytest.skip("edit landed in a label/citation key (layout)")
    assert lock.verify(lk, d / "main.tex")["status"] == 2


def test_refs_from_tex(proj):
    d, _ = proj
    ms = refs.load(d / "main.tex")
    assert [r.key for r in ms.refs] == ["adams2012", "baker2021", "clark2010"]
    f = refs.audit(ms, ms.library)
    codes = {x.code for x in f}
    assert "uncited" in codes and "doi-as-url" in codes
    edit(d / "main.tex", "\\citep{clark2010}", "\\citep{clark2010,ghost2099}")
    ms = refs.load(d / "main.tex")
    assert "ghost2099" in ms.missing_keys
    assert "missing-key" in {x.code for x in refs.audit(ms, ms.library)}


def test_analyze_and_format_tex(proj):
    d, lk = proj
    f = prepare.analyze(d / "main.tex")
    assert f["title"].startswith("A Synthetic Study")
    assert len(f["keywords"]) == 5 and f["figures"] == [1] and f["tables"] == [1]
    assert f["citation_format"] == "numeric, square brackets"
    assert f["sections_found"][-1] == "references"
    assert f["line_numbers"] is False
    req = {"journal": "T", "sources": [{"url": "https://x.org", "checked": "2026-11-23"}],
           "requirements": [{"id": "a", "rule": "", "check": {"type": "line_numbers", "value": True}},
                            {"id": "b", "rule": "", "check": {"type": "line_spacing", "value": 2.0}}]}
    out = d / "main_fmt.tex"
    prepare.format_docx(d / "main.tex", req, out)
    g = prepare.analyze(out)
    assert g["line_numbers"] is True and g["line_spacing"] == 2.0
    assert lock.verify(lk, out)["status"] == 0


def test_check_packages_latex_source(proj, tmp_path):
    d, lk = proj
    r = check.run(d / "main.tex", lk, PLOS, tmp_path / "pkg")
    assert (tmp_path / "pkg" / "manuscript" / "main.tex").exists()
    assert (tmp_path / "pkg" / "manuscript" / "refs.bib").exists()
    assert (tmp_path / "pkg" / "figures" / "Fig1.tif").exists()
    assert r["lock"] == 0


@pytest.mark.skipif(not shutil.which("latexmk"), reason="LaTeX not installed")
def test_formatted_file_still_compiles(proj):
    d, _ = proj
    req = {"journal": "T", "sources": [{"url": "https://x.org", "checked": "2026-11-23"}],
           "requirements": [{"id": "a", "rule": "", "check": {"type": "line_numbers", "value": True}},
                            {"id": "b", "rule": "", "check": {"type": "line_spacing", "value": 2.0}}]}
    out = d / "main_fmt.tex"
    prepare.format_docx(d / "main.tex", req, out)
    p = subprocess.run(["latexmk", "-pdf", "-interaction=nonstopmode", "-quiet", out.name], cwd=d,
                       capture_output=True, timeout=180)
    assert (d / "main_fmt.pdf").exists(), p.stdout[-2000:]


# ---- real-world LaTeX layouts (found on public papers, 2026-11-28) ----

def _tex(tmp_path, preamble, body, name="ms.tex"):
    p = tmp_path / name
    p.write_text(preamble + "\n\\begin{document}\n" + body + "\n\\end{document}\n", encoding="utf-8")
    return p


def test_plos_inline_bibliography_title_and_macros(tmp_path):
    p = _tex(tmp_path, "\\documentclass{article}\n\\newcommand{\\tool}{\\href{http://tool.org}{\\texttt{ToolX}}}\n"
                       "\\bibliographystyle{/Users/someone/stylefiles/plos2015}",
             "\\begin{flushleft}{\\Large\n\\textbf\\newline{\\tool{}: fast analysis of things}\n}\\end{flushleft}\n"
             "\\section*{Abstract}Short abstract here.\n\\section*{Introduction}As shown \\cite{a1,b2}.\n"
             "\\begin{thebibliography}{10}\n\\bibitem{a1}\nJaffe ES.\n\\newblock The 2008 WHO classification of lymphomas.\n"
             "\\newblock Hematology. 2009 Jan;2009(1):523--531.\n\n\\bibitem{b2}\nSmith J, Lee K.\n\\newblock A second paper.\n"
             "\\newblock Nature. 2019;5:1--2.\n\\end{thebibliography}")
    f = prepare.analyze(p)
    assert f["title"] == "ToolX : fast analysis of things"
    assert latex.citation_style(p) == "numeric, square brackets"           # style given as a path
    ms = refs.read_tex(p)
    assert [r.key for r in ms.refs] == ["a1", "b2"] and not ms.missing_keys
    d = ms.refs[0].data
    assert d["_year_label"] == "2009" and d["title"].startswith("The 2008 WHO classification")


def test_springer_nature_template_abstract_and_style(tmp_path):
    p = _tex(tmp_path, "%%\\documentclass[sn-basic]{sn-jnl}\n\\documentclass[pdflatex,sn-mathphys-num]{sn-jnl}",
             "\\title{A title}\n\\abstract{One two three four five.}\n\\keywords{alpha, beta}\n\\maketitle\n"
             "\\section{Introduction}Text \\cite{k}.")
    assert prepare.analyze(p)["abstract_words"] == 5
    assert latex.citation_style(p) == "numeric, square brackets"


def test_elsevier_keywords_and_a_missing_bib_file(tmp_path):
    p = _tex(tmp_path, "\\documentclass{elsarticle}",
             "\\begin{frontmatter}\\title{T}\\begin{abstract}A b c.\\end{abstract}\n"
             "\\begin{keyword}microgravity \\sep boundary layer \\sep \\textit{Arabidopsis thaliana}\\end{keyword}"
             "\\end{frontmatter}\n\\section{Introduction}See \\cite{x1} and \\cite{x2,x3}.\n\\bibliography{refs}")
    assert prepare.analyze(p)["keywords"] == ["microgravity", "boundary layer", "Arabidopsis thaliana"]
    f = refs.audit(refs.read_tex(p), None)
    errs = [x for x in f if x.severity == "error"]
    assert len(errs) == 1 and errs[0].code == "bib-missing" and "refs.bib" in errs[0].message and "all 3" in errs[0].message


def test_references_typed_by_hand_in_latex(tmp_path):
    entries = "\n\n".join([
        "Agapow, P.-M., and Burt, A. (2001). Indices of multilocus linkage disequilibrium. \\emph{Mol Ecol Notes} 1, 101--102.",
        "Csardi, G., and Nepusz, T. (2006). The igraph software package for complex network research.",
        "Goss, E. M. (2014). Genome-enabled analysis. \\emph{Phytopathology} 104, 1--8."])
    p = _tex(tmp_path, "\\documentclass{article}",
             "\\section{Introduction}Clones are common (Agapow and Burt, 2001; Csardi and Nepusz, 2006). See (Goss, 2014). Again (Goss, 2014).\n"
             "\\section*{References}\n\n" + entries)
    assert latex.citation_style(p) == "author-date (typed, not live citations)"
    ms = refs.read_tex(p)
    assert ms.plain and len(ms.refs) == 3                                 # software without pages still counts
    assert ms.refs[0].data["author"][0]["family"] == "Agapow"
    assert not [x for x in refs.audit(ms, None) if x.severity == "error"]


def test_bib_markup_and_accents_become_plain_text():
    assert refs._bib_text(r"The {\textit{Arabidopsis}} response") == "The Arabidopsis response"
    assert refs._bib_text(r"Gr{\"u}nwald and {\'E}tienne, 101--102") == "Gr\u00fcnwald and \u00c9tienne, 101\u2013102"
