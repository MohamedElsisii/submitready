"""Tests for scripts/prepare.py on the synthetic manuscript."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import lock  # noqa: E402
import prepare  # noqa: E402
from docx_tools import make_sample  # noqa: E402


def req(*checks):
    return {
        "journal": "Test Journal",
        "sources": [{"url": "https://example.org/guidelines", "checked": "2026-11-23"}],
        "requirements": [{"id": f"r{i}", "rule": c["type"], "quote": "", "check": c} for i, c in enumerate(checks)],
    }


def test_analyze_finds_sections_and_counts(tmp_path):
    f = prepare.analyze(make_sample(tmp_path / "s.docx"))
    assert f["title"].startswith("A synthetic manuscript")
    assert {"abstract", "methods", "results", "references"} <= set(f["sections_found"])
    assert f["citation_format"].startswith("numeric, square brackets")
    assert f["references"] == 1
    assert f["main_text_words"] > 0


def test_check_statuses(tmp_path):
    facts = prepare.analyze(make_sample(tmp_path / "s.docx"))
    res = prepare.check(facts, req(
        {"type": "max_chars", "target": "title", "value": 10},
        {"type": "max_chars", "target": "title", "value": 500},
        {"type": "citation_format", "value": "author-date"},
        {"type": "line_numbers", "value": True},
        {"type": "manual"},
    ))
    assert [r["status"] for r in res] == ["FIX", "PASS", "STYLE", "AUTO", "CHECK"]


def test_format_changes_layout_but_not_text(tmp_path):
    src = make_sample(tmp_path / "s.docx")
    lk = tmp_path / "s.lock.json"
    lock.create(src, lk)
    r = req({"type": "line_numbers", "value": True}, {"type": "line_spacing", "value": 2.0})
    out = tmp_path / "formatted.docx"
    done = prepare.format_docx(src, r, out)
    assert len(done) == 2
    after = prepare.analyze(out)
    assert after["line_numbers"] is True and after["line_spacing"] == 2.0
    assert lock.verify(lk, out)["status"] == 0


def test_requirement_snapshots_have_sources():
    for p in (ROOT / "journals" / "requirements").glob("*.json"):
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["sources"], p.name
        for s in data["sources"]:
            assert s["url"].startswith("https://") and s["checked"], p.name
        for r in data["requirements"]:
            assert 0 <= r.get("source", 0) < len(data["sources"]), (p.name, r["id"])


def test_caption_placement_by_kind():
    # ML-like layout: table captions in the text, figure captions at the end, table captions repeated at the end
    facts = {"captions_placement": {"table_in_text": 2, "figure_end": 3, "table_end": 2},
             "caption_duplicates": ["Table 1", "Table 2"]}
    res = prepare.check(facts, req(
        {"type": "captions_placement", "value": "in_text", "kind": "figure"},
        {"type": "captions_placement", "value": "in_text", "kind": "table"},
        {"type": "captions_placement", "value": "end"},
    ))
    fig, tab, both = res
    assert fig["status"] == "FIX" and fig["found"] == "3 figure captions at the end"
    assert tab["status"] == "FIX" and "Table 1, Table 2 appear twice" in tab["found"]
    assert both["status"] == "FIX"
    facts = {"captions_placement": {"figure_in_text": 3, "table_end": 1}, "caption_duplicates": []}
    assert prepare.check(facts, req({"type": "captions_placement", "value": "in_text", "kind": "figure"}))[0]["status"] == "PASS"


def test_duplicate_caption_message_says_which_copy_to_keep():
    facts = {"captions_placement": {"table_in_text": 2, "table_end": 2},
             "caption_duplicates": ["Table 1", "Table 2"]}
    tab = prepare.check(facts, req({"type": "captions_placement", "value": "in_text", "kind": "table"}))[0]
    assert tab["status"] == "FIX"
    assert "Table 1, Table 2 appear twice: keep the copy in the text, delete the one at the end" in tab["found"]
    end = prepare.check(facts, req({"type": "captions_placement", "value": "end", "kind": "table"}))[0]
    assert "keep the copy at the end, delete the one in the text" in end["found"]


def test_title_word_limit(tmp_path):
    facts = prepare.analyze(make_sample(tmp_path / "s.docx"))
    assert facts["title_words"] > 0
    res = prepare.check(facts, req({"type": "max_words", "target": "title", "value": 2},
                                    {"type": "max_words", "target": "title", "value": 200}))
    assert [r["status"] for r in res] == ["FIX", "PASS"]


def test_section_heading_variants():
    for h in ["Author contributions", "Authors' contributions", "Authors’ contributions", "Author's contributions",
              "Author Contribution Statement", "CRediT authorship contribution statement"]:
        assert prepare._section_of(h) == "contributions", h


def test_inline_statements_count_as_sections():
    assert prepare.inline_statement("Author contributions: AB wrote it.") == ("contributions", "AB wrote it.")
    assert prepare.inline_statement("Funding. None.") == ("funding", "None.")
    assert prepare.inline_statement("Conflicts of interest: none declared") == ("competing", "none declared")
    assert prepare.inline_statement("Funding agencies often require reports.") is None


def test_format_never_overwrites_silently(tmp_path):
    src = make_sample(tmp_path / "s.docx")
    r = tmp_path / "req.json"
    r.write_text(json.dumps(req({"type": "line_spacing", "value": 2.0})))
    out = tmp_path / "s_journal.docx"
    out.write_bytes(b"my own edits")
    prepare.main(["format", str(src), str(r), "--out", str(out)])
    kept = list(tmp_path.glob("s_journal.previous-*.docx"))
    assert len(kept) == 1 and kept[0].read_bytes() == b"my own edits"
    assert out.exists() and out.read_bytes() != b"my own edits"


def test_count_limits_and_springer_declarations():
    assert prepare._section_of("Statements and Declarations") == "declarations"
    facts = {"figures": [1, 2, 3], "tables": [1], "references": 120}
    res = prepare.check(facts, req({"type": "max_count", "target": "display_items", "value": 3},
                                    {"type": "max_count", "target": "references", "value": 100},
                                    {"type": "max_count", "target": "references", "value": 250}))
    assert [(r["status"], r["found"]) for r in res] == [("FIX", "4"), ("FIX", "120"), ("PASS", "120")]


def test_headings_that_start_with_a_section_word():
    assert prepare._section_of("Background and rationale") == "introduction"
    assert prepare._section_of("Methods: Participants and setting") == "methods"
    assert prepare._section_of("Results and Discussion") == "results"
    assert prepare._section_of("Discussion") == "discussion"
    assert prepare._section_of("Backgrounds of the patients were diverse") is None


def test_unnamed_heading_ends_the_abstract(tmp_path):
    import docx
    d = docx.Document()
    d.add_paragraph("A made-up title")
    d.add_heading("Abstract", 1)
    d.add_paragraph("Short abstract here.")
    d.add_heading("Why this study", 1)
    d.add_paragraph("Many words of main text. " * 50)
    p = tmp_path / "a.docx"
    d.save(p)
    assert prepare.analyze(p)["abstract_words"] == 3


# ---- house-style checks from real editor letters (Epigenomics, 2026) ----

def _doc(tmp_path, paragraphs, name="h.docx"):
    import docx
    d = docx.Document()
    for t in paragraphs:
        d.add_paragraph(t)
    p = tmp_path / name
    d.save(p)
    return p


HOUSE = ["A made-up title for house style",
         "Abstract",
         "Values were higher in group A (p < .05) and lower in group B (P = 0.01).",
         "Introduction",
         "Earlier work showed this [1]. Later work did not.[2] A third study agreed [3].",
         "References",
         "[1] Doe J. A made-up paper. J Tests. 2020;1:1-2.",
         "** Of considerable interest: it set up the method.",
         "[2] Roe R. Another made-up paper. J Tests. 2021;2:3-4.",
         "* Of interest: it tested the method.",
         "[3] Moe M. A third made-up paper. J Tests. 2022;3:5-6."]


def test_p_values_need_a_leading_zero(tmp_path):
    facts = prepare.analyze(_doc(tmp_path, HOUSE))
    res = prepare.check(facts, req({"type": "p_leading_zero", "value": True}))
    assert res[0]["status"] == "FIX" and "p < .05" in res[0]["found"]


def test_citation_side_of_punctuation(tmp_path):
    facts = prepare.analyze(_doc(tmp_path, HOUSE))
    before, after = prepare.check(facts, req({"type": "citation_punctuation", "value": "before"},
                                             {"type": "citation_punctuation", "value": "after"}))
    assert before["status"] == "FIX" and "did not.[2]" in before["found"]
    assert after["status"] == "FIX"


def test_reference_annotations_are_counted(tmp_path):
    facts = prepare.analyze(_doc(tmp_path, HOUSE))
    assert facts["reference_annotations"] == 2
    res = prepare.check(facts, req({"type": "min_count", "target": "reference_annotations", "value": 6}))
    assert res[0]["status"] == "FIX" and res[0]["found"] == "2"


KNOWN_CHECKS = {"max_chars", "max_words", "keywords", "section_present", "section_absent", "sections_order",
                "line_numbers", "page_numbers", "line_spacing", "footnotes", "figures_embedded", "captions_placement",
                "citation_format", "supp_label_style", "max_count", "min_count", "p_leading_zero",
                "citation_punctuation", "figure_files", "manual"}


def test_snapshot_checks_are_known_types():
    for p in (ROOT / "journals" / "requirements").glob("*.json"):
        for r in json.loads(p.read_text(encoding="utf-8"))["requirements"]:
            assert r["check"]["type"] in KNOWN_CHECKS, (p.name, r["id"], r["check"]["type"])
            sec = r["check"].get("section")
            if sec:
                assert sec in {k for k, _ in prepare.SECTIONS}, (p.name, r["id"], sec)


def test_families_point_to_real_snapshots():
    import yaml
    for p in (ROOT / "journals" / "families").glob("*.yaml"):
        fam = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert fam["guidelines"]["url"].startswith("https://") and fam["guidelines"]["checked"], p.name
        snap = fam["requirements_snapshot"].split(" ")[0]
        assert (ROOT / snap).exists(), (p.name, snap)
        if "journals" in fam:  # a named list, or a publisher match with a list page
            assert len(fam["journals"]) == len(set(fam["journals"])) > 1, p.name
        else:
            assert fam["match"]["publisher"] and fam["match"]["journal_page"].startswith("https://"), p.name
            if "journals_list" in fam:
                assert fam["journals_list"]["url"].startswith("https://"), p.name
            assert fam["journal_specific"], p.name  # what must still be read from each journal's page
        for q in fam.get("known_quirks", []):
            assert q["source"].startswith("https://") and q["checked"], p.name


def test_rules_for_things_the_paper_does_not_have_pass():
    # seen with a real preprint: no tables, no supplementary items
    facts = {"captions_placement": {"figure_end": 2}, "caption_duplicates": [], "figures": [1, 2], "tables": [],
             "sections_found": ["introduction"], "supplementary_items": []}
    res = prepare.check(facts, req({"type": "captions_placement", "value": "in_text", "kind": "table"},
                                   {"type": "section_present", "section": "supplementary"},
                                   {"type": "captions_placement", "value": "in_text", "kind": "figure"}))
    assert [r["status"] for r in res] == ["PASS", "PASS", "FIX"]


def test_line_numbers_turned_off_when_journal_says_no(tmp_path):
    import zipfile
    from docx import Document
    from lxml import etree
    src = tmp_path / "in.docx"
    d = Document()
    d.add_paragraph("Some text.")
    sect = d.sections[0]._sectPr
    ln = etree.SubElement(sect, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}lnNumType")
    ln.set("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}countBy", "1")
    d.save(str(src))
    req = {"requirements": [{"id": "ln", "check": {"type": "line_numbers", "value": False}}]}
    out = tmp_path / "out.docx"
    done = prepare.format_docx(src, req, out)
    assert "Turned off line numbers" in done
    assert b"lnNumType" not in zipfile.ZipFile(out).read("word/document.xml")
    tex = tmp_path / "main.tex"
    tex.write_text("\\documentclass{article}\n\\usepackage{lineno}\n\\begin{document}\n\\linenumbers\nHi.\n\\end{document}\n")
    tout = tmp_path / "main_fmt.tex"
    done = prepare.format_tex(tex, req, tout)
    assert any("Turned off line numbers" in x for x in done)
    assert "% \\linenumbers" in tout.read_text()


def test_live_citations_in_word_fields_are_not_called_typed(tmp_path):
    # Zotero and EndNote keep citations in Word fields, not content controls (found by tests/matrix.py)
    import docx_tools
    f = docx_tools.make_zotero_sample(tmp_path / "z.docx")
    fmt = prepare.analyze(f)["citation_format"]
    assert "typed" not in fmt and not fmt.startswith("unknown")


def _docx(path, paras, heading=None):
    """A small docx: paras is a list of text; names in `heading` get Heading 1."""
    import docx
    d = docx.Document()
    for t in paras:
        if heading and t in heading:
            d.add_heading(t, 1)
        else:
            d.add_paragraph(t)
    d.save(path)
    return path


def test_text_box_in_abstract_heading_does_not_hide_the_abstract(tmp_path):
    from lxml import etree
    import zipfile
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    abstract = "We measured things carefully and found results. " * 5
    src = _docx(tmp_path / "a.docx", ["A title", "Abstract", abstract, "Introduction", "Some text here."],
                heading={"Abstract", "Introduction"})
    with zipfile.ZipFile(src) as z:
        files = {n: z.read(n) for n in z.namelist()}
    root = etree.fromstring(files["word/document.xml"])
    head = [p for p in root.iter(f"{{{W}}}p") if "".join(p.itertext()) == "Abstract"][0]
    box = (f'<w:r xmlns:w="{W}" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
           '<mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><w:txbxContent><w:p><w:r><w:t>Note in a box.</w:t>'
           '</w:r></w:p></w:txbxContent></w:drawing></mc:Choice><mc:Fallback><w:pict><w:txbxContent><w:p><w:r>'
           '<w:t>Note in a box.</w:t></w:r></w:p></w:txbxContent></w:pict></mc:Fallback></mc:AlternateContent></w:r>')
    head.append(etree.fromstring(box))
    files["word/document.xml"] = etree.tostring(root)
    dst = tmp_path / "b.docx"
    with zipfile.ZipFile(dst, "w") as z:
        for n, b in files.items():
            z.writestr(n, b)
    f = prepare.analyze(dst)
    assert "abstract" in f["sections_found"]
    assert f["abstract_words"] == len(abstract.split()) + 4  # the box sits in the abstract, so it counts once
    _, _, paras = prepare.read_paragraphs(dst)
    assert sum(p["text"] == "Note in a box." for p in paras) == 1  # the fallback copy is not read twice


def test_keywords_line_closes_the_abstract(tmp_path):
    src = _docx(tmp_path / "k.docx", ["A title", "Abstract", "One two three four five six.",
                                      "Keywords: alpha; beta; gamma",
                                      "Impact statement. This sentence is not part of the abstract.",
                                      "Introduction", "Body text."], heading={"Abstract", "Introduction"})
    f = prepare.analyze(src)
    assert f["abstract_words"] == 6
    assert f["keywords"] == ["alpha", "beta", "gamma"]


def test_abstract_without_a_heading_after_it_stops_before_cited_text(tmp_path):
    abstract = "Plain summary words without any citation at all here. " * 20
    body = ["Courts rely on experts in many cases [1]. " * 60, "More body text follows here [2]. " * 60]
    src = _docx(tmp_path / "apa.docx", ["A title", "Abstract", abstract, *body, "References", "1. A ref."],
                heading={"Abstract", "References"})
    f = prepare.analyze(src)
    assert f["abstract_words"] == len(abstract.split())
    assert "guessed" in f["abstract_note"]
    assert f["main_text_words"] >= sum(len(b.split()) for b in body)


def test_normal_abstract_has_no_guess_note(tmp_path):
    f = prepare.analyze(make_sample(tmp_path / "s.docx"))
    assert "abstract_note" not in f
