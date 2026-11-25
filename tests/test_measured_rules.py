"""Rules that used to be "check yourself" and are now measured (from the Breast Cancer Research and
Treatment test run, 2026-11-25): structured abstract, heading levels, caption style, font, DOI links,
supplementary names and formats, and one fix listed once."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import docx
from docx.shared import Pt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import changes  # noqa: E402
import check  # noqa: E402
import lock  # noqa: E402
import prepare  # noqa: E402


def build(path, paras, headings=(), bold=(), font=None):
    """paras: text list. headings: texts given Word heading styles, as (text, level). bold: texts set in bold."""
    d = docx.Document()
    if font:
        d.styles["Normal"].font.name, d.styles["Normal"].font.size = font[0], Pt(font[1])
    levels = dict(headings)
    for t in paras:
        if t in levels:
            d.add_heading(t, levels[t])
            continue
        p = d.add_paragraph()
        for part, b in bold_parts(t, bold):
            p.add_run(part).bold = b
    d.save(path)
    return path


def bold_parts(t, bold):
    for b in bold:
        if t.startswith(b):
            return [(b, True), (t[len(b):], False)]
    return [(t, t in bold)]


def req(*checks):
    return {"journal": "T", "sources": [{"url": "https://example.org", "checked": "2026-11-25"}],
            "requirements": [{"id": c.pop("id", f"r{i}"), "rule": c["type"], "quote": "", "check": c}
                             for i, c in enumerate(checks)]}


def result(path, check_):
    return prepare.check(prepare.analyze(path), req(dict(check_)))[0]


ABSTRACT_RULE = {"type": "abstract_structure", "headings": ["Purpose", "Methods", "Results", "Conclusion"]}


def test_structured_abstract_inline_labels(tmp_path):
    good = build(tmp_path / "a.docx", ["Title", "Abstract", "Purpose: To test. Methods: We tested. Results: It worked. "
                                       "Conclusions: Good.", "Introduction", "Text."], headings=[("Abstract", 1), ("Introduction", 1)])
    assert result(good, ABSTRACT_RULE)["status"] == "PASS"     # "Conclusions" counts as "Conclusion"
    bad = build(tmp_path / "b.docx", ["Title", "Abstract", "Background: Why. Methods: How. Results: What. Conclusion: So.",
                                      "Introduction", "Text."], headings=[("Abstract", 1), ("Introduction", 1)])
    r = result(bad, ABSTRACT_RULE)
    assert r["status"] == "FIX" and "missing: Purpose" in r["found"] and "Background" in r["found"]
    plain = build(tmp_path / "c.docx", ["Title", "Abstract", "We tested and it worked.", "Introduction", "Text."],
                  headings=[("Abstract", 1), ("Introduction", 1)])
    assert "unstructured" in result(plain, ABSTRACT_RULE)["found"]


def test_structured_abstract_with_bold_label_paragraphs_keeps_the_real_sections(tmp_path):
    paras = ["Title", "Abstract", "Purpose", "To test.", "Methods", "We tested.", "Results", "It worked.",
             "Conclusion", "Good.", "Introduction", "Body text cites work [1].", "Methods", "Long methods text [2].",
             "Results", "Findings [3].", "References", "1. A ref."]
    p = build(tmp_path / "s.docx", paras, headings=[("Abstract", 1), ("Introduction", 1), ("References", 1)],
              bold=("Purpose", "Methods", "Results", "Conclusion"))
    f = prepare.analyze(p)
    assert f["abstract_labels"] == ["Purpose", "Methods", "Results", "Conclusion"]
    assert f["abstract_words"] == 7
    assert ["abstract", "introduction", "methods", "results", "references"] == \
        [s for s in f["sections_found"] if s in ("abstract", "introduction", "methods", "results", "references")]


def test_bold_methods_after_an_ordinary_abstract_is_still_the_methods_section(tmp_path):
    p = build(tmp_path / "m.docx", ["Title", "Abstract", "An ordinary abstract with no labels.", "Methods",
                                    "We trained models.", "Results", "It worked."], headings=[("Abstract", 1)],
              bold=("Methods", "Results"))
    assert {"methods", "results"} <= set(prepare.analyze(p)["sections_found"])


def test_heading_levels(tmp_path):
    p = build(tmp_path / "h.docx", ["Title", "Introduction", "Sub", "Subsub", "Deep", "Text."],
              headings=[("Introduction", 1), ("Sub", 2), ("Subsub", 3), ("Deep", 4)])
    r = result(p, {"type": "max_heading_levels", "value": 3})
    assert r["status"] == "FIX" and r["found"].startswith("4 heading levels")
    q = build(tmp_path / "q.docx", ["Title", "Introduction", "Text."], bold=("Introduction",))
    assert result(q, {"type": "max_heading_levels", "value": 3})["status"] == "CHECK"   # bold text: cannot count


CAPTION_RULE = {"type": "caption_style", "label": "Fig.", "bold": True, "no_punct_after_number": True, "no_end_punct": True}


def test_caption_style(tmp_path):
    good = build(tmp_path / "g.docx", ["Title", "Results", "As in Fig. 1.", "Fig. 1 Survival by subtype"],
                 bold=("Fig. 1",))
    assert result(good, CAPTION_RULE)["status"] == "PASS"
    bad = build(tmp_path / "b.docx", ["Title", "Results", "As in Figure 1.", "Figure 1. Survival by subtype."])
    r = result(bad, CAPTION_RULE)
    assert r["status"] == "FIX"
    for part in ('start with "Figure"', "label not bold", "punctuation after the number", "ending with punctuation"):
        assert part in r["found"]


def test_font_is_reported_but_an_example_font_is_not_a_rule(tmp_path):
    p = build(tmp_path / "f.docx", ["Title", "Body text here."], font=("Calibri", 12))
    r = result(p, {"type": "font", "names": ["Times New Roman", "Times"], "size": 10})
    assert r["status"] == "CHECK" and "Calibri, 12 pt" in r["found"] and "example" in r["found"]
    strict = result(p, {"type": "font", "names": ["Times New Roman"], "size": 12, "strict": True})
    assert strict["status"] == "FIX"
    ok = build(tmp_path / "t.docx", ["Title", "Body text here."], font=("Times New Roman", 10))
    assert result(ok, {"type": "font", "names": ["Times New Roman"], "size": 10})["status"] == "PASS"


def test_doi_links_in_the_reference_list(tmp_path):
    def refs(lines):
        return build(tmp_path / f"r{len(lines)}{lines[0][:3]}.docx", ["Title", "Text [1].", "References", *lines],
                     headings=[("References", 1)])
    rule = {"type": "doi_format", "value": "link"}
    assert result(refs(["1. A (2020) J 1:2. https://doi.org/10.1000/abc"]), rule)["status"] == "PASS"
    r = result(refs(["1. A (2020) J 1:2. doi:10.1000/abc", "2. B (2021) J 3:4. https://doi.org/10.1000/xyz"]), rule)
    assert r["status"] == "STYLE" and r["found"].startswith("1 DOIs not written as links")
    assert result(refs(["1. A (2020) J 1:2."]), rule)["status"] == "CHECK"


def test_missing_section_and_its_place_are_one_fix(tmp_path):
    p = build(tmp_path / "d.docx", ["Title", "Abstract", "Text.", "Introduction", "Text.", "References", "1. A ref."],
              headings=[("Abstract", 1), ("Introduction", 1), ("References", 1)])
    rq = req({"id": "decl", "type": "section_present", "section": "declarations"},
             {"id": "order", "type": "sections_order", "order": ["abstract", "references", "declarations"]})
    res = prepare.check(prepare.analyze(p), rq)
    decl, order = res
    assert decl["status"] == "FIX" and decl["location"].endswith("add it after References")
    assert order["covered_by"] == "decl"


def test_supplementary_names_and_formats(tmp_path):
    files = []
    for n in ("Table S1.csv", "Table S2.tsv", "Code S1.txt", "Notes S1.docx", "ESM_5.pdf"):
        f = tmp_path / n
        f.write_text("x")
        files.append(f)
    rule = {"type": "supp_files", "name_pattern": r"^ESM_\d+$", "name_example": "ESM_1.pdf",
            "formats": {"spreadsheet": ["csv", "xlsx"], "text": ["pdf"]}}
    probs, notes = check.supp_file_problems(files, rule)
    assert probs[0].startswith("4 of 5 supplementary files are not named as the journal asks (for example ESM_1.pdf)")
    assert "Table S2.tsv: spreadsheet files must be .csv or .xlsx" in probs
    assert "Notes S1.docx: text files must be .pdf" in probs
    assert not any(p.startswith("Code S1") for p in probs)              # code has no format rule: never flagged
    assert notes and "1 .txt" in notes[0]
    assert check.supp_file_problems(files, None) == ([], [])


def test_online_resource_relabel_renames_files_and_passes_the_lock(tmp_path):
    ms = build(tmp_path / "m.docx", ["Title", "Abstract", "Text.", "Introduction",
                                     "Code is in Code S1 and genes in Supplementary Table S2. See Table S1.",
                                     "Results", "Values in Table S2.", "References", "1. A ref."],
               headings=[("Abstract", 1), ("Introduction", 1), ("References", 1)])
    lk = tmp_path / "m.lock.json"
    lock.create(ms, lk)
    supp = tmp_path / "supp"
    supp.mkdir()
    for n in ("Table S1.csv", "Table S2.csv", "Code S1.txt"):
        (supp / n).write_text("x")
    rq = tmp_path / "req.json"
    rq.write_text(json.dumps(req({"id": "label", "type": "supp_label_style", "value": "Online Resource 1"})))
    pl = changes.plan(ms, lk, rq, supp)
    c = pl["changes"][0]
    assert c["pairs"][0] == ["Code S1", "Online Resource 1"]                       # order of first mention
    assert ["Supplementary Table S2", "Online Resource 2"] in c["pairs"]
    assert sorted(c["file_renames"]) == [["Code S1.txt", "ESM_1.txt"], ["Table S1.csv", "ESM_3.csv"],
                                         ["Table S2.csv", "ESM_2.csv"]]
    c["approved"] = True
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(pl))
    out = tmp_path / "out.docx"
    changes.apply(ms, plan_path, lk, out)
    v = lock.verify(lk, out, lock.load_approved(plan_path))
    assert v["status"] == 0 and len(v["relabeled"]) == 2
    text = " ".join(p.text for p in docx.Document(out).paragraphs)
    assert "Code is in Online Resource 1 and genes in Online Resource 2. See Online Resource 3." in text
    # a planted edit next to the relabel is still caught
    d = docx.Document(out)
    for p in d.paragraphs:
        if p.text.startswith("Values in"):
            p.runs[0].text = p.runs[0].text.replace("Values", "Value")
    d.save(tmp_path / "edited.docx")
    assert lock.verify(lk, tmp_path / "edited.docx", lock.load_approved(plan_path))["status"] == 2


def test_package_summary_counts_and_supplementary_line(tmp_path):
    ms = build(tmp_path / "m.docx", ["Title", "Abstract", "Text.", "Introduction", "See Online Resource 1.",
                                     "References", "1. A ref."],
               headings=[("Abstract", 1), ("Introduction", 1), ("References", 1)])
    lk = tmp_path / "m.lock.json"
    lock.create(ms, lk)
    supp = tmp_path / "supp"
    supp.mkdir()
    (supp / "ESM_1.pdf").write_text("x")
    (supp / "ESM_2.xlsx").write_text("x")
    rq = tmp_path / "req.json"
    rq.write_text(json.dumps(req(
        {"id": "decl", "type": "section_present", "section": "declarations"},
        {"id": "order", "type": "sections_order", "order": ["abstract", "references", "declarations"]},
        {"id": "esm", "type": "supp_files", "name_pattern": r"^ESM_\d+$",
         "formats": {"spreadsheet": ["csv", "xlsx"], "text": ["pdf"]}},
        {"type": "manual"})))
    r = check.run(ms, lk, rq, tmp_path / "pkg", supp=supp)
    summary = (tmp_path / "pkg" / "SUMMARY.md").read_text(encoding="utf-8")
    assert "- Journal rules: 0 pass, 1 to fix, 1 to check yourself" in summary
    assert "- Supplementary files: 2 files, names and formats match the journal's rules; all cited items are present" in summary
    assert len([b for b in r["blocking"] if "declarations" in b.lower() or "section" in b.lower()]) == 1
    xr = (tmp_path / "pkg" / "reports" / "crossrefs.md").read_text(encoding="utf-8")
    assert "ESM_2.xlsx: Online Resource 2 is never cited" in xr
