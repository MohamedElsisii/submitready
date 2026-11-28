"""The freshness check: quotes still on the page, quotes gone, pages that did not load."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import freshness as F  # noqa: E402

PAGE = """<html><body><h1>Guide</h1>
<p>Please provide an abstract of 150 to 250 words; it must stand alone.</p>
<p>Use “double” line spacing&nbsp;throughout – including references.</p>
<p>Data availability statements are required.</p>
</body></html>"""


def _snapshot(tmp: Path, quotes: list[str]) -> Path:
    req = tmp / "requirements"
    req.mkdir()
    d = {"journal": "Test", "sources": [{"name": "Guide", "url": "https://example.org/guide", "checked": "2026-11-24"}],
         "requirements": [{"id": f"r{i}", "rule": "x", "quote": q, "source": 0, "check": {"type": "manual"}}
                          for i, q in enumerate(quotes)]}
    (req / "test.2026-11-24.json").write_text(json.dumps(d), encoding="utf-8")
    (tmp / "families").mkdir()
    return req


def _run(tmp, monkeypatch, quotes, page=PAGE, status="ok-text"):
    req = _snapshot(tmp, quotes)
    monkeypatch.setattr(F, "REQ", req)
    monkeypatch.setattr(F, "FAM", tmp / "families")
    fetched = []

    def fake(url):
        fetched.append(url)
        return F.Page(url, status, F.html_text(page) if status == "ok-text" else "", "")
    return F.run(fetcher=fake), fetched


def test_quotes_found_despite_layout(tmp_path, monkeypatch):
    rep, _ = _run(tmp_path, monkeypatch, [
        "Please provide an abstract of 150 to 250 words.",          # page goes on with ";"
        'Use "double" line spacing throughout - including references.',  # curly quotes, nbsp, en dash
        "Data availability statements are required."])
    assert rep["counts"] == {"ok": 1}
    assert rep["results"][0]["found"] == 3


def test_planted_change_is_caught(tmp_path, monkeypatch):
    rep, _ = _run(tmp_path, monkeypatch, [
        "Please provide an abstract of 150 to 250 words.",
        "Data availability statements are required.",
        "Please provide 4 to 6 keywords."])                      # not on the page
    r = rep["results"][0]
    assert r["status"] == "changed" and [m["id"] for m in r["missing"]] == ["r2"]
    md = F.report_markdown(rep)
    assert "## Changed" in md and "Please provide 4 to 6 keywords." in md


def test_changed_number_is_caught(tmp_path, monkeypatch):
    page = PAGE.replace("150 to 250", "150 to 300")
    rep, _ = _run(tmp_path, monkeypatch, ["Please provide an abstract of 150 to 250 words.",
                                          "Data availability statements are required."], page=page)
    assert rep["results"][0]["status"] == "changed"


def test_bot_page_is_not_reported_as_changed(tmp_path, monkeypatch):
    bot = "<html><body>Client Challenge. A required part of this site couldn't load.</body></html>"
    rep, _ = _run(tmp_path, monkeypatch, ["Please provide an abstract of 150 to 250 words.",
                                          "Data availability statements are required.",
                                          "Use double line spacing."], page=bot)
    assert rep["results"][0]["status"] == "unclear"
    assert "changed" not in rep["counts"]


def test_unreachable_and_cache(tmp_path, monkeypatch):
    rep, _ = _run(tmp_path, monkeypatch, ["Data availability statements are required."], status="unreachable")
    assert rep["results"][0]["status"] == "unreachable"
    md = F.report_markdown(rep)
    assert F.key("https://example.org/guide") in md
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / f"{F.key('https://example.org/guide')}.txt").write_text(
        "Data availability statements are **required**.", encoding="utf-8")
    called = []
    rep = F.run(cache=cache, fetcher=lambda u: called.append(u) or F.Page(u, "unreachable"))
    assert rep["counts"] == {"ok": 1} and not called


def test_old_check_is_listed(tmp_path, monkeypatch):
    rep, _ = _run(tmp_path, monkeypatch, ["Data availability statements are required."])
    rep["results"][0]["days_old"] = 400
    assert "more than 180 days ago" in F.report_markdown(rep)


def test_real_snapshots_have_checkable_sources():
    # Every snapshot source with quotes has a URL, so the monthly check can read it.
    for f in F.snapshots():
        d = json.loads(f.read_text(encoding="utf-8"))
        used = {int(r.get("source", 0)) for r in d["requirements"] if r.get("quote")}
        for i in used:
            assert d["sources"][i].get("url", "").startswith("http"), (f.name, i)


def test_autolinks_and_link_text_match():
    page = "See <https://doi.org/abc> and [https://doi.org/xyz](https://doi.org/xyz) here. After the [References](#s7) section."
    body = F.norm(page)
    assert F.quote_key("See https://doi.org/abc and https://doi.org/xyz here.") in body
    assert F.quote_key("After the References section.") in body


def test_a_requirements_file_can_be_checked_directly(tmp_path):
    rq = tmp_path / "ms_journal.requirements.json"
    rq.write_text(json.dumps({"journal": "J", "sources": [{"url": "https://example.org/g", "checked": "2026-11-25"}],
                              "requirements": [{"id": "a", "rule": "r", "quote": "Use 10-point Times Roman.", "source": 0},
                                               {"id": "b", "rule": "r", "quote": "A rule that is not there.", "source": 0}]}))
    page = "Text formatting. Use a normal, plain font. Use 10-point Times Roman. " * 60
    rep = F.run(files=[rq], fetcher=lambda url: F.Page(url, "ok-text", page))
    r = rep["results"][0]
    assert r["status"] == "changed" and r["found"] == 1 and r["missing"][0]["id"] == "b"


def test_a_missing_quote_shows_the_closest_sentence_on_the_page():
    page = "Abstract. The abstract would normally be 150-250 words. Use plain language. " * 30
    c = F.closest("The abstract would normally be 150–200 words.", page)
    assert c["closest"].startswith("The abstract would normally be 150-250 words") and c["similarity"] > 0.8
    assert F.closest("Something unrelated about peer review fees.", page) == {}


def test_pages_without_a_charset_are_read_as_utf8():
    raw = "The abstract would normally be 150–200 words.".encode("utf-8")
    assert F.decode(raw, "text/html") == "The abstract would normally be 150–200 words."
    assert F.decode("café".encode("cp1252"), "text/html") == "café"
    assert F.decode("x".encode("utf-16"), "text/html; charset=utf-16") == "x"
