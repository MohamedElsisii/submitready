# SubmitReady: rules for working in this repo

- Never commit manuscripts, reference libraries or supplementary files from real papers. Real test material lives only in `tests/private/`, which is git-ignored.
- Scripts in `scripts/` must be deterministic Python. They change files; the AI never edits manuscript text directly.
- Every change to the text lock (`scripts/lock.py`) needs a test with planted edits; the lock must catch 100% of them.
- Never commit downloaded journal lists (SCImago, Scopus Source List). Users download them and SubmitReady reads them locally.
- Never invent journal facts in `journals/` profiles. Every value needs a source URL and the date it was checked.
- Keep Mendeley Cite, Zotero and EndNote citation fields working: operate on copies and never flatten live citations unless the user asks.
- Writing style for docs and messages: short sentences, plain words, no em dashes.
