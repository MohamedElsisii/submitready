---
name: submit
description: Start here. One command that prepares a manuscript for a journal from start to finish - lock, live guidelines, references, layout, figures, supplementary files, final check and a copy-paste sheet for the submission website
argument-hint: <manuscript.docx|main.tex> <journal name> [article type]
disable-model-invocation: true
---

Prepare this manuscript for submission, from start to finish: $ARGUMENTS

The researcher should have to do as little as possible. Ask only what you cannot find yourself, all at once, at the start. After that, run every step without stopping, unless a step must stop (tracked changes, a STOP from the lock, or a page you could not read).

Follow the five rules in `${CLAUDE_PLUGIN_ROOT}/skills/submitready/SKILL.md` on every step. Never edit manuscript text yourself.

## 1. Gather the inputs (one round of questions at most)

- Manuscript and journal: from the arguments. If the journal is missing, run the journal finder (`${CLAUDE_PLUGIN_ROOT}/skills/find/SKILL.md`) first and continue here with the journal the user picks.
- Article type: use the one in the arguments. If missing, use the journal's research article type and say so.
- Figures folder: look next to the manuscript for a folder whose name contains "fig". If there is none, use the figures inside the manuscript file.
- Supplementary folder: look next to the manuscript for a folder whose name contains "supp" or "SI".
- Reference library export (RIS or BibTeX): optional. Use one if it sits next to the manuscript with the same base name.
- If more than one folder could fit, or nothing fits and the manuscript cites supplementary items, ask the user in one question. Otherwise do not ask.

All outputs go next to the manuscript and are named after it and the journal, so different papers never overwrite each other.

## 2. Lock

`python "${CLAUDE_PLUGIN_ROOT}/scripts/lock.py" create <manuscript> --out <manuscript>.lock.json`

If it is refused (tracked changes or comments), stop and tell the user exactly what to accept, reject or delete in Word. This is the only reason to stop here.

## 3. Journal rules from the live guidelines

Do steps 2 and 3 of `${CLAUDE_PLUGIN_ROOT}/skills/prepare/SKILL.md`: read the live pages (and say how much of each you got), then write `<manuscript>_<journal>.requirements.json` starting from the newest snapshot for this journal.

## 4. Layout fixes, then prove nothing else changed

1. `python "${CLAUDE_PLUGIN_ROOT}/scripts/prepare.py" format <manuscript> <requirements.json> --out <manuscript>_<journal>_formatted.docx`
2. `python "${CLAUDE_PLUGIN_ROOT}/scripts/lock.py" verify <manuscript>.lock.json <manuscript>_<journal>_formatted.docx --report <manuscript>_change_report.md`

If the lock says STOP, stop and show every changed line.

## 5. Changes that need the author's yes (Word files only)

Some journal rules can be met without retyping: moving a whole section (for example Funding) out of the submission copy into the form sheet, and swapping supplementary labels (for example "Table S1" to "S1 Table").

1. `python "${CLAUDE_PLUGIN_ROOT}/scripts/changes.py" plan <manuscript>_<journal>_formatted.docx <manuscript>.lock.json <requirements.json> [--supp <dir>] --out <manuscript>_<journal>.changes.json`
2. If the plan is empty, skip to step 6 with the formatted file.
3. Otherwise show the user `<manuscript>_<journal>.changes.md` and ask once, with one multiple-choice question listing each change by its title, which ones to make. Never approve a change yourself, even in auto mode. If the user is not there, make none of them and list them in the to-do.
4. Set `"approved": true` only on the changes the user chose, in the `.changes.json` file. Change nothing else in it.
5. `python "${CLAUDE_PLUGIN_ROOT}/scripts/changes.py" apply <manuscript>_<journal>_formatted.docx <manuscript>_<journal>.changes.json <manuscript>.lock.json --out <manuscript>_<journal>.docx`
   It checks its own result with the lock and refuses anything the user did not approve.

The file to submit is `<manuscript>_<journal>.docx` if changes were made, otherwise `<manuscript>_<journal>_formatted.docx`.

## 6. Final check and package

`python "${CLAUDE_PLUGIN_ROOT}/scripts/check.py" <file to submit> --lock <manuscript>.lock.json --requirements <requirements.json> --original <manuscript> [--approved <manuscript>_<journal>.changes.json] [--figures <dir>] [--supp <dir>] [--library <export>] --online --out <manuscript>_<journal>_package`

Exit code 1 means NOT READY. It is not a failure. If the online reference check could not run, say so in one line.

## 7. Tell the user, in this order, on one screen

1. **Verdict:** READY or NOT READY, and the lock line ("all N sentences unchanged").
2. **Done for you:** the layout fixes, the changes they approved, and the figure conversions, one line each.
3. **Your to-do:** copy the "Your to-do" list from `SUMMARY.md` as it is, grouped by where the work happens (reference manager, Word, submission website, figure files). Keep each item's location and guideline quote. Do not add items of your own.
4. **Ready to paste:** say that `submission_form.md` in the package holds the title, abstract, keywords, statements and counts, copied word for word for the journal's website.
5. **Where everything is:** the package folder path.

If a script said an older output "was kept as" another name, say so in one line.

Use plain words. No exit codes, script names or file internals unless something failed. Then stop. End with one line: make the Word fixes in the original manuscript (not in the package copy), then run the same command again; the approved changes are redone and checked each time.
