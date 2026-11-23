# SubmitReady

**Your manuscript, ready to submit. Not a single word of your science changed.**

SubmitReady is a free, open-source Claude skill that prepares a research manuscript for a specific journal: it reads the journal's live author guidelines, checks and formats your references, prepares supplementary files, and hands you a submission package with a report proving that your text was not changed.

> Status: in development. All four version 1 commands (`/lock`, `/refs`, `/prepare`, `/check`) work and are being tested on real manuscripts. Public release planned for early 2027.

## Why

Preparing one manuscript for one journal takes days, and every rejection restarts the work for the next journal. Existing tools are paid, cover only one step, or rewrite your words. SubmitReady does the whole job and proves it touched nothing it shouldn't.

## The five rules

1. **The text lock.** Every sentence of your manuscript (body, tables, figure legends, abstract, title, keywords) is fingerprinted before any step. Formatting is applied by plain scripts, never by AI rewriting. If a single word changes, the run stops and shows you the line.
2. **Suggest, never apply.** Typos, word-limit overruns and guideline conflicts are listed for you to fix or approve one by one.
3. **No number without a source.** Every metric, date or guideline comes with its source link and the date it was checked. If something can't be verified, SubmitReady says "not found". It never guesses.
4. **Flag, don't fix, references.** If a reference disagrees with Crossref or PubMed, you see both versions and decide. Nothing is invented or silently corrected.
5. **New text is a separate draft.** Cover letters, statements and reviewer responses are separate files, marked as drafts, never mixed into your manuscript.

## Commands

| Command | What it does | Version |
| --- | --- | --- |
| `/lock` | Fingerprints your manuscript and proves later that nothing changed | v1 |
| `/prepare <journal>` | Reads the journal's live guidelines, builds a checklist, formats your files | v1 |
| `/refs` | Verifies every reference against Crossref and PubMed, flags problems, formats the style | v1 |
| `/check` | Final pre-submission audit | v1 |
| `/supp` | Numbers, checks and packages supplementary material | v2 |
| `/cover` | Drafts a cover letter for your approval | v2 |
| `/match` | Cross-publisher journal shortlist with real review times | v2 |
| `/respond` | Organizes reviewer comments and drafts replies for your approval | v3 |
| `/resubmit <journal>` | After a rejection, prepares everything for the next journal | v3 |

## Supported inputs (v1)

- Manuscripts: Word (.docx) and LaTeX
- References: the reference list in your manuscript, Mendeley Cite citations, EndNote / Mendeley exports (RIS, XML), BibTeX
- Language: English

## Privacy

Your manuscript stays on your machine and in your Claude session. Outside services receive only what a lookup needs (a DOI, a title, a journal name). SubmitReady collects no usage data.

## Contributing

Journal profiles are community-contributed. See [CONTRIBUTING.md](CONTRIBUTING.md) and open an "Add my journal" issue.

## License

[MIT](LICENSE)
