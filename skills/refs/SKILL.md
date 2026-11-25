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
4. Citations inserted with a reference manager (Mendeley Cite, Zotero, EndNote) stay live. The report names the manager it found; give the steps for that manager only (Zotero: Document Preferences; EndNote: the Style list, with EndNote's own style files; Mendeley: the Mendeley Cite panel). Then rerun `/lock` to confirm only citation markers changed.
   On Linux there is no desktop Word. For Mendeley Cite, say the same steps work in Word for the web (office.com, with the Mendeley Cite add-in): upload a copy of the file to OneDrive, refresh there, then download it. Warn that saving the file from LibreOffice can change Word citation fields.
5. A library export can be RIS, BibTeX, CSL JSON or EndNote XML (`--library <file>`).
