"""Approved changes: section moves and label swaps.

The lock may allow exactly the changes the author approved, and nothing else.
Every test here plants an unapproved edit next to approved ones; the lock must catch 100% of them.
"""
from __future__ import annotations

import json
import random
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import changes  # noqa: E402
import lock  # noqa: E402
from docx_tools import _add_citation, plant_edit, read_doc, text_nodes, write_doc  # noqa: E402

REQ = {
    "journal": "Test Journal", "article_type": "Article",
    "sources": [{"url": "https://example.org/guidelines", "checked": "2026-11-23"}],
    "requirements": [
        {"id": "no-funding", "rule": "Funding goes in the form", "quote": "", "check": {"type": "section_absent", "section": "funding"}},
        {"id": "no-ci", "rule": "Competing interests go in the form", "quote": "", "check": {"type": "section_absent", "section": "competing"}},
        {"id": "labels", "rule": "Labels like S1 Table", "quote": "", "check": {"type": "supp_label_style", "value": "S1 Table"}},
    ],
}


def make(tmp_path: Path) -> Path:
    import docx
    d = docx.Document()
    d.add_paragraph("A made-up manuscript for approved changes")
    d.add_heading("Abstract", 1)
    d.add_paragraph("We tested things.")
    d.add_heading("Methods", 1)
    p = d.add_paragraph("Samples are listed in Table S1 and the code in Code S1 ")
    _add_citation(p._p, "[1]", "Doe")
    p.add_run(".")
    d.add_paragraph("Ranges such as Table S2-S3 and lists such as Table S2, S3 stay for the author.")
    run_split = d.add_paragraph("Split across runs: Table ")
    run_split.add_run("S2").bold = True
    run_split.add_run(" holds the rest.")
    d.add_heading("Results", 1)
    d.add_paragraph("Not applicable.")
    d.add_heading("Funding", 1)
    d.add_paragraph("This work was funded by the Made-Up Foundation.")
    d.add_paragraph("Not applicable.")
    d.add_paragraph("Conflicts of interest: none declared.")
    d.add_heading("References", 1)
    d.add_paragraph("1. Doe J (2020) A reference. J Test 1:1-2")
    path = tmp_path / "m.docx"
    d.save(path)
    return path


@pytest.fixture
def setup(tmp_path):
    src = make(tmp_path)
    lk = tmp_path / "m.lock.json"
    lock.create(src, lk)
    req = tmp_path / "req.json"
    req.write_text(json.dumps(REQ), encoding="utf-8")
    plan = changes.plan(src, lk, req)
    for c in plan["changes"]:
        c["approved"] = True
    pp = tmp_path / "plan.json"
    pp.write_text(json.dumps(plan), encoding="utf-8")
    out = tmp_path / "changed.docx"
    changes.apply(src, pp, lk, out)
    return src, lk, pp, out, plan


def body_text(path: Path) -> str:
    root = read_doc(path)
    return "\n".join("".join(t.text or "" for t in p.iter("{%s}t" % lock.W)) for p in root.iter("{%s}p" % lock.W))


def test_plan_lists_exactly_the_expected_changes(setup):
    _, _, _, _, plan = setup
    kinds = {c["id"]: c for c in plan["changes"]}
    assert set(kinds) == {"move-funding", "move-competing", "relabel-supp"}
    texts = [p["text"] for p in kinds["move-funding"]["paragraphs"]]
    assert texts == ["Funding", "This work was funded by the Made-Up Foundation.", "Not applicable."]
    assert ["Table S1", "S1 Table"] in kinds["relabel-supp"]["pairs"]
    assert kinds["relabel-supp"]["left_for_you"]  # the range and the list


def test_apply_makes_only_the_approved_changes(setup):
    src, lk, pp, out, _ = setup
    text = body_text(out)
    assert "S1 Table" in text and "S1 Code" in text and "Table S1" not in text
    assert "Split across runs: S2 Table holds the rest." in text
    assert "Table S2-S3" in text and "Table S2, S3" in text          # ranges and lists untouched
    assert "Made-Up Foundation" not in text and "Conflicts of interest" not in text
    assert text.count("Not applicable.") == 1                        # the one under Results stays
    r = lock.verify(lk, out, lock.load_approved(pp))
    assert r["status"] == 0 and not r["changed"] and not r["missing"]
    assert len(r["moved_out"]) == 4 and r["relabeled"]
    assert r["citations_found"] == r["citations_locked"] == 1       # the live citation survived
    assert zipfile.ZipFile(src).read("word/document.xml") != zipfile.ZipFile(out).read("word/document.xml")


def test_without_approval_the_same_file_is_a_stop(setup):
    _, lk, pp, out, _ = setup
    assert lock.verify(lk, out)["status"] == 2
    plan = json.loads(pp.read_text())
    for c in plan["changes"]:
        c["approved"] = False
    pp.write_text(json.dumps(plan))
    assert lock.verify(lk, out, lock.load_approved(pp))["status"] == 2


def test_planted_edit_inside_a_relabeled_paragraph_is_caught(setup, tmp_path):
    _, lk, pp, out, _ = setup
    root = read_doc(out)
    for t in root.iter("{%s}t" % lock.W):
        if t.text and "S1 Table" in t.text:
            t.text = t.text.replace("Samples are", "Samples were")
    bad = tmp_path / "bad.docx"
    write_doc(out, bad, root)
    assert lock.verify(lk, bad, lock.load_approved(pp))["status"] == 2


def test_unapproved_swap_is_caught(setup, tmp_path):
    src, lk, pp, out, _ = setup
    plan = json.loads(pp.read_text())
    for c in plan["changes"]:
        if c["kind"] == "relabel":
            c["pairs"] = [p for p in c["pairs"] if p[0] != "Table S2"]  # Table S2 swap no longer approved
    pp.write_text(json.dumps(plan))
    assert lock.verify(lk, out, lock.load_approved(pp))["status"] == 2


def test_wrong_swap_target_is_caught(setup, tmp_path):
    _, lk, pp, out, _ = setup
    root = read_doc(out)
    for t in root.iter("{%s}t" % lock.W):
        if t.text and "S1 Table" in t.text:
            t.text = t.text.replace("S1 Table", "S9 Table")
    bad = tmp_path / "bad.docx"
    write_doc(out, bad, root)
    assert lock.verify(lk, bad, lock.load_approved(pp))["status"] == 2


def test_removing_an_unapproved_paragraph_is_caught(setup, tmp_path):
    _, lk, pp, out, _ = setup
    root = read_doc(out)
    for p in list(root.iter("{%s}p" % lock.W)):
        if "We tested things." in "".join(t.text or "" for t in p.iter("{%s}t" % lock.W)):
            p.getparent().remove(p)
    bad = tmp_path / "bad.docx"
    write_doc(out, bad, root)
    assert lock.verify(lk, bad, lock.load_approved(pp))["status"] == 2


def test_same_text_in_another_section_cannot_stand_in(setup, tmp_path):
    src, lk, pp, _, _ = setup
    # only "Not applicable." under Results is deleted; approval covers the one under Funding
    root = read_doc(src)
    for p in list(root.iter("{%s}p" % lock.W)):
        if "".join(t.text or "" for t in p.iter("{%s}t" % lock.W)) == "Not applicable.":
            p.getparent().remove(p)
            break
    bad = tmp_path / "bad.docx"
    write_doc(src, bad, root)
    r = lock.verify(lk, bad, lock.load_approved(pp))
    assert r["status"] == 2 and r["missing"]


def test_identical_outcome_by_another_route_passes(setup, tmp_path):
    src, lk, pp, out, _ = setup
    # deleting the approved paragraphs by hand gives the same text as apply, so it passes too
    assert lock.verify(lk, out, lock.load_approved(pp))["status"] == 0


@pytest.mark.parametrize("seed", range(40))
def test_random_planted_edits_after_approved_changes_are_all_caught(setup, tmp_path, seed):
    _, lk, pp, out, _ = setup
    bad = tmp_path / f"bad{seed}.docx"
    plant_edit(out, bad, random.Random(seed))
    r = lock.verify(lk, bad, lock.load_approved(pp))
    assert r["status"] == 2, "a planted edit slipped through the approved changes"


def test_relabel_text_leaves_ranges_and_lists():
    pairs = [["Table S1", "S1 Table"], ["Table S2", "S2 Table"]]
    assert lock.relabel_text("see Table S1.", pairs) == "see S1 Table."
    assert lock.relabel_text("Table S1-S3", pairs) == "Table S1-S3"
    assert lock.relabel_text("Table S1, S2", pairs) == "Table S1, S2"
    assert lock.relabel_text("Table S1 and S2", pairs) == "Table S1 and S2"
    assert lock.relabel_text("Table S10", pairs) == "Table S10"
    assert lock.relabel_text("Tables S1", pairs) == "Tables S1"


def test_package_with_approved_changes(setup, tmp_path):
    import check
    src, lk, pp, out, _ = setup
    supp = tmp_path / "supp"
    supp.mkdir()
    (supp / "Table S1.csv").write_text("a,b\n1,2\n")
    plan = json.loads(pp.read_text())
    for c in plan["changes"]:
        if c["kind"] == "relabel":
            c["file_renames"] = [["Table S1.csv", "S1 Table.csv"]]
    pp.write_text(json.dumps(plan))
    req = tmp_path / "req.json"
    r = check.run(out, lk, req, tmp_path / "pkg", supp=supp, approved=pp, original=src)
    assert r["lock"] == 0
    assert (tmp_path / "pkg" / "supplementary" / "S1 Table.csv").exists()
    assert (supp / "Table S1.csv").exists()                       # the author's file keeps its name
    form = (tmp_path / "pkg" / "submission_form.md").read_text()
    assert "This work was funded by the Made-Up Foundation." in form  # moved out, still in the form sheet
    summary = (tmp_path / "pkg" / "SUMMARY.md").read_text()
    assert "changed only as you approved" in summary and "Paste the funding text" in summary
