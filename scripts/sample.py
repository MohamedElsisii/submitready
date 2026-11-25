#!/usr/bin/env python3
"""Get a free sample manuscript to try SubmitReady without risking your own work.

Downloads an openly licensed (CC BY 4.0) preprint from Europe PMC and builds a Word
manuscript from it, with live Zotero-style citations and a reference list, in a new folder.

    python sample.py                       # the default sample, into ./submitready_sample/
    python sample.py --out my_folder
    python sample.py --id PPR1217902 --mode endnote

Modes: zotero (default), endnote, numbered, authoryear (the last two are typed citations).

The sample is made on your computer. SubmitReady's repository never stores manuscripts.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# A CC BY 4.0 preprint with 43 references, used because its licence allows reuse.
DEFAULT_ID = "PPR1217902"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/{id}/fullTextXML"
CC_BY = "creativecommons.org/licenses/by/"


def licence(xml: bytes) -> str:
    from lxml import etree
    root = etree.fromstring(xml)
    lic = root.find(".//license")
    if lic is None:
        return ""
    import re
    m = re.search(rb"https?://creativecommons\.org/[\w./-]+", etree.tostring(lic))
    return m.group(0).decode() if m else " ".join(" ".join(lic.itertext()).split())


def make(article_id: str, out: Path, mode: str = "zotero", fetch=None) -> dict:
    import requests
    import jats_to_docx as jd

    fetch = fetch or (lambda url: requests.get(url, timeout=60, headers={"User-Agent": "SubmitReady sample"}))
    r = fetch(EPMC.format(id=article_id))
    if getattr(r, "status_code", 200) != 200 or not r.content.strip().startswith(b"<"):
        raise SystemExit(f"Europe PMC did not return the article {article_id} (HTTP {getattr(r, 'status_code', '?')}).")
    lic = licence(r.content)
    if CC_BY not in lic:
        raise SystemExit(f"{article_id} is not under a CC BY licence ({lic or 'no licence found'}); not used.")
    out.mkdir(parents=True, exist_ok=True)
    xml = out / f"{article_id}.xml"
    xml.write_bytes(r.content)
    docx = out / "sample_manuscript.docx"
    info = jd.build(xml, docx, mode)
    from lxml import etree
    root = etree.fromstring(r.content)
    title = " ".join(root.find(".//article-title").itertext()).strip() if root.find(".//article-title") is not None else ""
    doi = (root.find(".//article-id[@pub-id-type='doi']").text or "") if root.find(".//article-id[@pub-id-type='doi']") is not None else ""
    (out / "ABOUT_THIS_SAMPLE.txt").write_text(
        f"Sample manuscript for trying SubmitReady.\n\n"
        f"Built from: {title}\nDOI: https://doi.org/{doi}\nEurope PMC: {article_id}\nLicence: {lic}\n\n"
        "The text is the authors' work, reused under its CC BY licence. Cite the original if you share anything "
        "made from it. The citations in the Word file were rebuilt by SubmitReady for testing, so they are not the "
        "authors' own reference-manager fields.\n", encoding="utf-8")
    return {"docx": str(docx), "title": title, "doi": doi, "licence": lic, **(info or {})}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Make a free sample manuscript to try SubmitReady")
    ap.add_argument("--id", default=DEFAULT_ID, help="Europe PMC id of a CC BY article (default: %(default)s)")
    ap.add_argument("--out", type=Path, default=Path("submitready_sample"))
    ap.add_argument("--mode", choices=["zotero", "endnote", "numbered", "authoryear"], default="zotero")
    a = ap.parse_args(argv)
    r = make(a.id, a.out, a.mode)
    print(f"Sample ready: {r['docx']}\n{r['title']}\nLicence: {r['licence']}")
    print(f"Try:  /submitready:submit {r['docx']} PLOS ONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
