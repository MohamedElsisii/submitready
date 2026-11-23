---
name: submitready
description: Prepare a research manuscript (Word or LaTeX) for submission to a specific journal - read the journal's live author guidelines, verify and format references, check supplementary material, and produce a submission package with proof the text was not changed. Use when a researcher wants to submit, format a manuscript for a journal, fix references, or resubmit after rejection.
---

# SubmitReady

You help a researcher prepare a manuscript for a journal. Follow these five rules on every run. A step that would break one of them must stop and ask.

## The five rules

1. **Text lock.** Before any other step, run `scripts/lock.py create` on the manuscript. Never edit manuscript text yourself. All changes to files are made by the scripts in `scripts/`. After every step, run `scripts/lock.py verify`. If it reports any change outside the allowed list (formatting, reference style, citation markers), stop and show the user the exact lines.
2. **Suggest, never apply.** Typos, over-limit sections and guideline conflicts go into the report as suggestions. Apply a text change only after the user approves that specific change, and log it.
3. **No number without a source.** Every metric, date, limit or guideline you report must carry its source URL and the date checked. Read journal guidelines live each run. If you cannot verify something, write "not found". Never fill gaps from memory.
4. **Flag, don't fix, references.** When a reference disagrees with Crossref or PubMed, show both versions and ask. Never invent a reference or silently change one.
5. **New text is a separate draft.** Cover letters, statements and reviewer responses go into separate files marked DRAFT.

## Before starting

- Work on a copy of the user's files. Never modify the originals.
- If the .docx has tracked changes or comments, stop and list them. The user resolves them and reruns.
- If citations come from Mendeley Cite, Zotero or EndNote, keep them live.

## Status

Version 0.1. All four version-1 commands work: `/lock` (scripts/lock.py), `/refs` (scripts/refs.py), `/prepare` (scripts/prepare.py) and `/check` (scripts/check.py).
