"""Tests for scripts/form.py: the submission form sheet copies fields word for word."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import form  # noqa: E402

FUNDING = "This work was funded by the Made-Up Foundation (grant  X-1)."  # double space must survive


def make(tmp_path):
    import docx
    d = docx.Document()
    d.add_paragraph("A made-up title for testing the form sheet")
    d.add_paragraph("Ann Author1, Ben Writer2*")
    d.add_paragraph("1 Test University, Cairo, Egypt")
    d.add_paragraph("Running title: Made-up form test")
    d.add_heading("Abstract", 1)
    d.add_paragraph("First abstract paragraph.")
    d.add_paragraph("Second abstract paragraph.")
    d.add_paragraph("Keywords: alpha; beta; gamma")
    d.add_heading("Introduction", 1)
    d.add_paragraph("Body text that must not appear in any box.")
    d.add_heading("Funding", 1)
    d.add_paragraph(FUNDING)
    d.add_paragraph("Author contributions: AA wrote it; BW checked it.")
    d.add_heading("Competing interests", 1)
    d.add_paragraph("None declared.")
    d.add_heading("References", 1)
    d.add_paragraph("1. Doe J (2020) A reference. J Test 1:1-2")
    p = tmp_path / "f.docx"
    d.save(p)
    return p


def test_fields_are_copied_word_for_word(tmp_path):
    d = form.build(make(tmp_path))
    assert d["title"] == "A made-up title for testing the form sheet"
    assert d["short_title"] == "Made-up form test"
    assert d["title_page"] == ["Ann Author1, Ben Writer2*", "1 Test University, Cairo, Egypt"]
    assert d["abstract"] == ["First abstract paragraph.", "Second abstract paragraph."]
    assert d["keywords"] == "alpha; beta; gamma"
    assert d["sections"]["funding"] == [FUNDING]
    assert d["sections"]["contributions"] == ["AA wrote it; BW checked it."]
    assert d["sections"]["competing"] == ["None declared."]


def test_markdown_marks_missing_fields_and_never_fills_them(tmp_path):
    md = form.markdown(form.build(make(tmp_path)))
    assert FUNDING in md
    assert "Body text that must not appear" not in md
    assert md.count("_Not found in the manuscript") == 4  # data, ethics, acknowledgements, AI
    assert "3 keywords" in md


def test_latex_form(tmp_path):
    d = form.build(ROOT / "tests" / "fixtures" / "latex" / "main.tex")
    assert d["title"] and d["abstract"]
    assert all("unsrtnat" not in x for x in d["sections"].get("acknowledgements", []))
