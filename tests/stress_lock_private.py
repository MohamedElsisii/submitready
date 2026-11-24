"""Stress test the text lock on real manuscripts in tests/private/ (never committed).

Usage: python tests/stress_lock_private.py [N_EDITS_PER_FILE]

For each .docx in tests/private/:
  * N random single-character edits in locked text  -> each must be caught
  * N random edits inside live citation markers     -> each must pass (allowed)
  * N random edits inside the bibliography          -> each must pass (left to /refs)
  * a re-save through python-docx                   -> must pass
"""
from __future__ import annotations

import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import lock  # noqa: E402
from docx_tools import plant_edit, read_doc, text_nodes  # noqa: E402


def run(n: int) -> int:
    private = ROOT / "tests" / "private"
    files = sorted(p for p in list(private.glob("*.docx")) + list(private.glob("preprints/*.docx"))
                   if not p.name.startswith("~$"))
    if not files:
        print("No manuscripts in tests/private/. Nothing to do.")
        return 0
    failures = 0
    for src in files:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            lk = td / "m.lock.json"
            info = lock.create(src, lk)
            root = read_doc(src)
            have_cite = bool(text_nodes(root, "citation"))
            have_bib = bool(text_nodes(root, "bibliography"))
            caught = 0
            for i in range(n):
                out = td / f"locked_{i}.docx"
                before, after = plant_edit(src, out, random.Random(1000 + i), "locked")
                if lock.verify(lk, out)["status"] == 2:
                    caught += 1
                else:
                    failures += 1
                    print(f"  MISSED: {before!r} -> {after!r}")
            allowed_ok = 0
            allowed_total = 0
            for where, ok in (("citation", have_cite), ("bibliography", have_bib)):
                if not ok:
                    continue
                for i in range(n):
                    out = td / f"{where}_{i}.docx"
                    plant_edit(src, out, random.Random(2000 + i), where)
                    allowed_total += 1
                    if lock.verify(lk, out)["status"] == 0:
                        allowed_ok += 1
                    else:
                        failures += 1
                        print(f"  FALSE ALARM in {where} edit {i}")
            import docx

            resaved = td / "resaved.docx"
            docx.Document(src).save(resaved)
            resave_ok = lock.verify(lk, resaved)["status"] == 0
            failures += 0 if resave_ok else 1
            print(
                f"{src.name}: {info['sentences']} sentences, {info['citations']} citations | "
                f"planted edits caught {caught}/{n} | allowed edits passed {allowed_ok}/{allowed_total} | "
                f"re-save {'PASS' if resave_ok else 'FAIL'}"
            )
    print("ALL GOOD" if failures == 0 else f"{failures} FAILURES")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run(int(sys.argv[1]) if len(sys.argv) > 1 else 50))
