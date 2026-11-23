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
3. Show the user the verdict and every "Must fix" item, word for word from `SUMMARY.md` in that package folder. Then the "Done automatically" list, then the "Check yourself" list.
4. Never claim READY unless SUMMARY.md says READY. Never edit the manuscript to clear an item; the author fixes it, then runs `/check` again.
