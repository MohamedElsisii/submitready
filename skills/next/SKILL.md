---
name: next
description: After a rejection, move to the next journal on the shortlist made by /submitready:find, refresh its facts, and prepare the manuscript for it once the user confirms
argument-hint: <manuscript.docx|main.tex>
disable-model-invocation: true
---

The manuscript was rejected; prepare it for the next journal: $ARGUMENTS

Follow the five rules in `${CLAUDE_PLUGIN_ROOT}/skills/submitready/SKILL.md`.

1. Find `<manuscript>.journals.json` next to the manuscript. If there is none, run `${CLAUDE_PLUGIN_ROOT}/skills/find/SKILL.md` instead and stop here.
2. Read the list. If no journal has status "chosen", ask which journal rejected the paper before going on.
3. `python "${CLAUDE_PLUGIN_ROOT}/scripts/match.py" next <manuscript>.journals.json`
   This marks the chosen journal as rejected and prints the next open one. If it prints `null`, the list is used up: offer to run `/submitready:find` again (the user may want to change the filters, or mention what the reviewers asked for).
4. Refresh the next journal's facts live, as in step 3 of the find skill (aims and scope quote, article type, impact factor as the journal states it), and record them with `match.py add`. Say the date of the list's other numbers (`checked` in the file).
5. Show that journal's row and fit lines from `python "${CLAUDE_PLUGIN_ROOT}/scripts/match.py" table <manuscript>.journals.json --all`, and ask once: prepare for this journal, skip to the one after it, or stop.
   - Skip: run step 3 again (the skipped journal is marked rejected; say so).
6. On yes: `python "${CLAUDE_PLUGIN_ROOT}/scripts/match.py" choose <manuscript>.journals.json <rank>`, then run the whole flow of `${CLAUDE_PLUGIN_ROOT}/skills/submit/SKILL.md` for this journal. If the author revised the manuscript after the reviews, the flow locks the revised version; that is expected.

Remind the user in one line that the reference style changes with the journal: they pick the new style in their reference manager (the to-do list names it).
