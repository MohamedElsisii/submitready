# Testing SubmitReady: the 2-minute start

Thank you for testing. SubmitReady prepares your manuscript for one journal in one command. It reads the journal's live guidelines, checks your references, fixes the layout, converts your figures and builds a to-do list, plus a sheet you can paste into the submission website. A text lock proves that not one word of your science changed.

## What you need

- **Claude Code**, signed in with a Claude Pro or Max plan. Run `claude update` first to get the latest version. If the start banner says "API Usage Billing", type `/login` and choose your Claude account, or every message is charged.
- **Python 3.10 or newer.** On first use, SubmitReady asks before installing the few packages it needs.
- **Git.** No GitHub account is needed.
- **A manuscript** in Word (.docx) or LaTeX. Keep a backup anyway, even though SubmitReady never changes your original.

## Install (once)

1. Start Claude Code and type these two commands:

   ```
   /plugin marketplace add MohamedElsisii/submitready
   /plugin install submitready@submitready
   ```

2. Restart Claude Code. You should now see `/submitready:submit` when you type `/submitready`.

To get new versions during the beta, type `/plugin marketplace update submitready`. Or turn on auto-update: type `/plugin`, open **Marketplaces**, pick SubmitReady and choose **Enable auto-update**.

## No journal yet?

```
/submitready:find my_paper.docx
```

It asks how you want to publish (open access, subscription or hybrid, a fee limit, MEDLINE) and shows 5 journals that publish papers like yours: fit, the impact factor as the journal states it, a free citation measure, median days from submission to acceptance, access model and fee. Pick one and it is prepared. If that journal rejects the paper, `/submitready:next my_paper.docx` prepares the next one on your list.

### Optional: real quartiles and Scopus status (5 minutes, once a year)

1. On scimagojr.com, open **Journal Rankings** and click **Download data**. You get a file like `scimagojr_2025.csv`.
2. Download the **Scopus Source List** (an Excel file) from Elsevier's Scopus content page.
3. In Claude Code, ask: *add my SCImago and Scopus files to SubmitReady*, with the two file paths.

The journal list then shows each journal's SJR quartile and Scopus status, leaves out titles Scopus discontinued, and can filter by "Scopus only" or "Q2 or better". The files stay on your computer; SubmitReady never shares them.

### Optional: let Claude read pages that need a browser (2 minutes)

Some publishers (Springer, BMC and others) block simple page readers, and many journals show their impact factor only after scripts load. With Chrome, Claude can read those pages in your own browser:

1. Install the free [Claude in Chrome](https://chromewebstore.google.com/detail/claude/fcoeoabgfenejglbffodgkkbkcdhcgfn) extension and sign in with the same Claude account as Claude Code.
2. Start Claude Code with `claude --chrome` (or run `/chrome` once and choose **Enabled by default**).

Claude only reads journal pages this way. It never signs in or submits anything for you.

## Run it

1. Put your manuscript in a folder. If you have them, put the figure files in a folder whose name contains "fig" and the supplementary files in a folder whose name contains "supp", next to the manuscript.
2. Open Claude Code in that folder and type, with your file and journal:

   ```
   /submitready:submit my_paper.docx PLOS ONE
   ```

3. Answer the yes/no questions. SubmitReady may offer to move a section the journal wants in its web form (like Funding) out of the submission copy, or to change labels like "Table S1" to "S1 Table". Nothing happens without your yes.

You get a folder named after your paper and the journal, with:

- `SUMMARY.md`: the verdict, what was done for you, and your to-do list grouped by where the work happens (Word, references, figure files, submission website)
- the formatted manuscript to upload
- `submission_form.md`: your title, abstract, keywords and statements, copied word for word, ready to paste
- `figures/`, `supplementary/` and `reports/`

Make your fixes in your **original** manuscript, then run the same command again.

## Please check two things

1. Open the formatted manuscript in Word and read a few paragraphs. Are they exactly as you wrote them?
2. Paste one box from `submission_form.md` into a text editor. Is it exactly your text?

## Send feedback

Open [a beta feedback form](https://github.com/MohamedElsisii/submitready/issues/new/choose) (a free GitHub account is needed to post). It takes about 2 minutes. Something broke? Use "Something went wrong" on the same page. No GitHub account? Send your answers to the person who invited you.

**Never attach an unpublished manuscript** to an issue. Describe the problem, or paste the message you saw.

## What leaves your computer

- SubmitReady's scripts run on your computer. Your original file is never changed. Everything is written to new files next to it.
- While it works, Claude reads parts of your manuscript and the reports, so that text is sent to Anthropic through Claude Code, as with anything else you open in Claude Code.
- To check references, their titles and DOIs are sent to Crossref and PubMed. The journal's guideline pages are read from the journal's website.
- SubmitReady itself collects no usage data.
