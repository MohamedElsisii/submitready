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
- Indexing, as one choice: "Scopus, Q1 to Q3" (the most common university rule: `--scopus --min-quartile Q3`), "Scopus, Q1 or Q2" (`--min-quartile Q2`), "Scopus, Q2 or Q3 only" (`--min-quartile Q3 --max-quartile Q2`), "MEDLINE", or "no rule". The quartile is the SJR quartile from SCImago, which is computed from Scopus data. Scopus and quartile filters need the two lists (step 0); without them, say the filter cannot be applied and offer to run without it.
- Only when a quartile range is chosen: does the rule go by the journal's best quartile (default), or does a quartile in any of its subject categories count (`--quartile-basis any`)? A journal can be Q1 in Oncology and Q2 in Cancer Research. If the user does not know, use the best quartile and say that the other reading exists.
- Sort by: best fit (default), fastest review, or highest citation rate.

## 2. Search

`python "${CLAUDE_PLUGIN_ROOT}/scripts/match.py" find <manuscript> --out <manuscript>.journals.json [--access any|open|subscription|hybrid] [--max-fee N] [--medline] [--scopus] [--min-quartile Q1|Q2|Q3|Q4] [--max-quartile Q1|Q2|Q3|Q4] [--quartile-basis best|any] [--prefer fit|speed|impact]`

Map "subscription or hybrid" to `--access subscription`. If the search fails (no internet), say so and stop.

Every filter is a script option. Never filter, re-rank or count journals yourself. When the user asks for a rule the script has no option for, say so and offer the nearest option. Counts you report (journals checked, passed, left out and why) come only from the "Search" section of the table, word for word.

## 3. Read each suggested journal live (the 5 marked "suggested")

For each one, read the journal's own pages with the built-in web fetch (for pages that need JavaScript, use Claude in Chrome as step 2 of the prepare skill describes) (the `scope_url_crossref` or `homepage` in the list file is a good start; the publisher's "aims and scope", "about" and "journal metrics" pages).

Some publisher sites (Elsevier often) refuse the built-in fetch. Then try Claude in Chrome. Never use a paid tool (a scraping connector such as Firecrawl, or any service that bills per page or per call) unless the user said yes to it in this conversation. Ask once, say it may cost money, and offer the free ways first: Claude in Chrome, or the user pasting the page text. If a permission check blocks a tool, never work around it. Tell the user in one line which tool was blocked and give both ways forward: allow it (in Claude Code, `/permissions`) and run `/submitready:find` again, or paste the journal's aims and scope text into the chat. A pasted text is recorded with the journal's page address as its source and `--year` left out. Do all this once for all blocked journals, not once per journal.

Record every fact with:

`python "${CLAUDE_PLUGIN_ROOT}/scripts/match.py" add <manuscript>.journals.json <rank> --field <field> --value "<value>" --source <URL> [--year <year>]`

- `scope_quote`: one sentence from the aims and scope, word for word, that shows why this paper fits. If nothing in the scope fits the paper, record that with `--field note` instead.
- `accepts_type`: whether the journal publishes this article type (the list file's `profile.article_type`), from its article-types page.
- `impact_factor`: only as the journal or publisher states it on its own site, with the year. Never from memory, search snippets or third-party sites. If the site does not state it, record nothing.
- `quartile`: only if the SCImago file is not added and a page you read states it. Otherwise leave it.
- `fee_note`: if the journal's site states a different publication fee or waivers than the list shows.

## 4. Show the shortlist and let the user choose

`python "${CLAUDE_PLUGIN_ROOT}/scripts/match.py" table <manuscript>.journals.json`

Show the table and the "Why each journal fits" lines as they are, plus the "Search" counts and the "Please double-check" notes. If the table says only a few journals passed, name the filter that removed the most and offer to loosen it.

Then ask once which journal to prepare for. Every suggested journal must be a choice, plus "none, search again" (then ask what to change and go back to step 2). If the question tool cannot show that many choices, ask in plain text instead: "Reply with a number (1 to 5), or 'search again'." Never drop a journal to fit the question.

## 5. Prepare for the chosen journal

1. `python "${CLAUDE_PLUGIN_ROOT}/scripts/match.py" choose <manuscript>.journals.json <rank>`
2. Run the whole flow of `${CLAUDE_PLUGIN_ROOT}/skills/submit/SKILL.md` for this manuscript and the chosen journal, starting at its step 1.

End by saying that if this journal rejects the paper, `/submitready:next <manuscript>` prepares the next journal on the list.
