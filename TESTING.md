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

The last line should end with `passed`. Check the plugin too:

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
