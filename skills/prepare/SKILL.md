---
name: prepare
description: Check a manuscript against a journal's live author guidelines and apply safe layout fixes
argument-hint: <manuscript.docx> <journal name> [article type]
disable-model-invocation: true
---

Prepare this manuscript for submission: $ARGUMENTS

1. **Lock first.** If there is no lock for this manuscript, run `/lock` on it before anything else.
2. **Read the live guidelines.** Find the journal's official author guidelines and article-type pages. Read them now, even if a snapshot exists in `journals/requirements/`. If a page cannot be read automatically, ask the user to open it and paste the text. Never fill gaps from memory.
3. **Write the requirements file** (same format as the files in `journals/requirements/`): one entry per rule, with the exact quoted sentence where possible (leave `quote` empty if you paraphrased), the source URL and today's date. Use a measurable `check` type when one fits; otherwise `manual`. Find the journal's CSL style in the CSL styles repository and record its id and URL.
4. **Check:** `python "${CLAUDE_PLUGIN_ROOT}/scripts/prepare.py" check <manuscript.docx> <requirements.json> --report checklist.md`
5. **Format (layout only):** `python "${CLAUDE_PLUGIN_ROOT}/scripts/prepare.py" format <manuscript.docx> <requirements.json> --out <manuscript>_<journal>.docx`
6. **Prove it:** `python "${CLAUDE_PLUGIN_ROOT}/scripts/lock.py" verify <lock.json> <manuscript>_<journal>.docx --report change_report.md`. Show the PASS/STOP line.
7. Summarize for the user: what was fixed automatically, what they must fix (with the guideline quote), which reference style to pick in their reference manager, and the "check yourself" items. Never edit manuscript text.
