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
