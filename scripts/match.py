#!/usr/bin/env python3
"""SubmitReady journal finder.

Finds journals that publish papers like yours, from open data, and lists them with
the numbers that matter. Nothing is guessed: each number carries its source, and a
number that cannot be found is shown as "not found".

How a journal gets on the list
  1. Similar papers: OpenAlex is searched with your title, your keywords together and each
     specific keyword on its own, and the key terms of your abstract (computational papers also
     get method-aware searches). Journals that published the most similar papers in the last five
     years are the candidates, with a mild correction for journal size.
  2. Screening: a journal must be in DOAJ, MEDLINE or the CWTS core list of journals, still
     publishing, and a journal (not a repository). Others are left out, with the reason.
  3. Your filters: access model, maximum publication fee, MEDLINE.
  4. Facts per journal: access model and fee (OpenAlex), MEDLINE (NLM Catalog), 2-year mean
     citations per paper (OpenAlex; not the official Impact Factor), median days from
     submission to acceptance (PubMed history dates, else Crossref).

Facts that live on journal websites (impact factor as the journal states it, aims and scope,
accepted article types) are read live by Claude and recorded with `add`, with their source.

    python match.py find paper.docx --out paper.journals.json [--access any|open|hybrid|subscription]
                         [--max-fee 2000] [--medline] [--prefer fit|speed|impact] [--email you@uni.edu]
    python match.py add paper.journals.json <rank> --field impact_factor --value 3.2 --year 2024 --source URL
    python match.py table paper.journals.json
    python match.py choose paper.journals.json <rank>
    python match.py next paper.journals.json        # after a rejection: marks the chosen one rejected
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import statistics
import sys
import time
import urllib.parse
from collections import Counter
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import form  # noqa: E402
import journaldata  # noqa: E402
import prepare  # noqa: E402

OPENALEX = "https://api.openalex.org"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
CROSSREF = "https://api.crossref.org"
CREDIBLE_LISTS = {"medline", "cwts-core", "doaj", "scopus"}
STOP = set("""a an and the of in on for to with by from as at or is are was were be been this that these those
using use based via into their its our we study analysis approach new novel role towards toward between among
than within without under over through effect effects""".split())
SHOWN, KEPT = 5, 10
MAX_CHECKED = 90  # journals looked up before the search stops widening
OFF_TOPIC = 0.2  # topic overlap below this, with 2 similar papers or fewer: left out
SIZE_UNIT = 500  # items in 2 years; journals larger than this get the size correction
COMPUTATIONAL = ["in silico", "computational", "docking", "molecular dynamics", "simulation", "alphafold",
                 "structure prediction", "modeling", "modelling", "machine learning", "deep learning", "bioinformatic"]
METHOD_QUERY = '("in silico" OR docking OR "molecular dynamics" OR computational OR simulation)'
GENERIC = 20000  # a keyword matching more papers than this (in 5 years) is too broad to use on its own
FIELDS = {"impact_factor", "quartile", "scope_quote", "scope_url", "accepts_type", "fee_note", "note"}


# ---------------------------------------------------------------- network (injectable for tests)
class Net:
    def __init__(self, email: str = "", pause: float = 0.1):
        import requests
        self.s = requests.Session()
        self.s.headers["User-Agent"] = ("SubmitReady (https://github.com/MohamedElsisii/submitready"
                                        + (f"; mailto:{email})" if email else ")"))
        self.email, self.pause = email, pause

    def _get(self, url: str):
        import requests
        for attempt in range(3):
            try:
                r = self.s.get(url, timeout=30)
            except requests.RequestException:
                time.sleep(1 + attempt)
                continue
            if r.status_code == 404:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 + 2 * attempt)
                continue
            r.raise_for_status()
            time.sleep(self.pause)
            return r
        raise RuntimeError(f"Could not reach {url.split('/')[2]}")

    def json(self, url: str):
        r = self._get(url)
        return r.json() if r is not None else None

    def text(self, url: str):
        r = self._get(url)
        return r.text if r is not None else None


# ---------------------------------------------------------------- the manuscript
def profile(path: Path) -> dict:
    d = form.build(path)
    f = d["facts"]
    title = d["title"]
    secs = set(f.get("sections_found", []))
    t = title.lower()
    if re.search(r"\b(review|overview|perspective|survey)\b", t) or not ({"methods", "results"} & secs):
        kind = "review"
    elif re.search(r"\bstudy protocol\b|\bprotocol for\b", t):
        kind = "study protocol"
    elif re.search(r"\b(pipeline|tool|software|method|workflow|database|server)\b", t):
        kind = "methods or resource"
    else:
        kind = "research article"
    kw = [k.strip(" .") for k in re.split(r"[;,•|]", d["keywords"]) if k.strip(" .")] or f.get("keywords", [])
    abstract = "\n\n".join(d["abstract"])
    text = (title + " " + abstract).lower()
    signals = [w for w in COMPUTATIONAL if w in text]
    return {"title": title, "abstract": abstract, "keywords": kw, "article_type": kind,
            "approach": "computational" if len(signals) >= 3 else "", "approach_signals": signals,
            "main_words": f.get("main_text_words", 0)}


FILLER = set("""particularly key while however here show shows shown showed results result findings finding potential
high low significant significantly also may can could which both these such well including include includes included
however thus therefore further furthermore moreover overall respectively compared various several different important
provide provides provided suggest suggests revealed reveal identify identified present presents demonstrate demonstrates
first second third one two three many most more less among across used use uses within without whereas although""".split())


def key_terms(text: str, n: int = 16) -> list[str]:
    """The most frequent meaningful terms of a text (ties: first seen first)."""
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]+", text)]
    freq: dict[str, int] = {}
    first: dict[str, int] = {}
    for i, w in enumerate(words):
        if w in STOP or w in FILLER or len(w) <= 2:
            continue
        freq[w] = freq.get(w, 0) + 1
        first.setdefault(w, i)
    return sorted(freq, key=lambda w: (-freq[w], first[w]))[:n]


def _terms(text: str, n: int = 12) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-]+", text)
    out = []
    for w in words:
        lw = w.lower()
        if lw not in STOP and len(lw) > 2 and lw not in out:
            out.append(lw)
    return out[:n]


# Conference abstracts printed in journal supplements carry numbers in front of the title:
# "374P ...", "1234PD ...", "P1-01-02 ...", "LBA12 ...", "TPS1101 ...", "e13001 ...", "Abstract 512: ...".
# Gene and protein names ("3D genome", "S100A4", "PD-1", "P21-activated") must not match.
MEETING_ABSTRACT = re.compile(
    r"^\s*(?:\d{1,5}(?:P|PD|MO|O|TiP|eP)"
    r"|(?:P|PS|PD|GS|OT|SS|PO)\d{1,2}-\d{1,3}(?:-\d{1,3})?"
    r"|(?:LBA|TPS|Abstract)\s?\d{1,5}"
    r"|e\d{4,6})\s*[:.]?\s+\S")


def meeting_abstract(title: str) -> bool:
    return bool(MEETING_ABSTRACT.match(title))


# ---------------------------------------------------------------- candidates
def similar_works(p: dict, net: Net, years: int = 5) -> list[dict]:
    """Papers like this one. First the precise searches (title; keywords together; or, without keywords,
    small groups of the abstract's top terms). They also show the paper's research fields (OpenAlex),
    and the broader searches (each keyword alone, method-aware, abstract terms) stay inside those
    fields, so an ecology paper cannot drift into physics."""
    since = f"{date.today().year - years}-01-01"
    kind = "review" if p["article_type"] == "review" else "article"
    top = key_terms(p["abstract"] or p["title"], 8)
    precise = [(" ".join(_terms(p["title"], 12)), 1.0, 100)]
    if p["keywords"]:
        precise.append((" ".join(p["keywords"][:6]), 1.0, 100))
    elif len(top) >= 6:
        precise += [(" ".join(g), 0.8, 50) for g in ((top[0], top[1], top[2]), (top[0], top[3], top[4]), (top[1], top[3], top[5]))]
    broad = [(k, 0.6, 50) for k in p["keywords"][:6]]
    # each pair of keywords finds the paper's angles: "DNA Methylation" + "Breast Cancer" reaches the
    # epigenetics journals that all five keywords together, or each one alone, miss
    kw = p["keywords"][:5]
    broad += [(f"{a} {b}", 0.8, 50) for i, a in enumerate(kw) for b in kw[i + 1:]]
    if p.get("approach") == "computational":
        broad += [(f'"{k}" AND {METHOD_QUERY}', 1.0, 50) for k in p["keywords"][:6]]
    wide = key_terms(p["abstract"] or " ".join([p["title"], " ".join(p["keywords"])]), 16)
    seen: dict[str, int] = {}
    works: list[dict] = []
    fields: dict[str, float] = {}
    topics: dict[str, float] = {}       # the paper's topics, from the papers most like it
    subfields: dict[str, str] = {}

    def run(qtext, w_query, per_page, field_filter=""):
        url = (f"{OPENALEX}/works?search={urllib.parse.quote(qtext)}"
               f"&filter=from_publication_date:{since},type:{kind},is_paratext:false{field_filter}&per-page={per_page}"
               f"&select=id,title,primary_location,publication_year,primary_topic"
               + (f"&mailto={urllib.parse.quote(net.email)}" if net.email else ""))
        data = net.json(url) or {}
        if w_query < 1.0 and qtext in p["keywords"] and (data.get("meta") or {}).get("count", 0) > GENERIC:
            return 0  # a keyword shared by tens of thousands of papers says little about this one
        res = data.get("results", [])
        for rank, w in enumerate(res):
            # the manuscript itself (already published, or a preprint) does not count
            if difflib.SequenceMatcher(None, (w.get("title") or "").lower(), p["title"].lower()).ratio() > 0.9:
                continue
            src = ((w.get("primary_location") or {}).get("source") or {})
            if not (src.get("id") and src.get("type") == "journal"):
                continue
            if meeting_abstract(w.get("title") or ""):
                continue  # a conference abstract in a supplement says little about the journal's papers
            weight = w_query / (1 + rank / 25)
            if not field_filter:
                f = (((w.get("primary_topic") or {}).get("field") or {}).get("id") or "").rsplit("/", 1)[-1]
                if f:
                    fields[f] = fields.get(f, 0) + weight
                pt = w.get("primary_topic") or {}
                tid = (pt.get("id") or "").rsplit("/", 1)[-1]
                sub = ((pt.get("subfield") or {}).get("id") or "").rsplit("/", 1)[-1]
                if tid:
                    topics[tid] = topics.get(tid, 0) + weight
                    subfields[tid] = sub
            if w["id"] in seen:  # found again by another search: more weight, counted once
                works[seen[w["id"]]]["weight"] += weight
                continue
            seen[w["id"]] = len(works)
            works.append({"source": src["id"].rsplit("/", 1)[-1], "name": src.get("display_name"),
                          "weight": weight, "title": w.get("title")})
        return len(res)

    for q in precise:
        run(*q)
    # the fields that hold most of the precise results (at most 3)
    keep, total, acc = [], sum(fields.values()), 0.0
    for f, w in sorted(fields.items(), key=lambda x: -x[1]):
        if total and acc / total >= 0.75 or len(keep) == 3:
            break
        keep.append(f)
        acc += w
    ff = f",primary_topic.field.id:{'|'.join(keep)}" if keep and total >= 5 else ""
    p["fields"] = keep
    tsum = sum(topics.values()) or 1.0
    top = sorted(topics.items(), key=lambda x: -x[1])[:15]
    p["topics"] = {t: round(w / tsum, 4) for t, w in top}
    p["topic_subfields"] = {t: subfields.get(t, "") for t, _ in top}
    for q in broad:
        run(*q, field_filter=ff)
    if len(wide) >= 3:
        run(" OR ".join(wide), 0.5 if len(works) < 60 else 0.35, 100, field_filter=ff)
    return works


def journal_facts(sid: str, net: Net) -> dict | None:
    s = net.json(f"{OPENALEX}/sources/{sid}")
    if not s:
        return None
    mix = net.json(f"{OPENALEX}/works?filter=primary_location.source.id:{sid},"
                   f"from_publication_date:{date.today().year - 2}-01-01&group_by=oa_status") or {}
    groups = {g["key"]: g["count"] for g in mix.get("group_by", [])}
    total = sum(groups.values()) or 1
    open_share = (groups.get("gold", 0) + groups.get("diamond", 0)) / total
    apc = s.get("apc_usd")
    if s.get("is_oa") or s.get("is_in_doaj") or open_share >= 0.9:
        access = "Fully open access" + (" (no fee)" if apc == 0 else "")
    elif groups.get("hybrid", 0) > 0:
        access = "Hybrid (subscription, open access optional)"
    else:
        access = "Subscription"
    listed = set(s.get("listed_in") or [])
    if s.get("is_in_doaj"):
        listed.add("doaj")
    mine = journaldata.lookup(s.get("issn") or [])
    if mine.get("scopus", {}).get("status") == "Active" and not mine["scopus"].get("discontinued"):
        listed.add("scopus")
    return {
        "scimago": mine.get("scimago"), "scopus": mine.get("scopus"),
        "openalex_id": sid, "name": s.get("display_name"), "issn_l": s.get("issn_l"), "issn": s.get("issn") or [],
        "publisher": s.get("host_organization_name"), "homepage": s.get("homepage_url"),
        "type": s.get("type"), "works_count": s.get("works_count", 0),
        "last_publication_year": s.get("last_publication_year"),
        "access": access, "fee_usd": apc, "open_share_2y": round(open_share, 2),
        "recent_works_2y": sum(groups.values()),
        "medline": "medline" in listed, "listed_in": sorted(listed),
        "citations_2y_mean": (s.get("summary_stats") or {}).get("2yr_mean_citedness"),
        "h_index": (s.get("summary_stats") or {}).get("h_index"),
        "topics": [(t.get("id", "").rsplit("/", 1)[-1], ((t.get("subfield") or {}).get("id") or "").rsplit("/", 1)[-1])
                   for t in (s.get("topics") or [])[:25]],
    }


def medline_now(j: dict, net: Net) -> bool | None:
    """NLM Catalog: is the journal currently indexed for MEDLINE? None if NLM could not be asked."""
    for issn in j.get("issn") or []:
        term = urllib.parse.quote(f"{issn}[issn] AND currentlyindexed[All]")
        try:
            r = net.json(f"{EUTILS}/esearch.fcgi?db=nlmcatalog&term={term}&retmode=json&tool=submitready")
        except Exception:
            return None
        if r and int((r.get("esearchresult") or {}).get("count", 0)) > 0:
            return True
    return False


def topic_overlap(j: dict, p: dict) -> float | None:
    """How much of the paper's topic profile sits in the journal's 25 main topics (0 to 1).

    A topic the journal lists counts fully; a paper topic whose subfield the journal covers
    counts a third. None when either side has no topics."""
    pt, js = p.get("topics") or {}, j.get("topics") or []
    if not pt or not js:
        return None
    jt = {t for t, _ in js}
    jsub = {sub for _, sub in js if sub}
    sub_of = p.get("topic_subfields") or {}
    return round(sum(w if t in jt else (w / 3 if sub_of.get(t) in jsub else 0) for t, w in pt.items()), 3)


def screen(j: dict) -> str:
    """Empty string if the journal passes; otherwise the reason it was left out."""
    if j.get("type") != "journal":
        return "not a journal"
    sc = j.get("scopus") or {}
    if sc.get("discontinued"):
        return (f"discontinued by Scopus{' in ' + str(sc['discontinued_year']) if sc.get('discontinued_year') else ''}, "
                f"a quality warning (Scopus Source List {sc.get('year', '')})")
    if not (CREDIBLE_LISTS & set(j.get("listed_in", []))):
        return "not in DOAJ, MEDLINE or the CWTS core list, so it could not be confirmed as a legitimate journal"
    if (j.get("last_publication_year") or 0) < date.today().year - 1:
        return "no longer publishing"
    if (j.get("works_count") or 0) < 100:
        return "too few published papers to judge"
    return ""


def _quartiles(j: dict, basis: str) -> list[int]:
    """The journal's SJR quartiles as numbers: its best one, or one per subject category."""
    sj = j.get("scimago") or {}
    if basis == "any":
        found = [int(q) for q in re.findall(r"\(Q([1-4])\)", sj.get("categories") or "")]
        if found:
            return found
    q = sj.get("quartile") or ""
    return [int(q[1])] if q else []


def apply_filters(j: dict, access: str, max_fee: float | None, medline: bool, scopus: bool = False,
                  min_quartile: str = "", max_quartile: str = "", quartile_basis: str = "best") -> str:
    a = j["access"].lower()
    if access == "open" and not a.startswith("fully open"):
        return "not fully open access"
    if access == "subscription" and not (a.startswith("subscription") or a.startswith("hybrid")):
        return "fully open access (you asked for subscription journals)"
    if access == "hybrid" and not a.startswith("hybrid"):
        return "not a hybrid journal"
    if max_fee is not None and a.startswith("fully open") and (j.get("fee_usd") or 0) > max_fee:
        return f"publication fee {j['fee_usd']} USD is over your maximum of {max_fee:g} USD"
    if medline and not j.get("medline"):
        return "not indexed in MEDLINE"
    if scopus and "scopus" not in j.get("listed_in", []):
        return "not active in Scopus (or the Scopus Source List was not added)"
    if min_quartile or max_quartile:
        qs = _quartiles(j, quartile_basis)
        if not qs:
            return "no SJR quartile found (or the SCImago file was not added)"
        lo, hi = int((min_quartile or "Q4")[1]), int((max_quartile or "Q1")[1])
        if not any(hi <= q <= lo for q in qs):
            what = "best quartile" if quartile_basis == "best" else "quartiles"
            shown = ", ".join(f"Q{q}" for q in sorted(set(qs)))
            return f"SJR {what} {shown} outside your range Q{hi} to Q{lo}"
    return ""


def reason_group(reason: str) -> str:
    """A short label for counting why journals were left out."""
    r = reason.lower()
    for key, label in (("you asked for subscription", "fully open access"), ("not fully open", "not fully open access"),
                       ("not a hybrid", "not hybrid"), ("publication fee", "fee over your maximum"),
                       ("medline", "not in MEDLINE"), ("discontinued", "discontinued by Scopus"),
                       ("scopus", "not active in Scopus"), ("quartile", "SJR quartile outside your range"),
                       ("legitimate", "could not be confirmed as legitimate"), ("no longer", "no longer publishing"),
                       ("too few", "too few papers"), ("not a journal", "not a journal"),
                       ("main topics are far", "off topic")):
        if key in r:
            return label
    return reason


# ---------------------------------------------------------------- review time
def _median_days(pairs: list[tuple[date, date]]) -> int | None:
    days = [(b - a).days for a, b in pairs if a and b and 0 <= (b - a).days <= 1000]
    return int(statistics.median(days)) if len(days) >= 5 else None


def review_time(j: dict, net: Net) -> dict:
    """Median days from submission (received) to acceptance, from recent papers."""
    from lxml import etree
    year = date.today().year
    for issn in j.get("issn") or []:
        term = urllib.parse.quote(f"{issn}[is] AND ({year - 1}[dp] OR {year}[dp])")
        ids = ((net.json(f"{EUTILS}/esearch.fcgi?db=pubmed&term={term}&retmax=40&retmode=json&tool=submitready")
                or {}).get("esearchresult") or {}).get("idlist", [])
        if not ids:
            continue
        xml = net.text(f"{EUTILS}/efetch.fcgi?db=pubmed&id={','.join(ids)}&retmode=xml&tool=submitready")
        if not xml:
            continue
        pairs = []
        for a in etree.fromstring(xml.encode()).findall(".//PubmedArticle"):
            d = {}
            for p in a.findall(".//History/PubMedPubDate"):
                try:
                    d[p.get("PubStatus")] = date(int(p.findtext("Year")), int(p.findtext("Month")), int(p.findtext("Day")))
                except (TypeError, ValueError):
                    pass
            pairs.append((d.get("received"), d.get("accepted")))
        m = _median_days(pairs)
        if m is not None:
            n = sum(1 for a, b in pairs if a and b)
            return {"days": m, "papers": n, "source": f"PubMed history dates of {n} papers from {year - 1}-{year}"}
        break
    # Crossref: some publishers deposit received/accepted dates
    res = net.json(f"{OPENALEX}/works?filter=primary_location.source.id:{j['openalex_id']},"
                   f"from_publication_date:{year - 1}-01-01&per-page=25&select=doi") or {}
    pairs = []
    for w in res.get("results", []):
        doi = (w.get("doi") or "").replace("https://doi.org/", "")
        if not doi:
            continue
        m = (net.json(f"{CROSSREF}/works/{urllib.parse.quote(doi)}") or {}).get("message") or {}
        a = {x.get("name"): x.get("value") for x in m.get("assertion", [])}
        pairs.append((_parse_date(a.get("received")), _parse_date(a.get("accepted"))))
        if j.get("scope_url") is None and a.get("aims_and_scope_url"):
            j["scope_url_crossref"] = a["aims_and_scope_url"]
    md = _median_days(pairs)
    if md is not None:
        n = sum(1 for a, b in pairs if a and b)
        return {"days": md, "papers": n, "source": f"Crossref received/accepted dates of {n} papers from {year - 1}-{year}"}
    return {"days": None, "papers": 0, "source": "not found (the journal does not deposit these dates)"}


def _parse_date(v) -> date | None:
    if not v:
        return None
    for fmt in ("%d %B %Y", "%Y-%m-%d", "%d %b %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(v.strip(), fmt).date()
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------- find
def find(path: Path, net: Net, access: str = "any", max_fee: float | None = None, medline: bool = False,
         prefer: str = "fit", review_times: bool = True, scopus: bool = False, min_quartile: str = "",
         max_quartile: str = "", quartile_basis: str = "best") -> dict:
    p = profile(path)
    works = similar_works(p, net)
    score: dict[str, float] = {}
    count: dict[str, int] = {}
    examples: dict[str, list[str]] = {}
    for w in works:
        score[w["source"]] = score.get(w["source"], 0) + w["weight"]
        count[w["source"]] = count.get(w["source"], 0) + 1
        examples.setdefault(w["source"], []).append(w["title"])
    ranked = sorted(score, key=lambda s: -score[s])
    # a single similar paper is weak evidence: those journals come last, and only while the list is short
    strong = [s for s in ranked if count[s] >= 2]
    weak = [s for s in ranked if count[s] < 2]
    kept, left_out, checked = [], [], 0
    for sid in strong + weak:
        if checked >= MAX_CHECKED or (count[sid] < 2 and len(kept) >= KEPT):
            break
        checked += 1
        try:
            j = journal_facts(sid, net)
        except Exception:
            j = None
        if not j:
            continue
        if medline or len(kept) < KEPT:
            m = medline_now(j, net)
            if m is not None:
                j["medline"] = m
                j["medline_source"] = "NLM Catalog (currently indexed)"
        why = screen(j) or apply_filters(j, access, max_fee, medline, scopus, min_quartile, max_quartile, quartile_basis)
        if why:
            left_out.append({"name": j["name"], "reason": why})
            continue
        # size correction by recent output: a focused journal with 7 similar papers should outrank a
        # journal that prints thousands of items a year and happened to carry 5 like this one
        size = max(1.0, (j.get("recent_works_2y") or 0) / SIZE_UNIT)
        fit = score[sid] / size ** 0.5
        # topic check: does the journal's own main work sit where this paper sits?
        ov = topic_overlap(j, p)
        j["topic_overlap"] = ov
        if ov is not None:
            if ov < OFF_TOPIC and count[sid] <= 2:
                left_out.append({"name": j["name"], "reason": f"only {count[sid]} similar paper{'s' if count[sid] > 1 else ''}, "
                                 "and the journal's main topics are far from this paper's"})
                continue
            fit *= 0.5 + min(ov, 1.0)
        j["fit_score"] = round(fit, 2)
        j["similar_papers"] = count[sid]
        j["similar_examples"] = [t for t in examples[sid][:3] if t]
        kept.append(j)
        if len(kept) >= 2 * KEPT:
            break
    kept = sorted(kept, key=lambda j: -j["fit_score"])[:KEPT]
    if review_times:
        for j in kept:
            try:
                j["review"] = review_time(j, net)
            except Exception:  # one source hiccup must not sink the whole list
                j["review"] = {"days": None, "papers": 0, "source": "not found (the lookup failed; try again later)"}
    if prefer == "speed":
        kept.sort(key=lambda j: (j.get("review", {}).get("days") is None, j.get("review", {}).get("days") or 0))
    elif prefer == "impact":
        kept.sort(key=lambda j: -(j.get("citations_2y_mean") or 0))
    for n, j in enumerate(kept, 1):
        j["rank"] = n
        j["status"] = "suggested" if n <= SHOWN else "reserve"
        j["facts"] = {}
        j["sjr_link"] = (f"https://www.scimagojr.com/journalsearch.php?q={j['issn_l']}" if j.get("issn_l") else "")
    return {"tool": "SubmitReady journal finder", "manuscript": path.name, "checked": date.today().isoformat(),
            "profile": {k: p.get(k, "") for k in ("title", "keywords", "article_type", "approach", "fields")},
            "filters": {"access": access, "max_fee_usd": max_fee, "medline": medline, "prefer": prefer,
                        "scopus": scopus, "min_quartile": min_quartile, "max_quartile": max_quartile,
                        "quartile_basis": quartile_basis},
            "searched": {"journals_checked": checked, "passed": len(kept),
                         "left_out": dict(Counter(reason_group(x["reason"]) for x in left_out).most_common())},
            "lists": {k: {kk: (journaldata.load(k) or {}).get(kk) for kk in ("file", "year", "edition", "added")}
                      for k in ("scimago", "scopus") if journaldata.load(k)},
            "method": "Journals that published the most similar papers in the last 5 years (OpenAlex), "
                      "screened and filtered as listed below.",
            "journals": kept, "left_out": left_out}


# ---------------------------------------------------------------- facts read by Claude, choices
def add_fact(data: dict, rank: int, field: str, value: str, source: str, year: str = "") -> dict:
    if field not in FIELDS:
        raise SystemExit(f"Unknown field {field}. Use one of: {', '.join(sorted(FIELDS))}")
    if not source.startswith("http"):
        raise SystemExit("Every fact needs the web address it was read from.")
    j = _by_rank(data, rank)
    j["facts"][field] = {"value": value, "year": year, "source": source, "checked": date.today().isoformat()}
    return data


def _by_rank(data: dict, rank: int) -> dict:
    for j in data["journals"]:
        if j["rank"] == rank:
            return j
    raise SystemExit(f"No journal with rank {rank} in this list.")


def choose(data: dict, rank: int) -> dict:
    for j in data["journals"]:
        if j["status"] == "chosen":
            j["status"] = "suggested" if j["rank"] <= SHOWN else "reserve"
    _by_rank(data, rank)["status"] = "chosen"
    return data


def next_journal(data: dict) -> dict | None:
    """After a rejection: the chosen journal becomes rejected; returns the next one still open."""
    for j in data["journals"]:
        if j["status"] == "chosen":
            j["status"] = "rejected"
            j["rejected_on"] = date.today().isoformat()
    for j in data["journals"]:
        if j["status"] in ("suggested", "reserve"):
            return j
    return None


# ---------------------------------------------------------------- the table
def _fee(j: dict) -> str:
    a = j["access"]
    if a.startswith("Fully open"):
        return f"{j['fee_usd']:,} USD" if j.get("fee_usd") else ("none" if j.get("fee_usd") == 0 else "not found")
    if a.startswith("Hybrid"):
        return ("optional, " + f"{j['fee_usd']:,} USD") if j.get("fee_usd") else "optional, amount not found"
    return "none for subscription"


def _fact(j: dict, field: str) -> str:
    f = j.get("facts", {}).get(field)
    if not f:
        return "not found"
    return f"{f['value']}" + (f" ({f['year']})" if f.get("year") else "") + f" [source]({f['source']})"


def _quartile_text(f: dict) -> str:
    lo, hi = f.get("min_quartile"), f.get("max_quartile")
    if not (lo or hi):
        return ""
    rng = f"Q{(hi or 'Q1')[1]} to Q{(lo or 'Q4')[1]}"
    return f", SJR {rng} ({'any subject category' if f.get('quartile_basis') == 'any' else 'best quartile'})"


def table(data: dict, show_all: bool = False) -> str:
    p = data["profile"]
    L = [f"# Journals for {data['manuscript']}", "",
         f"Article type: **{p['article_type']}**" + (f" ({p['approach']})" if p.get("approach") else "") + f". Checked {data['checked']}. {data['method']}", ""]
    f = data["filters"]
    L.append(f"Your filters: access {f['access']}, maximum fee {f['max_fee_usd'] or 'none'}"
             f"{', MEDLINE only' if f['medline'] else ''}{', Scopus only' if f.get('scopus') else ''}"
             f"{_quartile_text(f)}, sorted by {f['prefer']}.")
    L.append("")
    rows = [j for j in data["journals"] if show_all or j["status"] in ("suggested", "chosen")]
    L += ["| # | Journal | Fit | Impact factor (as the journal states it) | Mean citations per paper, 2 years (OpenAlex) "
          "| Median days, submission to acceptance | Access | Fee | Indexed in | SJR quartile | Status |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for j in rows:
        rv = j.get("review", {})
        days = f"{rv['days']} ({rv['papers']} papers)" if rv.get("days") is not None else "not found"
        sj = j.get("scimago") or {}
        if sj.get("quartile"):
            q = f"{sj['quartile']} (SJR {sj.get('year', '')})"
        elif j.get("facts", {}).get("quartile"):
            q = _fact(j, "quartile")
        else:
            q = f"[check SCImago]({j['sjr_link']})" if j.get("sjr_link") else "not found"
        idx = ", ".join(x for x, ok in (("MEDLINE", j.get("medline")), ("Scopus", "scopus" in j.get("listed_in", [])),
                                         ("DOAJ", "doaj" in j.get("listed_in", []))) if ok) or "none found"
        L.append(f"| {j['rank']} | {j['name']} | {j['similar_papers']} similar papers | {_fact(j, 'impact_factor')} | "
                 f"{j.get('citations_2y_mean') if j.get('citations_2y_mean') is not None else 'not found'} | {days} | "
                 f"{j['access']} | {_fee(j)} | {idx} | {q} | {j['status']} |")
    L += ["", "## Why each journal fits", ""]
    for j in rows:
        L.append(f"**{j['rank']}. {j['name']}** ({j.get('publisher') or 'publisher not found'})")
        sq = j.get("facts", {}).get("scope_quote")
        L.append(f"- Aims and scope: \"{sq['value']}\" [source]({sq['source']})" if sq else "- Aims and scope: not read yet")
        at = j.get("facts", {}).get("accepts_type")
        if at:
            L.append(f"- Takes this article type: {at['value']} [source]({at['source']})")
        if (j.get("scimago") or {}).get("categories"):
            L.append(f"- SJR {j['scimago'].get('year', '')} subject categories: {j['scimago']['categories']}")
        if j.get("similar_examples"):
            L.append("- Similar papers it published: " + "; ".join(f"\"{t}\"" for t in j["similar_examples"][:2]))
        L.append("")
    reserve = sum(1 for j in data["journals"] if j["status"] == "reserve")
    if reserve and not show_all:
        L += [f"{reserve} more journals are kept in reserve for after a rejection.", ""]
    se = data.get("searched")
    if se:
        counts = "; ".join(f"{n} {why}" for why, n in se["left_out"].items())
        L += ["## Search", "",
              f"- Journals checked: {se['journals_checked']}. Passed your filters: {se['passed']}. "
              f"Left out: {sum(se['left_out'].values())}" + (f" ({counts})." if counts else "."), ""]
        if se["passed"] < SHOWN:
            L += [f"- Only {se['passed']} journal{'s' if se['passed'] != 1 else ''} passed. The filters that removed the most "
                  "are listed first above; loosening one of them gives a longer list.", ""]
    if data.get("left_out"):
        more = len(data["left_out"]) - 10
        L += ["## Left out", ""] + [f"- {x['name']}: {x['reason']}" for x in data["left_out"][:10]] + \
             ([f"- and {more} more (all are in the list file)"] if more > 0 else []) + [""]
    L += ["## Please double-check", "",
          "- The impact factor shown is what the journal's own website states. The official figure is in "
          "Clarivate's Journal Citation Reports (through your library). Mean citations per paper (OpenAlex) "
          "is a free measure, not the Impact Factor.",
          "- Median days from submission to acceptance include the authors' revision time.",
          "- MEDLINE is the indexed core of PubMed. A journal can be in PubMed (through PMC) without being in MEDLINE.",
          *([f"- Quartiles: SCImago, (n.d.). SJR, SCImago Journal & Country Rank [Portal], from the file you downloaded "
             f"({(data.get('lists') or {}).get('scimago', {}).get('file')}). A journal can be Q1 in one category and Q3 in another; "
             "the categories are listed above."] if (data.get("lists") or {}).get("scimago") else
            ["- No SCImago file was added, so quartiles are links. Add one with `journaldata.py add` (see BETA.md)."]),
          *([f"- Scopus status: Scopus Source List ({(data.get('lists') or {}).get('scopus', {}).get('edition')}), "
             "the file you downloaded from Elsevier."] if (data.get("lists") or {}).get("scopus") else []),
          "- Fees change; confirm on the journal's website before you submit.", ""]
    return "\n".join(L)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="SubmitReady journal finder")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("find")
    a.add_argument("manuscript", type=Path)
    a.add_argument("--out", type=Path, required=True)
    a.add_argument("--access", choices=["any", "open", "hybrid", "subscription"], default="any")
    a.add_argument("--max-fee", type=float)
    a.add_argument("--medline", action="store_true")
    a.add_argument("--prefer", choices=["fit", "speed", "impact"], default="fit")
    a.add_argument("--scopus", action="store_true", help="only journals active in Scopus (needs the Scopus Source List)")
    a.add_argument("--min-quartile", choices=["Q1", "Q2", "Q3", "Q4"], default="",
                   help="lowest quartile allowed, e.g. Q3 = Q1 to Q3 (needs the SCImago file)")
    a.add_argument("--max-quartile", choices=["Q1", "Q2", "Q3", "Q4"], default="",
                   help="highest quartile allowed, e.g. Q2 = no Q1 journals")
    a.add_argument("--quartile-basis", choices=["best", "any"], default="best",
                   help="best = the journal's best quartile; any = a quartile in any of its subject categories")
    a.add_argument("--email", default="")
    b = sub.add_parser("add")
    b.add_argument("list", type=Path)
    b.add_argument("rank", type=int)
    b.add_argument("--field", required=True)
    b.add_argument("--value", required=True)
    b.add_argument("--source", required=True)
    b.add_argument("--year", default="")
    for name in ("table", "next"):
        c = sub.add_parser(name)
        c.add_argument("list", type=Path)
        if name == "table":
            c.add_argument("--all", action="store_true")
    d = sub.add_parser("choose")
    d.add_argument("list", type=Path)
    d.add_argument("rank", type=int)
    args = ap.parse_args(argv)

    if args.cmd == "find":
        data = find(args.manuscript, Net(args.email), args.access, args.max_fee, args.medline, args.prefer,
                    scopus=args.scopus, min_quartile=args.min_quartile, max_quartile=args.max_quartile,
                    quartile_basis=args.quartile_basis)
        args.out.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(table(data))
        return 0
    data = json.loads(args.list.read_text(encoding="utf-8"))
    if args.cmd == "add":
        add_fact(data, args.rank, args.field, args.value, args.source, args.year)
    elif args.cmd == "choose":
        choose(data, args.rank)
    elif args.cmd == "next":
        j = next_journal(data)
        args.list.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps({"next": j and {k: j[k] for k in ("rank", "name", "access", "status")}}, ensure_ascii=False))
        return 0 if j else 1
    elif args.cmd == "table":
        print(table(data, args.all))
        return 0
    args.list.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(table(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
