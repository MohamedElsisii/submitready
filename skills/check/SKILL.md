---
name: check
description: Final pre-submission audit; builds the submission package with a READY / NOT READY verdict
argument-hint: <formatted.docx|main.tex> <journal> [figures folder] [supplementary folder]
disable-model-invocation: true
---

Run the final check for: $ARGUMENTS

1. You need: the formatted manuscript, the lock of the ORIGINAL manuscript (from `/lock`), and the requirements file written by `/prepare` from the journal's live guidelines. If any is missing, run that step first.
2. Run:
   `python "${CLAUDE_PLUGIN_ROOT}/scripts/check.py" <formatted.docx> --lock <original.lock.json> --requirements <requirements.json> [--figures <dir>] [--supp <dir>] [--library <export.ris>] --online --out <manuscript>_<journal>_package`. Exit code 1 means NOT READY; it is not a failure.
   If the online reference check fails, say so plainly and keep the offline results.
   If changes were approved with `changes.py`, also pass `--approved <plan.json>` and `--original <original manuscript>`.
3. Show the user the verdict and every item under "Your to-do", word for word from `SUMMARY.md` in that package folder, keeping its groups (Word, references, figure files, supplementary files, submission website) and each "Where" line. Then the "Done automatically" list, then the "Check yourself" list. Mention `submission_form.md`, the copy-paste sheet for the journal's website.
4. Never claim READY unless SUMMARY.md says READY. Never edit the manuscript to clear an item; the author fixes it, then runs `/check` again.
