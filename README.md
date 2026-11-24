<img src="assets/icon.png" width="96" alt="SubmitReady icon">

[![tests](https://github.com/MohamedElsisii/submitready/actions/workflows/tests.yml/badge.svg)](https://github.com/MohamedElsisii/submitready/actions/workflows/tests.yml)

# SubmitReady

**Your manuscript, ready to submit. Not a single word of your science changed.**

SubmitReady is a free, open-source Claude skill that prepares a research manuscript for a specific journal: it reads the journal's live author guidelines, checks and formats your references, prepares supplementary files, and hands you a submission package with a report proving that your text was not changed.

> Status: in development. All four version 1 commands (`/lock`, `/refs`, `/prepare`, `/check`) work and are being tested on real manuscripts. Public release planned for early 2027.

## Why

Preparing one manuscript for one journal takes days, and every rejection restarts the work for the next journal. Existing tools are paid, cover only one step, or rewrite your words. SubmitReady does the whole job and proves it touched nothing it shouldn't.

## See it on a real paper

We ran SubmitReady on a published, peer-reviewed review with 296 references: Elsisi et al. (2024), *Stress Biology* 4:31, [doi:10.1007/s44154-024-00160-2](https://doi.org/10.1007/s44154-024-00160-2). The article is open access under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Its first author is SubmitReady's maintainer.

The online reference check took under two minutes and matched 289 of 296 references in Crossref. It found problems that passed peer review and copyediting:

- **27 DOIs with web-page junk at the end**, such as `10.2478/s11756-020-00507-w/figures/5` or `10.3389/fpls.2017.01206/bibtex`. Each one is shown with the corrected DOI, confirmed in Crossref.
- **Two references share one DOI** (Khokon 2010 and Khokon 2011).
- **A cited paper has an expression of concern**, published in April 2024 (Crossref notice 10.1016/j.chemosphere.2024.141603).
- **A citation with no reference**: "Fu et al., 2021" is cited in the text, but no Fu is in the list.
- **One entry, two citations**: Garcion 2008a and 2008b, Horváth 2007a and 2007b, and Huang 2010a and 2010b are cited, but each has only one list entry.
- **Figures cited out of order**: Figure 3 is cited before Figure 2.

Then `/submitready:prepare` reformatted the manuscript for another journal (line numbers, line spacing), and the text lock confirmed that all 1,842 sentences stayed exactly the same.

Nothing was changed in the references. Every finding is a suggestion for the authors to accept or reject.

## The five rules

1. **The text lock.** Every sentence of your manuscript (body, tables, figure legends, abstract, title, keywords) is fingerprinted before any step. Formatting is applied by plain scripts, never by AI rewriting. If a single word changes, the run stops and shows you the line.
2. **Suggest, never apply.** Typos, word-limit overruns and guideline conflicts are listed for you to fix. Only two kinds of change can be made for you, and only after you say yes to each: moving a whole section (like Funding) out of the submission copy word for word, and swapping supplementary labels exactly (like "Table S1" to "S1 Table"). The lock then proves those were the only changes.
3. **No number without a source.** Every metric, date or guideline comes with its source link and the date it was checked. If something can't be verified, SubmitReady says "not found". It never guesses.
4. **Flag, don't fix, references.** If a reference disagrees with Crossref or PubMed, you see both versions and decide. Nothing is invented or silently corrected.
5. **New text is a separate draft.** Cover letters, statements and reviewer responses are separate files, marked as drafts, never mixed into your manuscript.

## Find the right journal

No journal yet? `/submitready:find my_paper.docx` looks for journals that published papers like yours in the last five years, screens out ones it cannot confirm as legitimate, applies your filters (open access, subscription or hybrid, a fee limit, MEDLINE) and shows 5 with the numbers that matter. Every number has its source; anything that can't be found says "not found". Pick one and it is prepared. After a rejection, `/submitready:next` moves to the next journal on your list.

Add the free SCImago and Scopus lists once a year (see [BETA.md](BETA.md)) and the list also shows SJR quartiles, Scopus status, and leaves out titles Scopus discontinued. You download these files yourself; they stay on your computer.

## What you get

One folder per paper and journal, with:

- the formatted manuscript, with proof that every sentence is unchanged
- figure files converted to what the journal wants
- a to-do list grouped by where the work happens (Word, reference manager, figure files, submission website), each item with where to find it and the guideline's own words
- with your yes, the small fixes done for you: sections the journal wants in its web form moved out of the copy, and supplementary labels and file names switched to the journal's style
- `submission_form.md`: your title, abstract, keywords, funding, competing interests, data availability and author contributions, copied word for word, ready to paste into the journal's website

## Install

In Claude Code (run `claude update` first):

```
/plugin marketplace add MohamedElsisii/submitready
/plugin install submitready@submitready
```

Then restart Claude Code and run `/submitready:submit my_paper.docx PLOS ONE`. Beta testers: see [BETA.md](BETA.md).

SubmitReady's scripts need Python 3.10+ and a few packages. On first use, Claude checks for them and asks before installing (`pip install -r requirements.txt`).

To try it from a local copy instead, see [TESTING.md](TESTING.md).

## Commands

| Command | What it does | Version |
| --- | --- | --- |
| `/submitready:find <paper>` | **No journal yet? Start here.** Finds journals that publish papers like yours, filtered your way (open access, subscription or hybrid, fee limit, MEDLINE), with fit quotes, review times and fees; then prepares the one you pick | v1 |
| `/submitready:next <paper>` | After a rejection: the next journal on your list, refreshed, then prepared once you say yes | v1 |
| `/submitready:submit <paper> <journal>` | **Journal already chosen? Start here.** Does everything below in one go and gives you one to-do list and a copy-paste sheet for the journal website | v1 |
| `/submitready:lock` | Fingerprints your manuscript and proves later that nothing changed | v1 |
| `/submitready:prepare <journal>` | Reads the journal's live guidelines, builds a checklist, formats your files | v1 |
| `/submitready:refs` | Verifies every reference against Crossref and PubMed, flags problems, tells you which style to pick | v1 |
| `/submitready:check` | Final pre-submission audit | v1 |
| `/supp` | Numbers, checks and packages supplementary material | v2 |
| `/cover` | Drafts a cover letter for your approval | v2 |
| `/respond` | Organizes reviewer comments and drafts replies for your approval | v3 |

## Journals

`/prepare` works with any journal: it reads the live author guidelines each time. These journals also have a checked starting profile, with a source link and date for every rule:

PLOS ONE, Frontiers in Genetics, Scientific Reports, International Journal of Molecular Sciences, PeerJ, Bioinformatics, Heliyon, BMC Genomics, Stress Biology (Original Paper and Review).

Adding a journal is a good first contribution. Copy `journals/_TEMPLATE.yaml` and quote the guidelines page for each rule.

## Supported inputs (v1)

- Manuscripts: Word (.docx) and LaTeX
- References: the reference list in your manuscript, Mendeley Cite citations, EndNote / Mendeley exports (RIS, XML), BibTeX
- Language: English

## Privacy

SubmitReady's scripts run on your computer and never change your original file. Claude reads parts of your manuscript while it works, as with anything you open in Claude Code. Outside services receive only what a lookup needs: reference titles and DOIs go to Crossref and PubMed, and guideline pages are read from the journal's website. SubmitReady collects no usage data.

## Contributing

Journal profiles are community-contributed. See [CONTRIBUTING.md](CONTRIBUTING.md) and open an "Add my journal" issue.

## License

[MIT](LICENSE)
