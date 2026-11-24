"""Tests for scripts/lock.py on a synthetic manuscript (safe to commit)."""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import lock  # noqa: E402
from docx_tools import (  # noqa: E402
    plant_edit, q, read_doc, text_nodes, write_doc, make_sample,
)


@pytest.fixture
def sample(tmp_path):
    src = make_sample(tmp_path / "sample.docx")
    lk = tmp_path / "sample.lock.json"
    lock.create(src, lk)
    return src, lk, tmp_path


def edit_text(src, dst, old, new):
    root = read_doc(src)
    for t in root.iter(q("w:t"), q("m:t")):
        if t.text and old in t.text:
            t.text = t.text.replace(old, new, 1)
            break
    else:
        raise AssertionError(f"{old!r} not found")
    write_doc(src, dst, root)
    return dst


def test_identical_file_passes(sample):
    src, lk, _ = sample
    r = lock.verify(lk, src)
    assert r["status"] == 0
    assert r["sentences_unchanged"] == r["sentences_locked"]


def test_resaved_by_python_docx_passes(sample):
    import docx

    src, lk, tmp = sample
    out = tmp / "resaved.docx"
    docx.Document(src).save(out)
    assert lock.verify(lk, out)["status"] == 0


def test_single_character_change_is_caught(sample):
    src, lk, tmp = sample
    out = edit_text(src, tmp / "x.docx", "60%", "70%")
    r = lock.verify(lk, out)
    assert r["status"] == 2
    assert "[-" in r["changed"][0]["diff"] and "{+" in r["changed"][0]["diff"]


def test_table_cell_change_is_caught(sample):
    src, lk, tmp = sample
    out = edit_text(src, tmp / "x.docx", "1.84", "1.85")
    assert lock.verify(lk, out)["status"] == 2


def test_gene_name_mangled_is_caught(sample):
    src, lk, tmp = sample
    out = edit_text(src, tmp / "x.docx", "SEPTIN2", "2-Sep")
    assert lock.verify(lk, out)["status"] == 2


def test_equation_change_is_caught(sample):
    src, lk, tmp = sample
    out = edit_text(src, tmp / "x.docx", "SSres", "SSreg")
    assert lock.verify(lk, out)["status"] == 2


def test_whitespace_change_is_caught(sample):
    src, lk, tmp = sample
    out = edit_text(src, tmp / "x.docx", "33 models", "33  models")
    r = lock.verify(lk, out)
    assert r["status"] == 2
    assert r["changed"][0]["kind"] == "whitespace only"


def test_deleted_paragraph_is_caught(sample):
    src, lk, tmp = sample
    root = read_doc(src)
    for p in root.iter(q("w:p")):
        if "Fourteen genes" in "".join(t.text or "" for t in p.iter(q("w:t"))):
            p.getparent().remove(p)
            break
    out = tmp / "x.docx"
    write_doc(src, out, root)
    r = lock.verify(lk, out)
    assert r["status"] == 2 and len(r["missing"]) == 1


def test_added_paragraph_needs_review(sample):
    import docx

    src, lk, tmp = sample
    d = docx.Document(src)
    d.add_paragraph("Declarations")
    out = tmp / "x.docx"
    d.save(out)
    r = lock.verify(lk, out)
    assert r["status"] == 1 and r["added"][0]["text"] == "Declarations"


def test_moved_paragraph_passes_and_is_reported(sample):
    src, lk, tmp = sample
    root = read_doc(src)
    body = root.find(q("w:body"))
    paras = [p for p in body if p.tag == q("w:p")]
    methods = next(p for p in paras if "".join(t.text or "" for t in p.iter(q("w:t"))) == "Methods")
    body.remove(methods)
    body.insert(0, methods)
    out = tmp / "x.docx"
    write_doc(src, out, root)
    r = lock.verify(lk, out)
    assert r["status"] == 0 and r["moved_paragraphs"] >= 1


def test_citation_marker_restyle_is_allowed(sample):
    src, lk, tmp = sample
    out = edit_text(src, tmp / "x.docx", "[1]", "(Obeagu, 2024)")
    r = lock.verify(lk, out)
    assert r["status"] == 0 and r["citation_markers_changed"] == 1


def test_bibliography_restyle_is_left_to_refs(sample):
    src, lk, tmp = sample
    out = edit_text(src, tmp / "x.docx", "Obeagu EI.", "Obeagu, E. I.")
    assert lock.verify(lk, out)["status"] == 0


def test_tracked_changes_are_refused(tmp_path):
    src = make_sample(tmp_path / "s.docx")
    root = read_doc(src)
    p = next(root.iter(q("w:p")))
    ins = p.makeelement(q("w:ins"), {q("w:id"): "1", q("w:author"): "x"})
    p.append(ins)
    out = tmp_path / "tracked.docx"
    write_doc(src, out, root)
    with pytest.raises(lock.Refused):
        lock.create(out, tmp_path / "t.lock.json")


@pytest.mark.parametrize("seed", range(50))
def test_random_planted_edit_is_always_caught(sample, seed):
    src, lk, tmp = sample
    out = tmp / f"planted_{seed}.docx"
    plant_edit(src, out, random.Random(seed))
    assert lock.verify(lk, out)["status"] == 2


def test_verify_accepts_swapped_arguments(tmp_path):
    src = tmp_path / "a.docx"
    make_sample(src)
    lk = tmp_path / "a.lock.json"
    lock.create(src, lk)
    assert lock.main(["verify", str(src), str(lk)]) == 0


# ---- citations converted to plain text (Zotero Unlink Citations, EndNote Convert to Plain Text) ----

from docx_tools import make_zotero_sample, unlink_citations  # noqa: E402


@pytest.mark.parametrize("maker", ["mendeley", "fields"])
def test_unlinked_citations_pass(tmp_path, maker):
    src = make_sample(tmp_path / "m.docx") if maker == "mendeley" else make_zotero_sample(tmp_path / "z.docx")
    lk = tmp_path / "m.lock.json"
    lock.create(src, lk)
    out = tmp_path / "unlinked.docx"
    unlink_citations(src, out)
    r = lock.verify(lk, out)
    assert r["status"] == 0, (r["changed"], r["missing"])
    assert r["citations_unlinked"] >= 1


@pytest.mark.parametrize("seed", range(30))
def test_planted_edits_after_unlinking_are_caught(tmp_path, seed):
    src = make_zotero_sample(tmp_path / "z.docx") if seed % 2 else make_sample(tmp_path / "m.docx")
    lk = tmp_path / "m.lock.json"
    lock.create(src, lk)
    out = tmp_path / "unlinked.docx"
    unlink_citations(src, out)
    bad = tmp_path / "bad.docx"
    plant_edit(out, bad, random.Random(seed))
    assert lock.verify(lk, bad)["status"] == 2


def test_unlinked_citation_with_different_text_is_caught(tmp_path):
    src = make_zotero_sample(tmp_path / "z.docx")
    lk = tmp_path / "m.lock.json"
    lock.create(src, lk)
    out = tmp_path / "unlinked.docx"
    unlink_citations(src, out)
    root = read_doc(out)
    for t in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"):
        if t.text == "[1]":
            t.text = "[3]"
    bad = tmp_path / "bad.docx"
    write_doc(out, bad, root)
    assert lock.verify(lk, bad)["status"] == 2
