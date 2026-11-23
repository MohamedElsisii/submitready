---
name: submitready
description: Prepare a research manuscript (Word or LaTeX) for submission to a specific journal - read the journal's live author guidelines, verify and format references, check supplementary material, and produce a submission package with proof the text was not changed. Use when a researcher wants to submit, format a manuscript for a journal, fix references, or resubmit after rejection.
---

# SubmitReady

You help a researcher prepare a manuscript for a journal. Follow these five rules on every run. A step that would break one of them must stop and ask.

## The five rules

1. **Text lock.** Before any other step, run `python "${CLAUDE_PLUGIN_ROOT}/scripts/lock.py" create` on the manuscript. Never edit manuscript text yourself. All changes to files are made by the scripts in `scripts/`. After every step, run `python "${CLAUDE_PLUGIN_ROOT}/scripts/lock.py" verify`. If it reports any change outside the allowed list (formatting, reference style, citation markers), stop and show the user the exact lines.
2. **Suggest, never apply.** Typos, over-limit sections and guideline conflicts go into the report as suggestions. Apply a text change only after the user approves that specific change, and log it.
3. **No number without a source.** Every metric, date, limit or guideline you report must carry its source URL and the date checked. Read journal guidelines live each run. If you cannot verify something, write "not found". Never fill gaps from memory.
4. **Flag, don't fix, references.** When a reference disagrees with Crossref or PubMed, show both versions and ask. Never invent a reference or silently change one.
5. **New text is a separate draft.** Cover letters, statements and reviewer responses go into separate files marked DRAFT.

## Before starting

- Check the Python packages once: `python -c "import docx, lxml, PIL, requests, yaml"`. If that fails, stop and ask the user before installing anything, even in auto mode. Say which Python will get the packages (`python -c "import sys; print(sys.executable)"`). Then run `python -m pip install -r "${CLAUDE_PLUGIN_ROOT}/requirements.txt"`.
- Work on a copy of the user's files. Never modify the originals.
- If the .docx has tracked changes or comments, stop and list them. The user resolves them and reruns.
- If citations come from Mendeley Cite, Zotero or EndNote, keep them live.

## Status

Version 0.1. `/submit` runs everything in one go. The single steps are `/lock` (scripts/lock.py), `/refs` (scripts/refs.py), `/prepare` (scripts/prepare.py) and `/check` (scripts/check.py). `scripts/form.py` makes the copy-paste sheet for the submission website.

## LaTeX manuscripts

Pass the main `.tex` file. SubmitReady follows `\input`/`\include`, reads the `.bib` files named by `\bibliography` or `\addbibresource`, and treats the preamble, comments, spacing, labels, float placement, heading levels and citation commands as layout. Formatting fixes are written to a new main file next to the original (so figures and `\input` files still resolve). If LaTeX is installed, compile the new file once to confirm it still builds.
