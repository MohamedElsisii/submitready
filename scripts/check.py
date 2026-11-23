#!/usr/bin/env python3
"""SubmitReady check: the final pre-submission audit, and the submission package.

Runs everything on the formatted manuscript and writes one folder:

  submission_package/
    SUMMARY.md            verdict: READY or NOT READY, with every blocking item
    <manuscript>.docx     the formatted manuscript (copied, never edited here)
    figures/              figure files, checked; safe conversions only (JPEG->TIFF, same pixels)
    supplementary/        supplementary files, copied as they are
    reports/              change_report.md, checklist.md, references.md, figures.md, crossrefs.md

Usage:
  python scripts/check.py formatted.docx --lock original.lock.json --requirements req.json \
      [--figures DIR] [--supp DIR] [--library refs.ris] [--online] [--email you@uni.edu] \
      --out submission_package
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import zipfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lock as lockmod  # noqa: E402
import prepare  # noqa: E402
import refs as refsmod  # noqa: E402

IMG_EXT = {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".eps", ".pdf", ".bmp", ".gif"}
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


# ---------------------------------------------------------------- figures

def _copy(src: Path, dst: Path) -> None:
    """Copy a file, skipping it when source and destination are the same file (input already in the package)."""
    src, dst = Path(src), Path(dst)
    if dst.exists() and src.resolve() == dst.resolve():
        return
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists() and src.resolve() == dst.resolve():
        return
    shutil.copy(src, dst)

def extract_embedded_figures(docx: Path, dest: Path) -> list[Path]:
    """Copy images embedded in the manuscript out, in order of appearance (the .docx is not changed)."""
    from lxml import etree

    out = []
    with zipfile.ZipFile(docx) as z:
        rels = etree.fromstring(z.read("word/_rels/document.xml.rels"))
        target = {r.get("Id"): r.get("Target") for r in rels}
        doc = z.read("word/document.xml").decode("utf-8")
        seen = []
        for rid in re.findall(r'r:embed="([^"]+)"', doc):
            if rid not in seen and rid in target:
                seen.append(rid)
        dest.mkdir(parents=True, exist_ok=True)
        for n, rid in enumerate(seen, 1):
            name = "word/" + target[rid].lstrip("/").replace("word/", "")
            if name not in z.namelist():
                continue
            p = dest / f"Figure_{n}{Path(name).suffix.lower()}"
            p.write_bytes(z.read(name))
            out.append(p)
    return out


def _fig_number(p: Path) -> int | None:
    m = re.search(r"(?:fig(?:ure)?)[ _-]*(\d+)", p.stem, re.I)
    return int(m.group(1)) if m else None


def check_figures(files: list[Path], rule: dict | None, dest: Path) -> tuple[list[dict], list[str]]:
    """Measure each figure file; convert JPEG/PNG to LZW TIFF only when the journal requires TIFF."""
    from PIL import Image

    dest.mkdir(parents=True, exist_ok=True)
    rows, actions = [], []
    for f in sorted(files, key=lambda p: (_fig_number(p) or 999, p.name)):
        n = _fig_number(f)
        row = {"file": f.name, "number": n, "problems": []}
        if f.suffix.lower() in (".pdf", ".eps", ".svg"):
            rows.append(_vector_figure(f, n, rule, dest, actions))
            continue
        try:
            im = Image.open(f)
            w, h = im.size
            dpi = im.info.get("dpi", (None, None))[0]
            dpi = round(float(dpi)) if dpi else None
            row.update({"format": (im.format or f.suffix[1:]).upper(), "mode": im.mode, "px": f"{w}x{h}", "dpi": dpi,
                        "width_mm": round(w / dpi * 25.4) if dpi else None, "mb": round(f.stat().st_size / 1e6, 2)})
        except Exception as e:
            row["problems"].append(f"cannot open ({e})")
            rows.append(row)
            _copy(f, dest / f.name)
            continue
        out_name = f.name
        if rule:
            fmts = [x.lower() for x in rule.get("formats", [])]
            ext = f.suffix.lower().lstrip(".")
            if rule.get("dpi_at_final_size") and rule.get("dpi_min"):
                # Resolution counts at the printed size: how wide can this image print at the minimum dpi?
                printable_mm = round(w / rule["dpi_min"] * 25.4)
                row["printable_mm"] = printable_mm
                cols = sorted(rule.get("column_widths_mm", []))
                if cols and printable_mm < cols[0]:
                    row["problems"].append(f"{w} px wide: too small even for one column ({cols[0]} mm needs "
                                           f"{round(cols[0] / 25.4 * rule['dpi_min'])} px at {rule['dpi_min']} dpi)")
                elif cols and printable_mm < cols[-1]:
                    row.setdefault("notes", []).append(
                        f"{w} px wide: fine for one column ({cols[0]} mm); for full width ({cols[-1]} mm) it needs "
                        f"{round(cols[-1] / 25.4 * rule['dpi_min'])} px at {rule['dpi_min']} dpi")
            elif dpi is None:
                row["problems"].append("no resolution (dpi) stored in the file")
            else:
                if rule.get("dpi_min") and dpi < rule["dpi_min"]:
                    row["problems"].append(f"{dpi} dpi is below {rule['dpi_min']}")
                if rule.get("dpi_max") and dpi > rule["dpi_max"]:
                    row["problems"].append(f"{dpi} dpi is above {rule['dpi_max']}")
            if rule.get("min_width_px") and w < rule["min_width_px"]:
                row["problems"].append(f"{w} px wide, minimum {rule['min_width_px']}")
            if rule.get("max_width_px") and w > rule["max_width_px"]:
                row["problems"].append(f"{w} px wide, maximum {rule['max_width_px']}")
            if rule.get("max_height_px") and h > rule["max_height_px"]:
                row["problems"].append(f"{h} px high, maximum {rule['max_height_px']}")
            if rule.get("color_modes") and im.mode not in rule["color_modes"]:
                if im.mode in ("RGBA", "LA", "P") and "RGB" in rule["color_modes"]:
                    row.setdefault("notes", []).append(
                        f"color mode {im.mode} (transparency); the TIFF copy is flattened onto white, check it looks the same")
                else:
                    row["problems"].append(f"color mode {im.mode}, journal wants {' or '.join(rule['color_modes'])}")
            if rule.get("max_mb") and row["mb"] > rule["max_mb"]:
                row["problems"].append(f"{row['mb']} MB, maximum {rule['max_mb']} MB")
            stem = rule.get("name_pattern", "{stem}").format(n=n, stem=f.stem) if n else f.stem
            if fmts and ext not in fmts:
                if {"tif", "tiff"} & set(fmts) and im.format in ("JPEG", "PNG", "BMP"):
                    if im.mode in ("RGBA", "LA", "P"):
                        rgba = im.convert("RGBA")
                        conv = Image.new("RGB", rgba.size, (255, 255, 255))
                        conv.paste(rgba, mask=rgba.split()[3])
                    else:
                        conv = im.convert("RGB") if im.mode not in ("RGB", "L") else im
                    out_name = f"{stem}.tif"
                    conv.save(dest / out_name, format="TIFF", compression="tiff_lzw", dpi=(dpi or 300, dpi or 300))
                    reopened = Image.open(dest / out_name)
                    same = reopened.size == im.size and reopened.convert(conv.mode).tobytes() == conv.tobytes()
                    flattened = im.mode in ("RGBA", "LA", "P")
                    actions.append(f"{f.name} -> {out_name}: converted to LZW TIFF "
                                   + ("(transparency flattened onto white)" if flattened
                                      else "(pixels identical)" if same else "(PIXELS DIFFER, check by hand)"))
                    if not same and not flattened:
                        row["problems"].append("conversion changed pixels")
                    row["converted_to"] = out_name
                else:
                    row["problems"].append(f"format {ext.upper()} not accepted ({', '.join(f.upper() for f in fmts)})")
                    _copy(f, dest / f.name)
            else:
                out_name = f"{stem}{f.suffix.lower()}"
                _copy(f, dest / out_name)
                if out_name != f.name:
                    actions.append(f"{f.name} -> {out_name}: renamed to the journal's pattern")
        else:
            _copy(f, dest / f.name)
        rows.append(row)
    return rows, actions


def _vector_figure(f: Path, n, rule: dict | None, dest: Path, actions: list) -> dict:
    """PDF/EPS/SVG figures have no pixel size. Accept if the journal takes the format; otherwise
    offer a 600 dpi raster copy (needs pdftoppm), clearly marked for the author to check."""
    import subprocess

    ext = f.suffix.lower().lstrip(".")
    row = {"file": f.name, "number": n, "format": ext.upper() + " (vector)", "mode": "", "px": "", "dpi": "",
           "mb": round(f.stat().st_size / 1e6, 2), "problems": []}
    fmts = [x.lower() for x in (rule or {}).get("formats", [])]
    if not rule or not fmts or ext in fmts:
        _copy(f, dest / f.name)
        return row
    if ext == "pdf" and shutil.which("pdftoppm") and {"tif", "tiff"} & set(fmts):
        stem = rule.get("name_pattern", "{stem}").format(n=n, stem=f.stem) if n else f.stem
        out = dest / stem
        r = subprocess.run(["pdftoppm", "-r", "600", "-singlefile", "-tiff", "-tiffcompression", "lzw", str(f), str(out)],
                           capture_output=True)
        if r.returncode == 0 and (dest / f"{stem}.tif").exists():
            actions.append(f"{f.name} -> {stem}.tif: vector PDF rendered as a 600 dpi LZW TIFF")
            row.setdefault("notes", []).append("vector PDF was rendered as a 600 dpi image; check that text and lines look sharp")
            return row
    row["problems"].append(f"{ext.upper()} figures are not accepted ({', '.join(x.upper() for x in fmts)}); export it in an accepted format")
    _copy(f, dest / f.name)
    return row


# ---------------------------------------------------------------- cross-references
def _supp_labels_in_files(files: list[Path]) -> dict[str, str]:
    """Map 'Table S4' style labels to the file that holds them (by name, or inside .docx files)."""
    found = {}
    for f in files:
        for m in prepare.SUPP_RE.finditer(f.stem.replace("_", " ")):
            found[_label(m)] = f.name
        if f.suffix.lower() == ".docx":
            try:
                x = zipfile.ZipFile(f).read("word/document.xml").decode("utf-8")
                x = re.sub(r"<w:instrText[^>]*>.*?</w:instrText>", "", x, flags=re.S)  # field codes, e.g. SEQ
                text = re.sub(r"<[^>]+>", "", re.sub(r"</w:p>", "\n", x))
                for m in prepare.SUPP_RE.finditer(text):
                    found.setdefault(_label(m), f.name)
            except Exception:
                pass
    return found


def _label(m) -> str:
    kind = (m.group(1) or m.group(4) or "").rstrip(".").title()
    kind = {"Fig": "Figure"}.get(kind, kind)
    return f"{kind} S{m.group(2) or m.group(3)}"


def tex_crossrefs(tex: Path) -> list[dict]:
    """LaTeX: every figure/table label is \\ref'd, and first references follow the float order."""
    import latex

    _, body = latex.split_document(latex.read_project(tex))
    issues = []
    for env, kind in (("figure", "Figure"), ("table", "Table")):
        labels = []
        for f in re.finditer(rf"\\begin\{{{env}\*?\}}(.*?)\\end\{{{env}\*?\}}", body, re.S):
            m = re.search(r"\\label\s*\{([^}]*)\}", f.group(1))
            if "\\caption" in f.group(1):
                labels.append(m.group(1) if m else None)
        first = {}
        for m in re.finditer(r"\\(?:ref|autoref|cref|Cref|eqref)\s*\{([^}]*)\}", body):
            for lab in m.group(1).split(","):
                first.setdefault(lab.strip(), m.start())
        for n, lab in enumerate(labels, 1):
            if lab is None:
                issues.append({"severity": "CHECK", "item": f"{kind} {n}", "problem": "has no \\label, so it cannot be referenced"})
            elif lab not in first:
                issues.append({"severity": "FIX", "item": f"{kind} {n} ({lab})", "problem": "is never referenced in the text"})
        order = [first[l] for l in labels if l in first]
        if order != sorted(order):
            issues.append({"severity": "FIX", "item": kind, "problem": "first referenced out of numerical order"})
    return issues


def crossrefs(facts: dict, docx: Path, supp_files: list[Path] | None) -> list[dict]:
    if docx.suffix.lower() == ".tex":
        issues = tex_crossrefs(docx)
        supp_mentioned = {_norm_supp(s) for s in facts["supplementary_items"]}
        if supp_files is not None:
            have = {_norm_supp(k): v for k, v in _supp_labels_in_files(supp_files).items()}
            for s_ in sorted(supp_mentioned - set(have)):
                issues.append({"severity": "FIX", "item": s_, "problem": "cited in the manuscript but no supplementary file contains it"})
        return issues
    issues = []
    for kind, nums in (("Figure", facts["figures"]), ("Table", facts["tables"])):
        if nums and nums != list(range(1, max(nums) + 1)):
            issues.append({"severity": "FIX", "item": kind, "problem": f"numbering has gaps: {nums}"})
    text = " ".join(p for p in prepare_text(docx))
    mentioned = []
    for kind, n in re.findall(r"\b(Fig(?:ure)?s?\.?|Tables?)\s*(\d+)", text):
        k = "Table" if kind.lower().startswith("t") else "Figure"
        if (k, int(n)) not in mentioned:
            mentioned.append((k, int(n)))
    for k, nums in (("Figure", facts["figures"]), ("Table", facts["tables"])):
        order = [n for kk, n in mentioned if kk == k]
        for n in nums:
            if n not in order:
                issues.append({"severity": "FIX", "item": f"{k} {n}", "problem": "has a caption but is never cited in the text"})
        if order != sorted(order):
            issues.append({"severity": "FIX", "item": k, "problem": f"first cited out of order: {order}"})
    supp_mentioned = {_norm_supp(s) for s in facts["supplementary_items"]}
    if supp_files is not None:
        have = {_norm_supp(k): v for k, v in _supp_labels_in_files(supp_files).items()}
        for s in sorted(supp_mentioned - set(have)):
            issues.append({"severity": "FIX", "item": s, "problem": "cited in the manuscript but no supplementary file contains it"})
        for s in sorted(set(have) - supp_mentioned):
            issues.append({"severity": "CHECK", "item": s, "problem": f"in {have[s]} but never cited in the manuscript"})
    return issues


def _norm_supp(s: str) -> str:
    m = re.match(r"(\w+)\.? S(\d+)", s)
    if not m:
        return s
    kind = {"Fig": "Figure"}.get(m.group(1).title(), m.group(1).title())
    return f"{kind} S{int(m.group(2))}"


def prepare_text(docx: Path) -> list[str]:
    if docx.suffix.lower() == ".tex":
        import latex

        _, body = latex.split_document(latex.read_project(docx))
        return [latex.detex(b) for b in re.split(r"\n\s*\n", body)]
    import docx as pydocx

    return [p.text for p in pydocx.Document(docx).paragraphs]


def tex_figures(tex: Path) -> list[Path]:
    """Figure files referenced by \\includegraphics, in order, resolved like TeX does (common extensions)."""
    import latex

    _, body = latex.split_document(latex.read_project(tex))
    out = []
    for n, m in enumerate(re.finditer(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", body), 1):
        base = tex.parent / m.group(1).strip()
        cands = [base] if base.suffix else [base.with_suffix(e) for e in (".pdf", ".png", ".jpg", ".jpeg", ".eps", ".tif")]
        for c in cands:
            if c.exists():
                out.append(c)
                break
    return out


TEX_BUILD = {".aux", ".log", ".out", ".toc", ".bbl", ".blg", ".synctex.gz", ".fls", ".fdb_latexmk", ".lof", ".lot"}


# ---------------------------------------------------------------- main audit
def run(manuscript: Path, lock_path: Path, req_path: Path, out: Path, figures: Path | None = None,
        supp: Path | None = None, library: Path | None = None, online: bool = False, email: str = "") -> dict:
    out.mkdir(parents=True, exist_ok=True)
    rep = out / "reports"
    rep.mkdir(exist_ok=True)
    req = json.loads(req_path.read_text(encoding="utf-8"))
    blocking, checks, done = [], [], []

    is_tex = manuscript.suffix.lower() == ".tex"
    # 0. tracked changes / comments in the final file (Word only)
    refusals = []
    if not is_tex:
        with zipfile.ZipFile(manuscript) as z:
            refusals = lockmod.check_refusals(z)
    for r in refusals:
        blocking.append(f"The manuscript still has {r}")

    # 1. text lock
    lv = lockmod.verify(lock_path, manuscript)
    (rep / "change_report.md").write_text(lockmod.report_markdown(lv), encoding="utf-8")
    if lv["status"] == 2:
        blocking.append(f"Text changed: {len(lv['changed'])} changed and {len(lv['missing'])} missing paragraphs "
                        "(see reports/change_report.md)")
    elif lv["status"] == 1:
        checks.append(f"{len(lv['added'])} paragraphs were added; confirm each one (reports/change_report.md)")

    # 2. journal checklist
    facts = prepare.analyze(manuscript)
    res = prepare.check(facts, req)
    (rep / "checklist.md").write_text(prepare.checklist_markdown(facts, req, res), encoding="utf-8")
    for r in res:
        if r["status"] in ("FIX", "AUTO"):
            if r["id"] != "figure-files":
                blocking.append(f"{r['rule']} (found: {r['found']})")
        elif r["status"] == "STYLE":
            if is_tex:
                blocking.append(f"Reference style: {r['rule']}. Change \\bibliographystyle (or the natbib/biblatex options) "
                                "to the journal's style, ideally from its LaTeX template, then rerun /lock")
            else:
                blocking.append(f"Reference style: {r['rule']}. Pick '{req.get('csl_style', {}).get('title', 'the journal style')}' "
                                "in your reference manager, then rerun /lock")
        elif r["status"] == "CHECK" and r["id"] != "figure-files":
            checks.append(r["rule"])

    # 3. references
    ms = refsmod.load(manuscript)
    lib = refsmod.load(library).refs if library else None
    findings = refsmod.audit(ms, lib)
    stats = None
    if online:
        try:
            more, stats = refsmod.verify(ms, refsmod.http_fetch(email))
            findings += more
        except Exception as e:
            checks.append(f"Online reference check could not run ({e}); run it again with internet access")
    else:
        checks.append("Online reference check (Crossref, PubMed, retractions) was not run; use --online")
    (rep / "references.md").write_text(refsmod.report_markdown(manuscript.name, ms, findings, stats), encoding="utf-8")
    sev = Counter(f.severity for f in findings)
    if sev.get("error"):
        blocking.append(f"{sev['error']} reference errors (see reports/references.md)")
    if sev.get("warning"):
        checks.append(f"{sev['warning']} reference warnings to review (reports/references.md)")

    # 4. figures
    fig_rule = next((r["check"] for r in req["requirements"] if r.get("check", {}).get("type") == "figure_files"), None)
    fig_files: list[Path] = []
    if figures:
        fig_files = sorted(p for p in figures.iterdir() if p.suffix.lower() in IMG_EXT)
    elif is_tex:
        found = tex_figures(manuscript)
        fig_files = []
        tmp = out / "_extracted"
        tmp.mkdir(parents=True, exist_ok=True)
        for n, f in enumerate(found, 1):
            dst = tmp / f"Figure_{n}{f.suffix.lower()}"
            _copy(f, dst)
            fig_files.append(dst)
        if found:
            done.append(f"Collected {len(found)} figure files referenced by \\includegraphics")
    elif facts["figures_embedded"]:
        fig_files = extract_embedded_figures(manuscript, out / "_extracted")
        done.append(f"Copied {len(fig_files)} figures out of the manuscript into separate files (the manuscript is unchanged)")
    rows, actions = check_figures(fig_files, fig_rule, out / "figures") if fig_files else ([], [])
    shutil.rmtree(out / "_extracted", ignore_errors=True)
    done += actions
    fig_md = ["# Figure files", "", "| File | Format | Mode | Pixels | dpi | Size | Problems | Notes |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        fig_md.append(f"| {r['file']} | {r.get('format', '')} | {r.get('mode', '')} | {r.get('px', '')} | {r.get('dpi', '')} | "
                      f"{r.get('mb', '')} MB | {'; '.join(r['problems']) or 'none'} | {'; '.join(r.get('notes', []))} |")
    if not fig_rule:
        fig_md += ["", "The journal's figure file rules were not measurable; check them by hand."]
    (rep / "figures.md").write_text("\n".join(fig_md) + "\n", encoding="utf-8")
    for r in rows:
        for pr in r["problems"]:
            blocking.append(f"{r['file']}: {pr}")
        for nt in r.get("notes", []):
            checks.append(f"{r['file']}: {nt}")
    if facts["figures"] and len(rows) < len(facts["figures"]):
        blocking.append(f"{len(facts['figures'])} figures have captions but only {len(rows)} figure files were given")

    # 5. cross-references and supplementary files
    supp_files = sorted(p for p in supp.iterdir() if p.is_file()) if supp else None
    xr = crossrefs(facts, manuscript, supp_files)
    (rep / "crossrefs.md").write_text("# Cross-references\n\n" + ("\n".join(
        f"- {i['severity']}: {i['item']}: {i['problem']}" for i in xr) or "All figures, tables and supplementary items are numbered, cited and present.") + "\n", encoding="utf-8")
    for i in xr:
        (blocking if i["severity"] == "FIX" else checks).append(f"{i['item']}: {i['problem']}")
    if supp_files:
        (out / "supplementary").mkdir(exist_ok=True)
        for f in supp_files:
            _copy(f, out / "supplementary" / f.name)
    elif facts["supplementary_items"]:
        checks.append("Supplementary items are cited but no supplementary folder was given (--supp)")

    # 6. package the manuscript (LaTeX: the whole source folder, without build files)
    if is_tex:
        src_dir = manuscript.parent
        dst_dir = out / "manuscript"
        for f in src_dir.rglob("*"):
            if f.is_file() and not any(str(f).endswith(e) for e in TEX_BUILD) and out.resolve() not in f.resolve().parents \
                    and not f.name.endswith(".lock.json"):
                target = dst_dir / f.relative_to(src_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                _copy(f, target)
    else:
        _copy(manuscript, out / manuscript.name)

    verdict = "READY" if not blocking else "NOT READY"
    summary = [
        f"# SubmitReady: {verdict}",
        "",
        f"**{manuscript.name}** for **{req['journal']}** ({req.get('article_type', '')})",
        "",
        f"- Text lock: **{lv['sentences_unchanged']:,} of {lv['sentences_locked']:,} sentences unchanged**",
        f"- Journal rules: {Counter(r['status'] for r in res).get('PASS', 0)} of {len(res)} pass",
        f"- References: {len(ms.refs)} checked, {sev.get('error', 0)} errors, {sev.get('warning', 0)} warnings",
        f"- Figures: {len(rows)} files checked",
        "",
    ]
    if blocking:
        summary += [f"## Must fix before submitting ({len(blocking)})", ""] + [f"- [ ] {b}" for b in blocking] + [""]
    if done:
        summary += ["## Done automatically", ""] + [f"- {d}" for d in done] + [""]
    if checks:
        summary += [f"## Check yourself ({len(checks)})", ""] + [f"- [ ] {c}" for c in checks] + [""]
    summary += ["## In this folder", "",
                (f"- `manuscript/`: the LaTeX source to upload (main file `{manuscript.name}`)" if is_tex
                 else f"- `{manuscript.name}`: the manuscript to upload"),
                "- `figures/`: figure files" if rows else "",
                "- `supplementary/`: supplementary files" if supp_files else "",
                "- `reports/`: change report, journal checklist, references, figures, cross-references", ""]
    (out / "SUMMARY.md").write_text("\n".join(s for s in summary if s is not None), encoding="utf-8")
    return {"verdict": verdict, "blocking": blocking, "checks": checks, "done": done, "lock": lv["status"]}


def main(argv=None) -> int:
    # Windows terminals and pipes default to a legacy code page; manuscripts contain Unicode.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="SubmitReady final check and submission package")
    ap.add_argument("manuscript", type=Path, help="the formatted manuscript to submit")
    ap.add_argument("--lock", type=Path, required=True, help="lock of the ORIGINAL manuscript")
    ap.add_argument("--requirements", type=Path, required=True)
    ap.add_argument("--figures", type=Path, help="folder with figure files (otherwise embedded images are used)")
    ap.add_argument("--supp", type=Path, help="folder with supplementary files")
    ap.add_argument("--library", type=Path, help="reference-manager export (RIS/BibTeX) to compare")
    ap.add_argument("--online", action="store_true", help="also verify references with Crossref and PubMed")
    ap.add_argument("--email", default="")
    ap.add_argument("--out", type=Path, default=Path("submission_package"))
    a = ap.parse_args(argv)
    r = run(a.manuscript, a.lock, a.requirements, a.out, a.figures, a.supp, a.library, a.online, a.email)
    print((a.out / "SUMMARY.md").read_text(encoding="utf-8"))
    return 0 if r["verdict"] == "READY" else 1


if __name__ == "__main__":
    sys.exit(main())
