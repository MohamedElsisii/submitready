"""Revision helper: letter to comments, response table, status, what changed. Synthetic letters only."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from docx import Document

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import revision as R  # noqa: E402

LETTER_A = """12-Jan-2026
Dear Dr Test,
The referees would like to see some revisions made to your manuscript.
PLEASE NOTE: When you revise your manuscript please highlight the changes you make in the manuscript by using the track changes mode in MS Word.
Sincerely,
An Editor
Comments to Authors
Please use either tracked changes within the revised manuscript files or specify the page and line number of the changes made.
Reviewer 1
General Comments:
This is a useful study.
Author response:
Specific Comments:
1. Abstract: Please list all thirty genes.
Author response and action taken:
Page and line number(s) for changes made:
2. Methods: Justify the R2 threshold of 0.6.
Author response and action taken:
Page and line number(s) for changes made:
Reviewer 2
General Comments:
The authors explore methylation signatures.
More detail is needed throughout.
Author response:
Specific Comments:
1. Results 3.1
A. Report AUC values for the classifier.
Author response and action taken:
B. The figure legends lack detail.
Supplementary tables were not provided.
Author response and action taken:
2. Validate on METABRIC.
Author response and action taken:
Editor Comments to Author:
1. Under a heading titled 'Funding', please list the funders.
-       "This manuscript was funded by X."
-       If the manuscript was not funded, please state this.
Author response:
2. Please include a 'Writing assistance disclosure' section.
Author response:
This journal offers a selection of post-acceptance services for authors.
Open access publication: have you considered it?
Author response:
"""

LETTER_B = """Dear Dr Test,
Please provide a reply to the referee comments; summarizing the changes you have made within the body of the manuscript.
Referee(s)' Comments to Author:
Referee: 1
Comments to the Author
Title of the paper.
The authors dock RNA to proteins.
2.9
Full length models for BAX are available in the PDB.
In Figure 1, some labels are blocked.
3.4
Table 6 shows NA for BCL-xL, why?
Saxena, N., Katiyar, S. P., Liu, Y. (2013). Molecular interactions of Bcl-2. PLoS One.
Referee: 2
Comments to the Author
This manuscript provides good insights, however:
-How did the docking account for charge?
-Were mutagenesis data used?
Editor Comments to Author:
Associate Editor: 1
Comments to Author:
(There are no comments.)
"""


def _split(tmp, text):
    f = tmp / "letter.txt"
    f.write_text(text, encoding="utf-8")
    return R.split(f)


def test_numbered_letter(tmp_path):
    d = _split(tmp_path, LETTER_A)
    ids = [c["id"] for c in d["comments"]]
    assert ids == ["R1.G", "R1.1", "R1.2", "R2.G", "R2.1A", "R2.1B", "R2.2", "E.1", "E.2", "E.S1"]
    by = {c["id"]: c for c in d["comments"]}
    assert by["R1.1"]["comment"] == "Abstract: Please list all thirty genes."      # word for word
    assert by["R2.1A"]["where_in_paper"] == "Results 3.1"
    assert by["R2.1B"]["comment"] == "The figure legends lack detail.\nSupplementary tables were not provided."
    assert by["R2.G"]["comment"].count("\n") == 1
    assert "If the manuscript was not funded" in by["E.1"]["comment"]           # sub-bullets stay with item 1
    assert not any("Author response" in c["comment"] for c in d["comments"])
    assert not any("post-acceptance" in c["comment"] for c in d["comments"])
    assert by["E.S1"]["comment"] == "Open access publication: have you considered it?" and by["E.S1"]["optional"]
    assert not any(c.get("optional") for c in d["comments"] if c["id"] != "E.S1")
    assert any("track changes" in h for h in d["how_to_answer"])
    assert any("page and line number" in h for h in d["how_to_answer"])


def test_section_label_and_bullet_letter(tmp_path):
    d = _split(tmp_path, LETTER_B)
    by = {c["id"]: c for c in d["comments"]}
    assert [c["id"] for c in d["comments"]] == ["R1.G", "R1.1", "R1.2", "R1.3", "R2.G", "R2.1", "R2.2"]
    assert by["R1.1"]["where_in_paper"] == "2.9" and by["R1.3"]["where_in_paper"] == "3.4"
    assert "Saxena" in by["R1.3"]["comment"]                 # the cited paper stays with its comment
    assert by["R2.1"]["comment"] == "How did the docking account for charge?"
    assert "Editor 1" not in d["reviewers"]                  # "(There are no comments.)"
    assert any("reply to the referee" in h for h in d["how_to_answer"])


def test_table_and_status_round_trip(tmp_path):
    d = _split(tmp_path, LETTER_A)
    out = tmp_path / "response.docx"
    R.table(d, out)
    doc = Document(str(out))
    tables = [t for t in doc.tables if t.rows[0].cells[0].text == "ID"]
    assert len(tables) == 3
    assert tables[0].rows[2].cells[1].text == "Abstract: Please list all thirty genes."
    assert all(r.cells[2].text == "" for t in tables for r in t.rows[1:])   # never writes a response
    st = R.status(out)
    assert st["comments"] == 10 and len(st["no_response"]) == 10
    assert "Optional" in tables[2].rows[-1].cells[1].text
    tables[0].rows[2].cells[2].text = "We now list all thirty genes."
    tables[0].rows[3].cells[2].text = "Done."
    tables[0].rows[3].cells[3].text = "Page 4, lines 80-85"
    doc.save(str(out))
    st = R.status(out)
    assert "R1.1" not in st["no_response"] and st["no_where"] == ["R1.1"]


def _docx(path, paras):
    doc = Document()
    for p in paras:
        doc.add_paragraph(p)
    doc.save(str(path))
    return path


def test_changes_and_hints(tmp_path):
    a = _docx(tmp_path / "a.docx", ["Title", "We trained a model.", "Table 2 lists the genes.", "Old sentence."])
    b = _docx(tmp_path / "b.docx", ["Title", "We trained a model.", "Table 2 lists all genes with names.",
                                     "We validated it on METABRIC."])
    d = _split(tmp_path, LETTER_A)
    ch = R.changes(a, b, d)
    kinds = [r["kind"] for r in ch["rows"]]
    assert kinds.count("changed") == 2
    assert ch["hints"].get("R2.2") == [1]                     # "Validate on METABRIC." -> the new sentence
    md = R.changes_markdown(ch, d)
    assert "METABRIC" in md and "R2.2: change #2" in md


def test_journal_offers_need_an_answer_but_no_change(tmp_path):
    d = _split(tmp_path, LETTER_A)
    out = tmp_path / "response.docx"
    R.table(d, out)
    doc = Document(str(out))
    row = [r for t in doc.tables for r in t.rows if r.cells[0].text == "E.S1"][0]
    row.cells[2].text = "Not at this time."
    doc.save(str(out))
    st = R.status(out)
    assert "E.S1" not in st["no_response"] and "E.S1" not in st["no_where"]


def test_offers_without_answer_lines_are_left_out(tmp_path):
    text = LETTER_A.replace("Open access publication: have you considered it?\nAuthor response:\n",
                            "Open access publication: have you considered it?\n")
    d = _split(tmp_path, text)
    assert not any(c.get("optional") for c in d["comments"])


def test_status_reads_a_response_letter_in_your_own_layout(tmp_path):
    d = _split(tmp_path, LETTER_A)
    cj = tmp_path / "comments.json"
    cj.write_text(json.dumps(d), encoding="utf-8")
    letter = _docx(tmp_path / "my_letter.docx", [
        "Response to reviewers", "Reviewer 1", "We thank the reviewer.",
        "Comment 1", "Abstract: please list all the thirty genes.", "Our response:",
        "We now list all thirty genes in the abstract.", "Location: Page 2, lines 20-25",
        "Comment 2", "Methods: justify the R2 threshold of 0.6.", "Our response:",
        "We explain the threshold choice in more detail now.",
        "Reviewer 2", "Report AUC values for the classifier.", "Our response:", "Location:",
    ])
    assert R.status(letter)["comments"] == 0                         # not a SubmitReady table
    r = R.status_letter(letter, d)
    assert "R1.1" not in r["not_in_letter"] and "R1.2" not in r["not_in_letter"]   # small wording changes are fine
    assert "R2.2" in r["not_in_letter"]                               # METABRIC was never answered
    assert r["no_where"] == ["R1.2"] and r["no_response"] == ["R2.1A"]
    assert "R1.G" in r["general_not_quoted"] and "R1.G" not in r["not_in_letter"]
    assert R.main(["status", str(letter)]) == 2                       # asks for --comments instead of saying "none"
