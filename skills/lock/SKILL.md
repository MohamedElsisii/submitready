---
name: lock
description: Fingerprint a manuscript, or prove a formatted copy changed no locked text
argument-hint: <manuscript.docx|main.tex> [formatted copy]
disable-model-invocation: true
---

Run the SubmitReady text lock on: $ARGUMENTS

1. Never edit the manuscript yourself. Work on copies only.
2. If only one file is given, run:
   `python "${CLAUDE_PLUGIN_ROOT}/scripts/lock.py" create <manuscript.docx> --out <manuscript>.lock.json`
   If it is refused because of tracked changes or comments, tell the user exactly what to resolve in Word.
3. If two files are given, find the lock for the first one (create it if missing), then run:
   `python "${CLAUDE_PLUGIN_ROOT}/scripts/lock.py" verify <lock.json> <formatted.docx> --report <manuscript>_change_report.md` (next to the manuscript)
   Exit codes: 0 = OK (nothing changed), 1 = new text was added (show it to the user), 2 = STOP (locked text changed or missing), 3 = refused (tracked changes or comments).
4. Show the user the verdict line and the sentence count. "Live citations" counts citation markers, and one marker can cite several references ([3,4] or (Smith 2020; Lee 2021)), so it is often smaller than the number of bibliography entries. Say this in one short sentence whenever the two numbers differ, so the user does not read it as a problem. Whether every reference is cited is checked by /refs. If the result is STOP, list every changed line exactly as the report shows it. Never describe a STOP as minor.
