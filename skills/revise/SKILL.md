---
name: revise
description: After a revise decision, turn the decision letter into a response-to-reviewers table (comments word for word, empty Response and Where changed columns), then show which manuscript paragraphs changed and check that every comment is answered
argument-hint: <decision letter .docx|.txt> [original manuscript] [revised manuscript]
disable-model-invocation: true
---

Help the author answer the reviewers: $ARGUMENTS

Follow the five rules in `${CLAUDE_PLUGIN_ROOT}/skills/submitready/SKILL.md`. Above all: the responses are the author's words. Never write, suggest or fill in a response, and never change the manuscript.

1. If the letter is in an email or a PDF, ask the author to save it as a .docx or .txt (copy and paste is fine). Then run:
   `python "${CLAUDE_PLUGIN_ROOT}/scripts/revision.py" split <letter> --out <manuscript>.comments.json`
   Show the count per reviewer and the editor. Show the "how to answer" lines the letter gives (tracked changes, page and line numbers, two versions).
   Offers the journal asks you to answer, such as open access or videos ("post-acceptance services"), come out as optional rows with IDs like `E.S1`: one row per offer that has its own "Author response:" line. They need an answer but no change to the paper. Say how many there are in one line; do not ask whether to include them.
2. Check the split with the author before going on: open the letter yourself and compare. If a comment was cut in two, joined with the next one, or missed, say which, and fix the JSON so each comment is word for word from the letter. Do not reword any comment.
3. `python "${CLAUDE_PLUGIN_ROOT}/scripts/revision.py" table <manuscript>.comments.json --out <manuscript>_response_to_reviewers.docx`
   This is a DRAFT file for the author: one table per reviewer with empty Response and Where changed columns.
4. When the author has a revised manuscript, run:
   `python "${CLAUDE_PLUGIN_ROOT}/scripts/revision.py" changes <original.docx> <revised.docx> --comments <manuscript>.comments.json --out <manuscript>_what_changed.md`
   It lists changed, added and removed paragraphs, and for each comment the changes that share its key terms. Present these as hints for the Where changed column, never as proof a comment was answered.
5. When the author has filled in the table, run:
   `python "${CLAUDE_PLUGIN_ROOT}/scripts/revision.py" status <manuscript>_response_to_reviewers.docx`
   List the comments still without a response, and those answered without saying where the text changed.
   If the author wrote the response letter in their own layout instead of the table, add `--comments <manuscript>.comments.json`. It finds each comment in the letter (small wording changes are fine) and lists comments missing from the letter, comments with no answer, and answers that give no page, line or section. A reviewer's opening summary that is thanked but not quoted is listed apart, not as missing.
6. Many editor comments are format rules (funding heading, CRediT, legends at the end, house reference style). Point the author to `/submitready:prepare` for the same journal: those checks find most of them automatically.
