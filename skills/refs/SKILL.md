---
name: refs
description: Check a manuscript's references offline, then against Crossref and PubMed
argument-hint: <manuscript.docx | main.tex | refs.ris | refs.bib> [--library export.ris]
disable-model-invocation: true
---

Check the references of: $ARGUMENTS

1. Run the online check first:
   `python "${CLAUDE_PLUGIN_ROOT}/scripts/refs.py" verify <source> [--library <export>] --report <manuscript name>_refs_report.md` (next to the manuscript, so reports for different papers do not overwrite each other)
   Exit codes: 0 = no findings, 1 = warnings or notes only, 2 = at least one error. Exit code 1 is not a failure.
   If it says the online check failed, tell the user, and that only the offline audit ran.
2. Summarize: number of references, errors, warnings. List every ERROR (retracted papers, swapped author names, DOIs Crossref does not know, duplicates) with what was found and the suggestion.
3. Never change a reference yourself. The user fixes it in their reference manager (Mendeley, Zotero, EndNote) so their library stays correct.
4. Citations inserted with a reference manager stay live. To change the citation style, tell the user which style to pick in their plugin; then rerun `/lock` to confirm only citation markers changed.
