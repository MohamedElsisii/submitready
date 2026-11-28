#!/usr/bin/env python3
"""How much of each saved journal profile SubmitReady measures, and how much is left to "check yourself".

    python scripts/coverage.py                 # print the table
    python scripts/coverage.py --out COVERAGE.md

A rule is "measured" when its check type is not "manual": the scripts count it (words, sections,
captions, files ...). A "manual" rule is shown to the author as a check-yourself item, with its quote.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQ = ROOT / "journals" / "requirements"


def rows() -> list[dict]:
    out = []
    for p in sorted(REQ.glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        reqs = d.get("requirements", [])
        manual = [r for r in reqs if r.get("check", {}).get("type", "manual") == "manual"]
        out.append({"snapshot": p.name, "journal": d.get("journal", ""), "rules": len(reqs),
                    "measured": len(reqs) - len(manual), "manual": len(manual),
                    "manual_rules": [r.get("rule", "") for r in manual],
                    "types": Counter(r.get("check", {}).get("type", "manual") for r in reqs)})
    return out


def markdown(rs: list[dict]) -> str:
    total = sum(r["rules"] for r in rs)
    meas = sum(r["measured"] for r in rs)
    types = sum((r["types"] for r in rs), Counter())
    lines = [f"# What SubmitReady measures ({date.today().isoformat()})", "",
             f"{len(rs)} saved snapshots, {total} rules. **{meas} measured** ({round(100 * meas / max(total, 1))}%), "
             f"{total - meas} left for the author to check, each with the guideline's own words.", "",
             "Measured means a script counts or checks it (words, sections and their order, captions, figure and "
             "supplementary files, reference style ...). Rules about judgment (title page content, ethics wording, "
             "reporting guidelines) stay with the author on purpose.", "",
             "| Snapshot | Rules | Measured | Check yourself |", "| --- | --- | --- | --- |"]
    for r in sorted(rs, key=lambda r: (-r["measured"] / max(r["rules"], 1), r["snapshot"])):
        pct = round(100 * r["measured"] / max(r["rules"], 1))
        lines.append(f"| {r['snapshot']} | {r['rules']} | {r['measured']} ({pct}%) | {r['manual']} |")
    lines += ["", "## Check types in use", "", "| Type | Rules |", "| --- | --- |"]
    lines += [f"| {t} | {n} |" for t, n in types.most_common()]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    md = markdown(rows())
    if a.out:
        a.out.write_text(md, encoding="utf-8")
        print(f"Wrote {a.out}")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
