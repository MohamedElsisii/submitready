---
description: Fingerprint a manuscript, or prove a formatted copy changed no locked text
argument-hint: <manuscript.docx> [formatted.docx]
---

Run the SubmitReady text lock on: $ARGUMENTS

1. Never edit the manuscript yourself. Work on copies only.
2. If only one file is given, run:
   `python "${CLAUDE_PLUGIN_ROOT}/scripts/lock.py" create <manuscript.docx> --out <manuscript>.lock.json`
   If it is refused because of tracked changes or comments, tell the user exactly what to resolve in Word.
3. If two files are given, find the lock for the first one (create it if missing), then run:
   `python "${CLAUDE_PLUGIN_ROOT}/scripts/lock.py" verify <lock.json> <formatted.docx> --report change_report.md`
4. Show the user the verdict line and the sentence count. If the result is STOP, list every changed line exactly as the report shows it. Never describe a STOP as minor.
