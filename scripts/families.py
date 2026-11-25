#!/usr/bin/env python3
"""Which checked starting point fits a journal: its own profile, a family, or none.

    python families.py find "Future Oncology"
    python families.py find "Cancers" --publisher MDPI

A journal profile (journals/<slug>.yaml) wins. Then a family that lists the journal by name, then one whose name pattern fits (e.g. "BMC ...").
Then a family that matches the publisher. Every journal still works without any of these:
the prepare step reads the journal's live guidelines in every case.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
JOURNALS = ROOT / "journals"


def _norm(s: str) -> str:
    s = s.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def profiles() -> list[dict]:
    out = []
    for p in sorted(JOURNALS.glob("*.yaml")):
        if p.name.startswith("_"):
            continue
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        d["_file"] = p.relative_to(ROOT).as_posix()
        out.append(d)
    return out


def families() -> list[dict]:
    out = []
    for p in sorted((JOURNALS / "families").glob("*.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        d["_file"] = p.relative_to(ROOT).as_posix()
        out.append(d)
    return out


def find(journal: str, publisher: str = "") -> dict:
    j = _norm(journal)
    for p in profiles():
        if _norm(p.get("name", "")) == j:
            return {"kind": "profile", "file": p["_file"], "name": p["name"],
                    "family": p.get("family", ""), "snapshot": p.get("requirements_snapshot", [])}
    fams = families()
    for f in fams:
        if any(_norm(x) == j for x in f.get("journals", [])):
            return {"kind": "family", "how": "listed by name", "file": f["_file"], "name": f["name"],
                    "snapshot": f["requirements_snapshot"], "journal_specific": f.get("journal_specific", [])}
    pub = _norm(publisher)

    def pub_fits(f, with_parents: bool = False) -> bool:
        m = f.get("match") or {}
        names = m.get("publisher", []) + (m.get("parent_publishers", []) if with_parents else [])
        return any(n and re.search(rf"\b{re.escape(n)}\b", pub) for n in map(_norm, names))

    for f in fams:
        pat = (f.get("match") or {}).get("journal_name_pattern", "")
        # A name pattern counts only if the publisher, when given, also fits the family or its
        # parent company (listed in parent_publishers, which never select a family on their own).
        if pat and re.search(pat, journal.strip(), re.I) and (not pub or pub_fits(f, with_parents=True)):
            return {"kind": "family", "how": "matched by journal name", "file": f["_file"], "name": f["name"],
                    "snapshot": f["requirements_snapshot"], "journal_specific": f.get("journal_specific", []),
                    "journal_page": (f.get("match") or {}).get("journal_page", "")}
    if pub:
        for f in fams:
            if pub_fits(f):
                excluded = (f.get("match") or {}).get("not_in_family", "")
                if excluded:
                    other = yaml.safe_load((ROOT / excluded).read_text(encoding="utf-8")) or {}
                    if any(_norm(x) == j for x in other.get("journals", [])):
                        continue
                return {"kind": "family", "how": "matched by publisher", "file": f["_file"], "name": f["name"],
                        "snapshot": f["requirements_snapshot"], "journal_specific": f.get("journal_specific", []),
                        "journal_page": (f.get("match") or {}).get("journal_page", "")}
    return {"kind": "none", "note": "No checked starting point; the live guidelines are read and used directly."}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Find the profile or family for a journal")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("find")
    f.add_argument("journal")
    f.add_argument("--publisher", default="")
    args = ap.parse_args(argv)
    print(json.dumps(find(args.journal, args.publisher), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
