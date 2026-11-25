# Testing SubmitReady on your own computer

These steps load SubmitReady into Claude Code from a local copy of the repo. Nothing is installed permanently.

## 1. One-time setup

Open a terminal (on Windows: Anaconda Prompt, or Git Bash) and run:

```
cd %USERPROFILE%\Documents            (Windows)   or   cd ~/Documents   (Mac/Linux)
git clone https://github.com/MohamedElsisii/submitready
cd submitready
python -m pip install -r requirements.txt pytest
python -m pytest -q tests/
```

The last line should end with `passed`. Some tests are skipped on purpose, for example the LaTeX build test when LaTeX is not installed.

Claude Code runs commands in your default Python. With conda, that is usually `base`, even if you activated another environment first. On first use SubmitReady checks for its packages and asks before installing them there.

Sign in to Claude Code with your Claude account (`/login`). If the banner says "API Usage Billing", every message is charged to an API account. Check the plugin too:

```
claude plugin validate .
```

## 2. Start Claude Code with SubmitReady loaded

Go to the folder that holds your manuscript, then start Claude Code with the plugin:

```
cd path\to\my\manuscript\folder
claude --plugin-dir %USERPROFILE%\Documents\submitready      (Windows)
claude --plugin-dir ~/Documents/submitready                  (Mac/Linux)
```

## 3. Run the commands

```
/submitready:lock my_manuscript.docx
/submitready:refs my_manuscript.docx --library my_library.ris
/submitready:prepare my_manuscript.docx PLOS ONE
/submitready:check my_manuscript_plos-one.docx PLOS ONE figures supplementary
```

After pulling new changes (`git pull`), type `/reload-plugins` in a running session.

## What to report

For every run, note in a GitHub issue (no manuscript attached):
- the command, the journal, and how long it took;
- anything wrong: a missed problem, a false alarm, a crash, a confusing message;
- whether the online reference check ran.

## The big automated test (no testers needed)

`tests/matrix.py` builds Word files from openly licensed articles and runs each one through the whole pipeline.
Each article gets 4 citation modes (Zotero, EndNote, numbered, author-date) and Word quirks:
text boxes, equations, footnotes, a landscape section, bold-text headings, Unicode, merged table cells,
tracked changes, comments, and a LibreOffice round trip.

```
python tests/matrix.py <folder of JATS .xml> <work folder> --jobs 2 --libreoffice
```

It reports crashes, any change to locked text, tracked changes or comments that were not refused,
and facts that differ from the article's own XML. The files it makes come from real papers, so they stay
in the work folder and are never committed.

Last full run (2026-11-25): 36 CC BY articles, 1,512 files, no crashes, no locked-text changes,
no missed refusals. It found 5 real bugs, all fixed with tests:
- live Zotero and EndNote field citations were called "typed";
- a text box anchored in a heading hid that heading, so the abstract read 0 words;
- text after the Keywords line was counted as abstract;
- an abstract with no heading after it ran into the whole paper (now guessed, with a note);
- the sample converter put graphical abstracts and highlights under the Abstract heading.
