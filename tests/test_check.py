"""Tests for scripts/check.py: final audit and submission package."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import check  # noqa: E402
import lock  # noqa: E402
from docx_tools import make_sample  # noqa: E402


def write_req(tmp, *checks):
    p = tmp / "req.json"
    p.write_text(json.dumps({
        "journal": "Test Journal",
        "sources": [{"url": "https://example.org", "checked": "2026-11-23"}],
        "requirements": [{"id": c.get("id", f"r{i}"), "rule": c["type"], "quote": "", "check": c}
                         for i, c in enumerate(checks)],
    }), encoding="utf-8")
    return p


def setup(tmp):
    ms = make_sample(tmp / "m.docx")
    lk = tmp / "m.lock.json"
    lock.create(ms, lk)
    return ms, lk


def test_ready_when_everything_passes(tmp_path):
    ms, lk = setup(tmp_path)
    req = write_req(tmp_path, {"type": "max_chars", "target": "title", "value": 500})
    r = check.run(ms, lk, req, tmp_path / "pkg")
    assert r["verdict"] == "READY", r["blocking"]
    assert (tmp_path / "pkg" / "SUMMARY.md").exists()
    assert (tmp_path / "pkg" / "m.docx").exists()
    assert (tmp_path / "pkg" / "submission_form.md").exists()
    for name in ("change_report.md", "checklist.md", "references.md", "figures.md", "crossrefs.md"):
        assert (tmp_path / "pkg" / "reports" / name).exists()


def test_not_ready_when_text_changed(tmp_path):
    import docx

    ms, lk = setup(tmp_path)
    d = docx.Document(ms)
    d.paragraphs[4].runs[0].text = d.paragraphs[4].runs[0].text.replace("33", "34")
    changed = tmp_path / "changed.docx"
    d.save(changed)
    req = write_req(tmp_path, {"type": "manual"})
    r = check.run(changed, lk, req, tmp_path / "pkg")
    assert r["verdict"] == "NOT READY"
    assert any("Text changed" in b for b in r["blocking"])


def test_jpeg_converted_to_tiff_with_identical_pixels(tmp_path):
    ms, lk = setup(tmp_path)
    figs = tmp_path / "figs"
    figs.mkdir()
    Image.new("RGB", (1000, 800), (200, 30, 30)).save(figs / "Figure_1.png", dpi=(300, 300))
    req = write_req(tmp_path, {"id": "figure-files", "type": "figure_files", "formats": ["tif", "tiff"],
                               "dpi_min": 300, "min_width_px": 789, "name_pattern": "Fig{n}"})
    r = check.run(ms, lk, req, tmp_path / "pkg", figures=figs)
    out = tmp_path / "pkg" / "figures" / "Fig1.tif"
    assert out.exists()
    assert any("pixels identical" in d for d in r["done"])
    a, b = Image.open(figs / "Figure_1.png").convert("RGB"), Image.open(out).convert("RGB")
    assert a.tobytes() == b.tobytes()


def test_low_resolution_figure_blocks(tmp_path):
    ms, lk = setup(tmp_path)
    figs = tmp_path / "figs"
    figs.mkdir()
    Image.new("RGB", (500, 400)).save(figs / "Figure_1.tif", dpi=(300, 300))
    req = write_req(tmp_path, {"id": "figure-files", "type": "figure_files", "formats": ["tif"],
                               "dpi_min": 300, "dpi_at_final_size": True, "column_widths_mm": [85, 180]})
    r = check.run(ms, lk, req, tmp_path / "pkg", figures=figs)
    assert any("too small even for one column" in b for b in r["blocking"])


def test_missing_supplementary_file_blocks(tmp_path):
    import docx

    ms, lk = setup(tmp_path)
    d = docx.Document(ms)
    d.add_paragraph("Raw counts are in Table S1 and Table S2.")
    ms2 = tmp_path / "m2.docx"
    d.save(ms2)
    lock.create(ms2, lk)
    supp = tmp_path / "supp"
    supp.mkdir()
    (supp / "Table S1.csv").write_text("a,b\n1,2\n")
    req = write_req(tmp_path, {"type": "manual"})
    r = check.run(ms2, lk, req, tmp_path / "pkg", supp=supp)
    assert any("Table S2" in b and "no supplementary file" in b for b in r["blocking"])
    assert (tmp_path / "pkg" / "supplementary" / "Table S1.csv").exists()


def test_vector_pdf_figure_rendered_when_journal_wants_tiff(tmp_path):
    import shutil as sh

    import pytest

    if not sh.which("pdftoppm"):
        pytest.skip("pdftoppm not installed")
    ms, lk = setup(tmp_path)
    figs = tmp_path / "figs"
    figs.mkdir()
    Image.new("RGB", (600, 400), (10, 120, 200)).save(figs / "Figure_1.pdf", "PDF", resolution=300)
    req = write_req(tmp_path, {"id": "figure-files", "type": "figure_files", "formats": ["tif", "tiff"],
                               "name_pattern": "Fig{n}"})
    r = check.run(ms, lk, req, tmp_path / "pkg", figures=figs)
    assert (tmp_path / "pkg" / "figures" / "Fig1.tif").exists()
    assert any("600 dpi" in d for d in r["done"])
    assert any("vector PDF" in c for c in r["checks"])


def test_rerun_on_manuscript_inside_package(tmp_path):
    # second /check run on the copy that the first run put in the package folder
    ms, lk = setup(tmp_path)
    req = write_req(tmp_path, {"type": "max_chars", "target": "title", "value": 500})
    check.run(ms, lk, req, tmp_path / "pkg")
    inside = tmp_path / "pkg" / "m.docx"
    r = check.run(inside, lk, req, tmp_path / "pkg")
    assert r["verdict"] == "READY", r["blocking"]
    assert inside.exists() and inside.stat().st_size > 0


def test_table_with_empty_first_cells_is_flagged(tmp_path):
    # "Table 2 has information misalignment issues such as missing gene name filling in partial rows"
    import docx
    d = docx.Document()
    d.add_paragraph("A made-up table study")
    t = d.add_table(rows=4, cols=3)
    for r, row in enumerate([["Gene", "Site", "Value"], ["GENEA", "cg01", "0.1"], ["", "cg02", "0.2"], ["GENEB", "cg03", "0.3"]]):
        for c, v in enumerate(row):
            t.cell(r, c).text = v
    m = d.add_table(rows=3, cols=2)  # a merged first column is fine
    m.cell(0, 0).text, m.cell(0, 1).text = "Group", "Item"
    m.cell(1, 0).text, m.cell(1, 1).text = "G1", "a"
    m.cell(2, 1).text = "b"
    m.cell(1, 0).merge(m.cell(2, 0))
    p = tmp_path / "t.docx"
    d.save(p)
    gaps = check.table_gaps(p)
    assert len(gaps) == 1 and gaps[0]["item"].startswith("Table 1") and "rows 3" in gaps[0]["problem"]
