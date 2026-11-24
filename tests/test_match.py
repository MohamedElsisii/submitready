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


# ---- lists the researcher downloads (SCImago, Scopus): made-up miniature files ----

def _lists(tmp_path, monkeypatch):
    import openpyxl
    import journaldata
    monkeypatch.setenv("SUBMITREADY_DATA", str(tmp_path / "data"))
    journaldata._cache.clear()
    csvp = tmp_path / "scimagojr_2025.csv"
    csvp.write_text(
        "Rank;Sourceid;Title;Type;Issn;Publisher;Open Access;Open Access Diamond;SJR;SJR Best Quartile;H index;"
        "Total Docs. (2025);Total Docs. (3years);Total Refs.;Total Citations (3years);Citable Docs. (3years);"
        "Citations / Doc. (2years);Ref. / Doc.;%Female;Overton;Country;Region;Publisher;Coverage;Categories;Areas\n"
        '1;11;"Open Journal";journal;"11111111";"Pub A";Yes;No;2,500;Q1;100;1;1;1;1;1;1,0;1;1;0;X;Y;"Pub A";"2000-2026";"Genetics (Q1)";"Bio"\n'
        '2;22;"Hybrid Journal";journal;"22222222";"Pub B";No;No;0,400;Q3;50;1;1;1;1;1;1,0;1;1;0;X;Y;"Pub B";"2000-2026";"Genetics (Q3)";"Bio"\n',
        encoding="utf-8")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Scopus Sources Oct. 2026"
    ws.append(["Sourcerecord ID", "Source Title", "ISSN", "EISSN", "Active or Inactive", "Coverage",
               "Titles Discontinued by Scopus", "Article Language in Source", "Medline-sourced Title?",
               "Open Access Status", "Articles in Press Included?", "Added to List October 2026", "Source Type"])
    ws.append([1, "Open Journal", "11111111", "", "Active", "2000-2026", "", "ENG", "Medline", "", "", "", "Journal"])
    ws.append([2, "Hybrid Journal", "22222222", "", "Active", "2000-2026", "", "ENG", "", "", "", "", "Journal"])
    ws.append([4, "Pricey Open Journal", "44444444", "", "Inactive", "2000-2024", "Discontinued by Scopus", "ENG", "", "", "", "", "Journal"])
    xl = tmp_path / "ext_list_Oct_2026.xlsx"
    wb.save(xl)
    said = journaldata.add([csvp, xl])
    assert any("SCImago 2025" in x for x in said) and any("1 discontinued" in x for x in said)
    return journaldata


def test_downloaded_lists_add_quartiles_and_screen(paper, tmp_path, monkeypatch):
    jd = _lists(tmp_path, monkeypatch)
    d = match.find(paper, FakeNet())
    names = [j["name"] for j in d["journals"]]
    assert "Pricey Open Journal" not in names                      # discontinued by Scopus
    left = {x["name"]: x["reason"] for x in d["left_out"]}
    assert "discontinued by Scopus" in left["Pricey Open Journal"]
    md = match.table(d)
    assert "Q1 (SJR 2025)" in md and "SCImago Journal & Country Rank" in md
    assert [j["name"] for j in match.find(paper, FakeNet(), min_quartile="Q2")["journals"]] == ["Open Journal"]
    assert [j["name"] for j in match.find(paper, FakeNet(), scopus=True)["journals"]] == ["Open Journal", "Hybrid Journal"]
    jd._cache.clear()


def test_without_lists_quartiles_are_links_and_filters_explain(paper, tmp_path, monkeypatch):
    import journaldata
    monkeypatch.setenv("SUBMITREADY_DATA", str(tmp_path / "empty"))
    journaldata._cache.clear()
    d = match.find(paper, FakeNet(), min_quartile="Q1")
    assert not d["journals"] and all("SCImago file was not added" in x["reason"] or "legitimate" in x["reason"]
                                     for x in d["left_out"])
    assert "check SCImago" in match.table(match.find(paper, FakeNet()))


def test_keyword_pairs_are_searched():
    """Each pair of keywords is its own search, so a paper's narrower angle (e.g. DNA methylation within
    breast cancer) reaches the specialist journals. Seen with a real paper accepted in an epigenetics journal."""
    asked = []

    class Recorder(FakeNet):
        def json(self, url):
            asked.append(url)
            return super().json(url)

    p = {"title": "A made-up study", "abstract": "", "keywords": ["Breast Cancer", "DNA Methylation", "Machine Learning"],
         "article_type": "research article", "approach": ""}
    match.similar_works(p, Recorder())
    searches = [u.split("search=")[1].split("&")[0] for u in asked if "/works?search=" in u]
    import urllib.parse
    searches = [urllib.parse.unquote(s) for s in searches]
    assert "Breast Cancer DNA Methylation" in searches and "DNA Methylation Machine Learning" in searches
