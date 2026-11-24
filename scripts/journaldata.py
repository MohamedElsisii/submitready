#!/usr/bin/env python3
"""Journal lists you download yourself, read on your own computer.

SubmitReady never ships these files. You download them from their owners, and this
script builds a small index from them in your SubmitReady data folder
(~/.submitready/data, or the folder in the SUBMITREADY_DATA environment variable).

- SCImago Journal & Country Rank: the "Download data" button on scimagojr.com's journal
  rankings page (a .csv). Gives each journal's SJR, best quartile and subject categories.
  Please cite: SCImago, (n.d.). SJR, SCImago Journal & Country Rank [Portal]. https://www.scimagojr.com
- Scopus Source List: the free Excel download on Elsevier's Scopus content pages. Gives each
  title's Scopus status, including titles Scopus discontinued (a quality warning).

    python journaldata.py add scimagojr_2025.csv ext_list_Oct_2026.xlsx
    python journaldata.py status
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from datetime import date
from pathlib import Path


def data_dir() -> Path:
    return Path(os.environ.get("SUBMITREADY_DATA") or Path.home() / ".submitready" / "data")


def norm_issn(s) -> str:
    d = re.sub(r"[^0-9Xx]", "", str(s or "")).upper()
    return d.zfill(8) if d and len(d) <= 8 else ""


def _num(s: str):
    try:
        return float(str(s).replace(",", "."))
    except ValueError:
        return None


# ---------------------------------------------------------------- SCImago
def parse_scimago(path: Path) -> dict:
    raw = path.read_bytes().decode("utf-8-sig", errors="replace")
    rows = csv.reader(io.StringIO(raw), delimiter=";")
    hdr = next(rows)
    col = {h: i for i, h in enumerate(hdr)}
    m = re.search(r"(19|20)\d{2}", path.name) or re.search(r"Total Docs\. \(((?:19|20)\d{2})\)", ";".join(hdr))
    year = m.group(0) if m and m.group(0).isdigit() else (m.group(1) if m else "")
    out = {}
    for r in rows:
        if len(r) < len(hdr) - 2:
            continue
        rec = {"title": r[col["Title"]], "type": r[col["Type"]], "sjr": _num(r[col["SJR"]]),
               "quartile": r[col["SJR Best Quartile"]] if r[col["SJR Best Quartile"]] in ("Q1", "Q2", "Q3", "Q4") else "",
               "categories": r[col["Categories"]], "sourceid": r[col["Sourceid"]]}
        for issn in r[col["Issn"]].split(","):
            k = norm_issn(issn)
            if k and k != "00000000":
                out[k] = rec
    return {"source": "SCImago Journal & Country Rank", "file": path.name, "year": year,
            "added": date.today().isoformat(), "journals": out}


# ---------------------------------------------------------------- Scopus
def parse_scopus(path: Path) -> dict:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    hdr = [str(h or "").split("\n")[0].split(" (")[0].strip() for h in next(rows)]
    col = {h: i for i, h in enumerate(hdr)}
    out = {}
    for r in rows:
        rec = {"title": r[col["Source Title"]], "status": r[col["Active or Inactive"]] or "",
               "discontinued": bool(r[col["Titles Discontinued by Scopus"]]),
               "medline": bool(r[col["Medline-sourced Title?"]]), "type": r[col["Source Type"]] or ""}
        for k in (norm_issn(r[col["ISSN"]]), norm_issn(r[col["EISSN"]])):
            if k:
                out[k] = rec
    # the recently discontinued sheet adds the year and why
    for ws in wb.worksheets[1:]:
        if not ws.title.lower().startswith("discontinued"):
            continue
        for r in ws.iter_rows(values_only=True):
            if not r or not r[2] or not str(r[0]).isdigit():
                continue
            for k in (norm_issn(r[2]), norm_issn(r[3])):
                if k:
                    rec = out.setdefault(k, {"title": r[1], "status": "Inactive", "medline": False, "type": "Journal"})
                    rec["discontinued"] = True
                    rec["discontinued_year"] = r[6]
    m = re.search(r"([A-Za-z]{3,9})[._ -]?((?:19|20)\d{2})", path.name)
    return {"source": "Scopus Source List", "file": path.name, "edition": f"{m.group(1)} {m.group(2)}" if m else "",
            "added": date.today().isoformat(), "journals": out}


# ---------------------------------------------------------------- index on disk
def add(files: list[Path]) -> list[str]:
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    said = []
    for f in files:
        if f.suffix.lower() == ".csv":
            idx = parse_scimago(f)
            (d / "scimago.json").write_text(json.dumps(idx), encoding="utf-8")
            said.append(f"SCImago {idx['year']}: {len(set(map(id, idx['journals'].values()))):,} journals")
        elif f.suffix.lower() in (".xlsx", ".xlsm"):
            idx = parse_scopus(f)
            (d / "scopus.json").write_text(json.dumps(idx), encoding="utf-8")
            n_disc = sum(1 for v in {id(v): v for v in idx["journals"].values()}.values() if v.get("discontinued"))
            said.append(f"Scopus Source List {idx['edition']}: {len({id(v) for v in idx['journals'].values()}):,} titles, "
                        f"{n_disc:,} discontinued by Scopus")
        else:
            said.append(f"{f.name}: not a SCImago .csv or a Scopus .xlsx; skipped")
    return said


_cache: dict = {}


def load(name: str) -> dict | None:
    if name in _cache:
        return _cache[name]
    p = data_dir() / f"{name}.json"
    _cache[name] = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    return _cache[name]


def lookup(issns: list[str]) -> dict:
    out = {}
    for name in ("scimago", "scopus"):
        idx = load(name)
        if not idx:
            continue
        for i in issns:
            rec = idx["journals"].get(norm_issn(i))
            if rec:
                out[name] = dict(rec, file=idx["file"], year=idx.get("year") or idx.get("edition", ""))
                break
    return out


def status() -> str:
    lines = [f"SubmitReady data folder: {data_dir()}"]
    for name, label, where in (("scimago", "SCImago quartiles", "scimagojr.com > Journal Rankings > Download data"),
                               ("scopus", "Scopus Source List", "Elsevier's Scopus content page > Download the source list")):
        idx = load(name)
        if idx:
            age = (date.today() - date.fromisoformat(idx["added"])).days
            lines.append(f"- {label}: {idx['file']} ({idx.get('year') or idx.get('edition')}), added {idx['added']}"
                         + (" (over a year old: download the new one)" if age > 400 else ""))
        else:
            lines.append(f"- {label}: not added. Optional: {where}, then `journaldata.py add <file>`")
    return "\n".join(lines)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="SubmitReady journal lists you downloaded")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("files", type=Path, nargs="+")
    sub.add_parser("status")
    args = ap.parse_args(argv)
    if args.cmd == "add":
        for line in add(args.files):
            print(line)
    print(status())
    return 0


if __name__ == "__main__":
    sys.exit(main())
