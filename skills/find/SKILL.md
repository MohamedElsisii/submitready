---
name: find
description: Find journals that fit a manuscript - journals that publish similar papers, screened, filtered by access model and fee, with impact factor as the journal states it, review time and fit quotes from the aims and scope - then prepare the chosen one
argument-hint: <manuscript.docx|main.tex> [your preferences, e.g. "open access under 2000 USD, MEDLINE"]
disable-model-invocation: true
---

Find the right journal for: $ARGUMENTS

Follow the five rules in `${CLAUDE_PLUGIN_ROOT}/skills/submitready/SKILL.md`. No number without a source: every figure you show comes from the script output or from a page you read in this run, with its link. If you cannot find something, it stays "not found".

## 0. Optional lists

`python "${CLAUDE_PLUGIN_ROOT}/scripts/journaldata.py" status`

If the SCImago file or the Scopus Source List is missing, tell the user in two lines that adding them (free downloads, once a year, see BETA.md) gives real quartiles, Scopus status and a "Scopus only" filter. Then go on without them. If the user has a downloaded file, add it with `journaldata.py add <file>`. Never download these files yourself and never copy them anywhere else.

## 1. Preferences (one round of questions at most)

Take what the arguments already say. Ask only for the rest, in one multiple-choice round:

- Access model: any; fully open access (the authors pay a fee); subscription or hybrid (no fee to publish); hybrid only.
- Maximum publication fee in USD, if open access is allowed (or no limit).
- Indexing: MEDLINE, active in Scopus (only if the Scopus list is added), and a minimum SJR quartile such as Q2 or better (only if the SCImago file is added). Many universities require one of these; ask the user what theirs requires.
- Sort by: best fit (default), fastest review, or highest citation rate.

## 2. Search

`python "${CLAUDE_PLUGIN_ROOT}/scripts/match.py" find <manuscript> --out <manuscript>.journals.json [--access any|open|subscription|hybrid] [--max-fee N] [--medline] [--scopus] [--min-quartile Q1|Q2|Q3] [--prefer fit|speed|impact]`

Map "subscription or hybrid" to `--access subscription`. If the search fails (no internet), say so and stop.

## 3. Read each suggested journal live (the 5 marked "suggested")

For each one, read the journal's own pages with the built-in web fetch (for pages that need JavaScript, use Claude in Chrome as step 2 of the prepare skill describes) (the `scope_url_crossref` or `homepage` in the list file is a good start; the publisher's "aims and scope", "about" and "journal metrics" pages). Record every fact with:

`python "${CLAUDE_PLUGIN_ROOT}/scripts/match.py" add <manuscript>.journals.json <rank> --field <field> --value "<value>" --source <URL> [--year <year>]`

- `scope_quote`: one sentence from the aims and scope, word for word, that shows why this paper fits. If nothing in the scope fits the paper, record that with `--field note` instead.
- `accepts_type`: whether the journal publishes this article type (the list file's `profile.article_type`), from its article-types page.
- `impact_factor`: only as the journal or publisher states it on its own site, with the year. Never from memory, search snippets or third-party sites. If the site does not state it, record nothing.
- `quartile`: only if the SCImago file is not added and a page you read states it. Otherwise leave it.
- `fee_note`: if the journal's site states a different publication fee or waivers than the list shows.

## 4. Show the shortlist and let the user choose

`python "${CLAUDE_PLUGIN_ROOT}/scripts/match.py" table <manuscript>.journals.json`

Show the table and the "Why each journal fits" lines as they are, plus the "Please double-check" notes. Then ask once, as a multiple-choice question: which journal to prepare for (the 5 names), or "none, search again" (then ask what to change and go back to step 2).

## 5. Prepare for the chosen journal

1. `python "${CLAUDE_PLUGIN_ROOT}/scripts/match.py" choose <manuscript>.journals.json <rank>`
2. Run the whole flow of `${CLAUDE_PLUGIN_ROOT}/skills/submit/SKILL.md` for this manuscript and the chosen journal, starting at its step 1.

End by saying that if this journal rejects the paper, `/submitready:next <manuscript>` prepares the next journal on the list.
