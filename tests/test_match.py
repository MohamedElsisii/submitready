"""Journal finder tests with a fake network (no internet needed)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import match  # noqa: E402
from test_form import make  # noqa: E402  (a small made-up manuscript)

TITLE = "A made-up title for testing the form sheet"


def work(i, sid, name, title=None):
    return {"id": f"W{i}", "title": title or f"Similar paper {i}", "publication_year": 2024,
            "primary_location": {"source": {"id": f"https://openalex.org/{sid}", "display_name": name, "type": "journal"}}}


SOURCES = {
    "S1": {"display_name": "Open Journal", "issn_l": "1111-1111", "issn": ["1111-1111"], "type": "journal",
           "is_oa": True, "is_in_doaj": True, "apc_usd": 2500, "listed_in": ["doaj", "medline"], "works_count": 5000,
           "last_publication_year": 2026, "summary_stats": {"2yr_mean_citedness": 4.2}, "host_organization_name": "Pub A"},
    "S2": {"display_name": "Hybrid Journal", "issn_l": "2222-2222", "issn": ["2222-2222"], "type": "journal",
           "is_oa": False, "is_in_doaj": False, "apc_usd": 3900, "listed_in": ["cwts-core"], "works_count": 9000,
           "last_publication_year": 2026, "summary_stats": {"2yr_mean_citedness": 6.0}, "host_organization_name": "Pub B"},
    "S3": {"display_name": "Unknown Journal", "issn_l": "3333-3333", "issn": ["3333-3333"], "type": "journal",
           "is_oa": True, "is_in_doaj": False, "apc_usd": 900, "listed_in": [], "works_count": 4000,
           "last_publication_year": 2026, "summary_stats": {"2yr_mean_citedness": 1.0}},
    "S4": {"display_name": "Pricey Open Journal", "issn_l": "4444-4444", "issn": ["4444-4444"], "type": "journal",
           "is_oa": True, "is_in_doaj": True, "apc_usd": 5000, "listed_in": ["doaj"], "works_count": 3000,
           "last_publication_year": 2026, "summary_stats": {"2yr_mean_citedness": 8.0}},
}
MIX = {"S1": [("gold", 100)], "S2": [("closed", 60), ("hybrid", 30), ("green", 10)], "S3": [("gold", 50)], "S4": [("gold", 40)]}
PUBMED_XML = """<PubmedArticleSet>""" + "".join(
    f"""<PubmedArticle><PubmedData><History>
    <PubMedPubDate PubStatus="received"><Year>2025</Year><Month>1</Month><Day>1</Day></PubMedPubDate>
    <PubMedPubDate PubStatus="accepted"><Year>2025</Year><Month>{m}</Month><Day>1</Day></PubMedPubDate>
    </History></PubmedData></PubmedArticle>""" for m in (2, 3, 3, 4, 5, 6)) + "</PubmedArticleSet>"


class FakeNet:
    email = ""

    def json(self, url):
        if "/works?search=" in url:
            ws = [work(0, "S1", "Open Journal", TITLE)]  # the manuscript itself: must not count
            ws += [work(i, "S1", "Open Journal") for i in range(1, 6)]
            ws += [work(i, "S2", "Hybrid Journal") for i in range(6, 10)]
            ws += [work(i, "S3", "Unknown Journal") for i in range(10, 13)]
            ws += [work(i, "S4", "Pricey Open Journal") for i in range(13, 15)]
            return {"results": ws}
        if "/sources/" in url:
            return SOURCES.get(url.rsplit("/", 1)[-1])
        if "group_by=oa_status" in url:
            sid = url.split("primary_location.source.id:")[1].split(",")[0]
            return {"group_by": [{"key": k, "count": c} for k, c in MIX[sid]]}
        if "db=nlmcatalog" in url:
            return {"esearchresult": {"count": "1" if "1111-1111" in url else "0"}}
        if "db=pubmed" in url:
            return {"esearchresult": {"idlist": ["1", "2", "3", "4", "5", "6"] if "1111-1111" in url else []}}
        if "/works?filter=primary_location" in url:
            return {"results": []}
        return None

    def text(self, url):
        return PUBMED_XML if "efetch" in url else None


@pytest.fixture
def paper(tmp_path):
    return make(tmp_path)


def test_find_ranks_screens_and_measures(paper):
    d = match.find(paper, FakeNet())
    names = [j["name"] for j in d["journals"]]
    assert names == ["Open Journal", "Hybrid Journal", "Pricey Open Journal"]
    assert d["journals"][0]["similar_papers"] == 5               # the manuscript itself was not counted
    left = {x["name"]: x["reason"] for x in d["left_out"]}
    assert "could not be confirmed as a legitimate journal" in left["Unknown Journal"]
    o, h = d["journals"][0], d["journals"][1]
    assert o["access"].startswith("Fully open access") and o["fee_usd"] == 2500
    assert h["access"].startswith("Hybrid")
    assert o["medline"] is True and h["medline"] is False          # from the NLM Catalog
    assert o["review"]["days"] == 74 and "PubMed" in o["review"]["source"]
    assert h["review"]["days"] is None and "not found" in h["review"]["source"]


@pytest.mark.parametrize("kw,expected", [
    ({"access": "open"}, ["Open Journal", "Pricey Open Journal"]),
    ({"access": "hybrid"}, ["Hybrid Journal"]),
    ({"access": "open", "max_fee": 3000}, ["Open Journal"]),
    ({"medline": True}, ["Open Journal"]),
    ({"prefer": "impact"}, ["Pricey Open Journal", "Hybrid Journal", "Open Journal"]),
])
def test_filters_and_preferences(paper, kw, expected):
    d = match.find(paper, FakeNet(), **kw)
    assert [j["name"] for j in d["journals"]] == expected


def test_table_never_invents_numbers(paper):
    d = match.find(paper, FakeNet())
    md = match.table(d)
    assert "not found" in md                                       # impact factor not read yet
    assert "check SCImago" in md and "double-check" in md.lower()
    match.add_fact(d, 1, "impact_factor", "3.2", "https://example.org/journal/metrics", "2024")
    assert "3.2 (2024) [source](https://example.org/journal/metrics)" in match.table(d)
    with pytest.raises(SystemExit):
        match.add_fact(d, 1, "impact_factor", "9.9", "from memory")    # a fact needs its web address
    with pytest.raises(SystemExit):
        match.add_fact(d, 1, "made_up_field", "x", "https://example.org")


def test_choose_then_next_after_rejection(paper):
    d = match.find(paper, FakeNet())
    match.choose(d, 1)
    assert d["journals"][0]["status"] == "chosen"
    nxt = match.next_journal(d)
    assert d["journals"][0]["status"] == "rejected" and nxt["name"] == "Hybrid Journal"
    match.choose(d, nxt["rank"])
    match.next_journal(d)
    match.choose(d, 3)
    assert match.next_journal(d) is None                           # list used up: search again


def test_article_type_detection(paper):
    assert match.profile(paper)["article_type"] in ("research article", "review")
    assert match._terms("The role of a novel approach to methylation in luminal breast cancer")[:3] == \
        ["methylation", "luminal", "breast"]


def test_one_failing_source_does_not_sink_the_list(paper):
    class Flaky(FakeNet):
        def text(self, url):
            raise RuntimeError("Could not reach eutils.ncbi.nlm.nih.gov")
    d = match.find(paper, Flaky())
    assert d["journals"] and all("not found" in j["review"]["source"] for j in d["journals"])
