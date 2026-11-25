#!/usr/bin/env python3
"""Are the saved journal guidelines still current?

Every rule in journals/requirements/*.json quotes the guideline page it came from.
This script reads each page again and checks that every quote is still there.

    python freshness.py                     # check everything, print a report
    python freshness.py --only springer     # snapshots whose file name contains "springer"
    python freshness.py --report freshness.md --json freshness.json
    python freshness.py --cache pages/      # use saved page text where a site blocks scripts
    python freshness.py --key <url>         # the cache file name for one page

For each page you get one of:
  ok           every quote from this page is still on it
  changed      at least one quote is gone: the rule may have changed, read the page
  unreachable  the page did not load, or a bot check answered instead
  pdf          a PDF this computer cannot read; save its text into the cache (see below)
  unclear      the page loaded but none of its quotes matched: usually a partial page, so check by hand

Some publishers block scripts. Then open the page yourself (or let Claude read it), save its
text as <cache>/<key>.txt, where <key> is printed in the report, and run again with --cache.

The script only reads. It never edits a snapshot: when a rule changed, a person checks the
page and writes a new dated snapshot.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

import yaml

ROOT = Path(__file__).resolve().parents[1]
REQ = ROOT / "journals" / "requirements"
FAM = ROOT / "journals" / "families"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 SubmitReady-freshness")
STALE_DAYS = 180
BOT_WALL = re.compile(r"(just a moment|verify you are human|captcha|access denied|enable javascript and cookies|"
                      r"attention required|request unsuccessful|are you a robot|client challenge)", re.I)


# ---------------------------------------------------------------- text
def norm(s: str) -> str:
    """Text for comparing: same letters and words, no layout."""
    s = html.unescape(s)
    s = s.replace(" ", " ").replace("­", "")
    s = re.sub(r"[‘’‚‛′`]", "'", s)
    s = re.sub(r"[“”„″]", '"', s)
    s = re.sub(r"[‐-―−]", "-", s)
    s = s.replace("…", "...")
    s = re.sub(r"\]\([^)]*\)", "", s)                 # markdown links: keep the words
    s = re.sub(r"</?[a-zA-Z][^<>]{0,40}>", "", s)     # stray tags such as <u> or <br>
    s = re.sub(r"[\\*_]", "", s)                       # markdown emphasis and escapes
    s = re.sub(r"[\[\]#>|]", " ", s)                  # other markdown marks
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r" ([.,;:!?)])", r"\1", s)             # no space before punctuation
    return s.strip().lower()


def quote_key(q: str) -> str:
    """A quote often ends where the sentence goes on: drop its own final punctuation."""
    return norm(q).rstrip(" .,;:")


def html_text(raw: str) -> str:
    try:
        import lxml.html
        doc = lxml.html.fromstring(raw)
        for bad in doc.xpath("//script|//style|//noscript"):
            bad.drop_tree()
        for el in doc.iter("p", "li", "div", "br", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
                           "section", "article", "dt", "dd", "blockquote", "pre", "table", "ul", "ol"):
            el.tail = "\n" + (el.tail or "")              # keep block boundaries as line breaks
        return doc.text_content()
    except Exception:
        return re.sub(r"<[^>]+>", " ", raw)


def pdf_text(data: bytes) -> str | None:
    try:
        import pypdf  # optional
        from io import BytesIO
        return "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(BytesIO(data)).pages)
    except Exception:
        pass
    if shutil.which("pdftotext"):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.pdf"
            p.write_bytes(data)
            r = subprocess.run(["pdftotext", "-layout", str(p), "-"], capture_output=True, text=True)
            if r.returncode == 0:
                return r.stdout
    return None


def key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- fetching
@dataclass
class Page:
    url: str
    status: str            # ok-text | unreachable | pdf
    text: str = ""
    note: str = ""


def fetch(url: str, timeout: int = 30) -> Page:
    import requests
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en"}, timeout=timeout)
    except Exception as e:
        return Page(url, "unreachable", note=f"{type(e).__name__}")
    if r.status_code >= 400:
        return Page(url, "unreachable", note=f"HTTP {r.status_code}")
    ctype = r.headers.get("content-type", "")
    if "pdf" in ctype or url.lower().endswith(".pdf") or r.content[:4] == b"%PDF":
        t = pdf_text(r.content)
        return Page(url, "ok-text", t) if t else Page(url, "pdf", note="no PDF reader here")
    text = html_text(r.text)
    if len(norm(text)) < 1500 and BOT_WALL.search(text):
        return Page(url, "unreachable", note="a bot check answered instead of the page")
    return Page(url, "ok-text", text)


def get_page(url: str, cache: Path | None, fetcher: Callable[[str], Page], save: Path | None) -> Page:
    if cache:
        f = cache / f"{key(url)}.txt"
        if f.exists():
            return Page(url, "ok-text", f.read_text(encoding="utf-8"), note="from cache")
    p = fetcher(url)
    if save and p.status == "ok-text":
        save.mkdir(parents=True, exist_ok=True)
        (save / f"{key(url)}.txt").write_text(p.text, encoding="utf-8")
    return p


# ---------------------------------------------------------------- checking
@dataclass
class SourceResult:
    snapshot: str
    name: str
    url: str
    checked: str
    status: str                          # ok | changed | unreachable | pdf | no-quotes
    note: str = ""
    missing: list[dict] = field(default_factory=list)
    found: int = 0
    total: int = 0
    days_old: int | None = None


def _days(d: str) -> int | None:
    try:
        return (date.today() - date.fromisoformat(str(d))).days
    except Exception:
        return None


def snapshots(only: str = "") -> list[Path]:
    files = sorted(Path(p) for p in glob.glob(str(REQ / "*.json")))
    return [f for f in files if only.lower() in f.name.lower()]


def check_snapshot(path: Path, get: Callable[[str], Page]) -> list[SourceResult]:
    d = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for i, src in enumerate(d.get("sources", [])):
        quotes = [r for r in d.get("requirements", []) if r.get("quote") and int(r.get("source", 0)) == i]
        res = SourceResult(path.name, src.get("name", ""), src.get("url", ""), str(src.get("checked", "")),
                           "no-quotes", total=len(quotes), days_old=_days(src.get("checked", "")))
        if not quotes or not res.url:
            out.append(res)
            continue
        page = get(res.url)
        res.note = page.note
        if page.status != "ok-text":
            res.status = page.status
            out.append(res)
            continue
        body = norm(page.text)
        for r in quotes:
            if quote_key(r["quote"]) in body:
                res.found += 1
            else:
                res.missing.append({"id": r.get("id", ""), "quote": r["quote"]})
        if res.missing and (res.found == 0 and (res.total >= 3 or len(body) < 3000)):
            # Nothing at all matched: much more likely a partial page or a bot page than a full rewrite.
            res.status, res.note = "unclear", "none of the quotes were found; the page may not have loaded fully"
            res.missing = []
        else:
            res.status = "changed" if res.missing else "ok"
        out.append(res)
    return out


def family_ages() -> list[dict]:
    rows = []
    for p in sorted(FAM.glob("*.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        g = d.get("guidelines") or {}
        rows.append({"family": p.stem, "url": g.get("url", ""), "checked": str(g.get("checked", "")),
                     "days_old": _days(g.get("checked", ""))})
    return rows


def run(only: str = "", cache: Path | None = None, save: Path | None = None,
        fetcher: Callable[[str], Page] = fetch) -> dict:
    memo: dict[str, Page] = {}

    def get(url: str) -> Page:
        if url not in memo:
            memo[url] = get_page(url, cache, fetcher, save)
        return memo[url]

    results = [r for f in snapshots(only) for r in check_snapshot(f, get)]
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return {"date": date.today().isoformat(), "counts": counts,
            "results": [r.__dict__ for r in results], "families": family_ages()}


# ---------------------------------------------------------------- report
LABEL = {"ok": "ok", "changed": "CHANGED", "unreachable": "not checked (page did not load)",
         "pdf": "not checked (PDF)", "unclear": "not checked (page incomplete)", "no-quotes": "no quotes to check"}


def report_markdown(rep: dict, stale_days: int = STALE_DAYS) -> str:
    c = rep["counts"]
    res = rep["results"]
    lines = [f"# Guidelines freshness check, {rep['date']}", "",
             f"{c.get('ok', 0)} pages ok, {c.get('changed', 0)} changed, "
             f"{c.get('unreachable', 0) + c.get('pdf', 0) + c.get('unclear', 0)} not checked.", ""]
    changed = [r for r in res if r["status"] == "changed"]
    if changed:
        lines += ["## Changed: read these pages", "",
                  "A quoted rule is no longer on the page. The rule may have changed, or the page was reworded. "
                  "Check the page, then write a new dated snapshot if the rule changed.", ""]
        for r in changed:
            lines.append(f"### {r['name']} ({r['snapshot']})")
            lines.append(f"{r['url']}  ")
            lines.append(f"{r['found']} of {r['total']} quotes still found. Missing:")
            for m in r["missing"]:
                lines.append(f"- `{m['id']}`: \"{m['quote']}\"")
            lines.append("")
    blocked = [r for r in res if r["status"] in ("unreachable", "pdf", "unclear")]
    if blocked:
        lines += ["## Not checked", "",
                  "The page did not load for the script. Open it yourself and save its text as "
                  "`<cache>/<key>.txt`, then run again with `--cache <cache>`.", "",
                  "| Snapshot | Page | Why | Key |", "| --- | --- | --- | --- |"]
        for r in blocked:
            lines.append(f"| {r['snapshot']} | {r['url']} | {r['note'] or r['status']} | {key(r['url'])} |")
        lines.append("")
    old = [r for r in res if r["days_old"] is not None and r["days_old"] > stale_days]
    old_f = [f for f in rep["families"] if f["days_old"] is not None and f["days_old"] > stale_days]
    if old or old_f:
        lines += [f"## Checked more than {stale_days} days ago", ""]
        lines += [f"- {r['snapshot']}: {r['url']} (checked {r['checked']})" for r in old]
        lines += [f"- family {f['family']}: {f['url']} (checked {f['checked']})" for f in old_f]
        lines.append("")
    lines += ["## All pages", "", "| Snapshot | Page | Result | Quotes found |", "| --- | --- | --- | --- |"]
    for r in res:
        q = f"{r['found']}/{r['total']}" if r["status"] in ("ok", "changed") else "-"
        lines.append(f"| {r['snapshot']} | {r['url']} | {LABEL.get(r['status'], r['status'])} | {q} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Check that saved journal guidelines are still current")
    ap.add_argument("--only", default="", help="only snapshots whose file name contains this text")
    ap.add_argument("--cache", type=Path, help="folder of saved page text (<key>.txt) to use instead of fetching")
    ap.add_argument("--save", type=Path, help="save the text of every page that loaded into this folder")
    ap.add_argument("--report", type=Path, help="write the Markdown report here")
    ap.add_argument("--json", dest="json_out", type=Path, help="write the full result as JSON here")
    ap.add_argument("--stale-days", type=int, default=STALE_DAYS)
    ap.add_argument("--fail-on-change", action="store_true", help="exit with 1 when any quote is missing")
    ap.add_argument("--key", metavar="URL", help="print the cache file name for a page and stop")
    a = ap.parse_args(argv)
    if a.key:
        print(f"{key(a.key)}.txt")
        return 0
    rep = run(a.only, a.cache, a.save)
    md = report_markdown(rep, a.stale_days)
    if a.report:
        a.report.write_text(md, encoding="utf-8")
    if a.json_out:
        a.json_out.write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
    if not a.report:
        sys.stdout.write(md)
    else:
        c = rep["counts"]
        print(f"{c.get('ok', 0)} ok, {c.get('changed', 0)} changed, "
              f"{c.get('unreachable', 0) + c.get('pdf', 0) + c.get('unclear', 0)} not checked. Report: {a.report}")
    return 1 if a.fail_on_change and rep["counts"].get("changed") else 0


if __name__ == "__main__":
    sys.exit(main())
