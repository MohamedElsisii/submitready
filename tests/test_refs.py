"""Tests for scripts/refs.py using made-up references and simulated Crossref answers."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import refs  # noqa: E402
from docx_tools import make_sample  # noqa: E402

RIS = """TY  - JOUR
AU  - Doe, Jane
AU  - Roe, Richard
TI  - A study of things*
T2  - Journal of Things
PY  - 2021
VL  - Volume 12 - 2021
IS  - 3
SP  - e100
EP  - e100
DO  - https://doi.org/10.1234/ABC.5678
ER  -
TY  - GEN
AU  - W, Roberts Andrew
AU  - S, Davids Matthew
TI  - Targeting things with drugs
T2  - New Journal
PY  - 2016
VL  - 374
IS  - 4
SP  - 311
EP  - 322
DO  - 10.5555/syn.nejm.0001
ER  -
TY  - JOUR
AU  - Doe, Jane
TI  - A Study of Things
T2  - Journal of Things
PY  - 2021
VL  - 12
DO  - 10.1234/abc.5678
ER  -
TY  - JOUR
AU  - Smith, Ann
TI  - Uncited but present
T2  - Other Journal
PY  - 2020
VL  - 1
ER  -
"""


def ris(tmp_path, text=RIS):
    p = tmp_path / "lib.ris"
    p.write_text(text, encoding="utf-8")
    return p


def codes(findings):
    return {f.code for f in findings}


def test_read_ris(tmp_path):
    r = refs.read_ris(ris(tmp_path))
    assert len(r) == 4
    assert r[0].data["page"] == "e100"
    assert r[1].data["type"] == "generic"


def test_audit_catches_common_problems(tmp_path):
    ms = refs.load(ris(tmp_path))
    f = refs.audit(ms)
    c = codes(f)
    assert {"doi-as-url", "odd-volume", "title-trailing-mark", "type-generic", "author-swapped", "duplicate"} <= c
    swapped = next(x for x in f if x.code == "author-swapped")
    assert swapped.suggested.startswith("Roberts, Andrew W")
    url = next(x for x in f if x.code == "doi-as-url")
    assert url.suggested == "10.1234/ABC.5678"  # letter case kept


def test_docx_citations_are_read(tmp_path):
    ms = refs.read_docx(make_sample(tmp_path / "s.docx"))
    assert ms.citations == 1 and len(ms.refs) == 1
    assert ms.refs[0].data["author"][0]["family"] == "Obeagu"


def test_uncited_library_entries(tmp_path):
    ms = refs.ManuscriptRefs(refs=refs.read_ris(ris(tmp_path))[:1])
    lib = refs.read_ris(ris(tmp_path))
    f = refs.audit(ms, lib)
    uncited = [x for x in f if x.code == "uncited"]
    assert any("Uncited but present" in x.ref for x in uncited)


def fake_crossref(records):
    def fetch(url):
        for key, value in records.items():
            if key in url:
                return value
        return None
    return fetch


def test_verify_flags_mismatch_retraction_and_preprint(tmp_path):
    ms = refs.ManuscriptRefs(refs=[
        refs.Ref("a", {"type": "article-journal", "title": "A study of things", "DOI": "10.1234/abc.5678",
                       "issued": {"date-parts": [[2021]]}, "volume": "12", "page": "e100",
                       "author": [{"family": "Doe", "given": "Jane"}]}, "x"),
        refs.Ref("b", {"type": "article-journal", "title": "Retracted work", "DOI": "10.9999/ret",
                       "issued": {"date-parts": [[2019]]}, "author": [{"family": "Bad"}]}, "x"),
        refs.Ref("c", {"type": "article-journal", "title": "A preprint", "DOI": "10.1101/2020.01.01.1",
                       "issued": {"date-parts": [[2020]]}, "author": [{"family": "Pre"}]}, "x"),
        refs.Ref("d", {"type": "article-journal", "title": "Ghost paper", "DOI": "10.5555/none",
                       "author": [{"family": "Nobody"}]}, "x"),
    ])
    fetch = fake_crossref({
        "10.1234/abc.5678": {"message": {"title": ["A study of things"], "issued": {"date-parts": [[2022]]},
                                         "volume": "12", "page": "e100", "author": [{"family": "Doe"}],
                                         "container-title": ["Journal of Things"]}},
        "10.9999/ret": {"message": {"title": ["Retracted work"], "issued": {"date-parts": [[2019]]},
                                 "author": [{"family": "Bad"}],
                                 "updated-by": [{"type": "retraction", "DOI": "10.9999/notice"}]}},
        "10.1101/2020.01.01.1": {"message": {"title": ["A preprint"], "issued": {"date-parts": [[2020]]},
                                             "author": [{"family": "Pre"}], "type": "posted-content",
                                             "relation": {"is-preprint-of": [{"id": "10.1/published"}]}}},
    })
    f, stats = refs.verify(ms, fetch, pause=0)
    c = codes(f)
    assert "mismatch-year" in c
    assert "retracted" in c
    assert "preprint-published" in c
    assert "doi-not-found" in c
    assert stats["matched"] == 3


def test_verify_finds_missing_doi_by_title():
    ms = refs.ManuscriptRefs(refs=[refs.Ref("q", {"type": "article-journal",
                                                  "title": "The role of XYZ-9 test proteins in a made-up pathway",
                                                  "issued": {"date-parts": [[2022]]},
                                                  "author": [{"family": "Quinn"}]}, "x")])
    fetch = fake_crossref({"query.bibliographic": {"message": {"items": [
        {"title": ["The role of XYZ-9 test proteins in a made-up pathway"], "DOI": "10.5555/syn.2022.985",
         "issued": {"date-parts": [[2022]]}, "author": [{"family": "Quinn"}]}]}}})
    f, _ = refs.verify(ms, fetch, pause=0)
    found = next(x for x in f if x.code == "doi-found")
    assert found.suggested == "10.5555/syn.2022.985"


def test_report_says_nothing_was_changed(tmp_path):
    ms = refs.load(ris(tmp_path))
    md = refs.report_markdown("lib.ris", ms, refs.audit(ms))
    assert "Nothing was changed" in md and "| Reference |" in md


def test_online_print_year_and_consortium_are_not_false_alarms():
    ms = refs.ManuscriptRefs(refs=[
        refs.Ref("s", {"type": "article-journal", "title": "QUICK: a fast made-up sequence aligner",
                       "DOI": "10.5555/syn.quick.635", "issued": {"date-parts": [[2013]]},
                       "author": [{"family": "Quill"}]}, "x"),
        refs.Ref("c", {"type": "article-journal", "title": "A made-up architecture of 2,000 synthetic samples",
                       "DOI": "10.5555/syn.group.983", "issued": {"date-parts": [[2012]]},
                       "author": [{"family": "Moss"}]}, "x"),
        refs.Ref("f", {"type": "article-journal",
                       "title": "MadeUpTool: an open source test library [version 1; peer review: 2 approved]",
                       "DOI": "10.5555/syn.tool.7931.1", "issued": {"date-parts": [[2016]]},
                       "author": [{"family": "Mills"}]}, "x"),
    ])
    fetch = fake_crossref({
        "syn.quick.635": {"message": {"title": ["QUICK: a fast made-up sequence aligner"],
                               "issued": {"date-parts": [[2012, 10, 25]]},
                               "published-print": {"date-parts": [[2013, 1]]}, "author": [{"family": "Quill"}]}},
        "syn.group.983": {"message": {"title": ["A made-up architecture of 2,000 synthetic samples"],
                                    "issued": {"date-parts": [[2012]]},
                                    "author": [{"name": "SYNTH Consortium"}, {"family": "Moss"}]}},
        "7931.1": {"message": {"title": ["MadeUpTool: an open source test library"], "issued": {"date-parts": [[2016]]},
                               "author": [{"family": "Mills"}]}},
    })
    f, _ = refs.verify(ms, fetch, pause=0)
    assert not [x for x in f if x.code.startswith("mismatch")], [(x.code, x.ref) for x in f]


def test_junk_doi_suffix_is_repaired():
    ms = refs.ManuscriptRefs(refs=[refs.Ref("a", {"type": "article-journal", "title": "A study of things",
                                                  "DOI": "10.5555/syn.2017.01206/bibtex",
                                                  "author": [{"family": "Doe"}]}, "x")])
    fetch = fake_crossref({"works/10.5555/syn.2017.01206/bibtex": None,
                           "works/10.5555/syn.2017.01206": {"message": {"title": ["A study of things"],
                                                                        "author": [{"family": "Doe"}]}}})
    f, stats = refs.verify(ms, fetch, pause=0)
    junk = next(x for x in f if x.code == "doi-junk-suffix")
    assert junk.suggested == "10.5555/syn.2017.01206"
    assert stats["matched"] == 1


# ---- plain typed reference lists (no reference manager) ----

def test_parse_plain_author_year():
    d = refs.parse_plain("Roca Pereira JF, Gale FX, Rios TP (2019) Improved salt tolerance in a "
                         "test plant. Test Rep 9:8113. https://doi.org/10.5555/syn-2019-44133")
    assert d["author"][0]["family"] == "Roca Pereira"
    assert d["_year_label"] == "2019"
    assert d["DOI"] == "10.5555/syn-2019-44133"
    assert d["title"].startswith("Improved salt tolerance")


def test_parse_plain_numbered_with_particle_and_letter():
    d = refs.parse_plain("[3] van Dijk MC, Bold JF (2011a) Searching for test genes. Test Physiol 156:1-10")
    assert d["author"][0]["family"] == "van Dijk"
    assert d["_year_label"] == "2011a"


def test_author_year_citations_forms():
    text = ("Drought hurts yield (Smith 2020; Smith and Jones 2019a, b; Lee et al., 2018, 2021). "
            "Roca Pereira et al. (2019) showed this. See Table 1 (2020 data).")
    got = set(refs.author_year_citations(text))
    assert ("Smith", "2020") in got
    assert ("Smith", "2019a") in got and ("Smith", "2019b") in got
    assert ("Lee", "2018") in got and ("Lee", "2021") in got
    assert ("Roca Pereira", "2019") in got
    assert not any(f == "Table" for f, _ in got)


def _plain_docx(tmp_path, body, references):
    import docx as pydocx
    d = pydocx.Document()
    for b in body:
        d.add_paragraph(b)
    d.add_paragraph("References")
    for r in references:
        d.add_paragraph(r)
    p = tmp_path / "plain.docx"
    d.save(p)
    return p


def test_plain_docx_cited_and_listed(tmp_path):
    p = _plain_docx(tmp_path,
                    ["Salt stress is common (Smith et al. 2020; Fu et al. 2021).", "Jones (2019) disagreed."],
                    ["Jones A (2019) A title. J Plant 1:1-2",
                     "Smith B, Lee C (2020) Another title. J Plant 2:3-4",
                     "Zhou D (2018) Never cited. J Plant 3:5-6"])
    ms = refs.load(p)
    assert ms.plain and len(ms.refs) == 3
    f = refs.match_plain_citations(ms)
    codes = {(x.code, x.ref) for x in f}
    assert ("cited-not-listed", "Fu 2021") in codes
    assert any(c == "listed-not-cited" and "Zhou" in w for c, w in codes)
    assert not any("Smith" in w or "Jones" in w for _, w in codes)


def test_plain_docx_numbered(tmp_path):
    p = _plain_docx(tmp_path, ["As shown [1, 3-4] and [6]."],
                    ["1. A A (2019) T. J 1", "2. B B (2019) T. J 1", "3. C C (2019) T. J 1", "4. D D (2019) T. J 1"])
    f = refs.match_plain_citations(refs.load(p))
    assert any(x.code == "cited-not-listed" and x.ref == "[6]" for x in f)
    assert any(x.code == "listed-not-cited" and "2" in x.message for x in f)


def test_dedupe_one_problem_one_line():
    F = refs.Finding
    f = [F("Poe 2022", "odd-issue", "warning", "Issue has extra text", "9 September", "9"),
         F("Poe 2022", "mismatch-issue", "warning", "Issue differs from Crossref", "9 September", "9"),
         F("Lee 2021", "mismatch-first-page", "warning", "First page differs from Crossref", "1", "e623")]
    out = refs.dedupe(f)
    assert [x.code for x in out] == ["odd-issue", "mismatch-first-page"]


def test_dedupe_merges_missing_field_with_crossref_fill():
    """Offline "Missing issue" and online "Missing issue; Crossref has it" are one problem."""
    F = refs.Finding
    f = [F("Doe 2020", "missing-page", "info", "Missing pages or article number"),
         F("Doe 2020", "missing-issue", "info", "Missing issue"),
         F("Doe 2020", "missing-volume", "warning", "Missing volume"),
         F("Doe 2020", "fill-first-page", "info", "Missing first page; Crossref has it", "", "e100"),
         F("Doe 2020", "fill-issue", "info", "Missing issue; Crossref has it", "", "3"),
         F("Doe 2020", "fill-volume", "info", "Missing volume; Crossref has it", "", "12"),
         F("Roe 2019", "missing-issue", "info", "Missing issue")]
    out = refs.dedupe(f)
    assert [(x.ref, x.code) for x in out] == [("Doe 2020", "fill-first-page"), ("Doe 2020", "fill-issue"),
                                              ("Doe 2020", "fill-volume"), ("Roe 2019", "missing-issue")]
    # the merged line keeps the stronger severity
    assert next(x for x in out if x.code == "fill-volume").severity == "warning"
