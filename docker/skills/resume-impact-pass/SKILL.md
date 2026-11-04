---
name: resume-impact-pass
description: Second pass on a resume Fazil has already tailored with tailor-resume. Strengthens the existing company folder's resume in place - turns duty-style bullets into quantified achievements by asking him focused questions, sharpens bold/impact emphasis, and runs deep ATS and visual checks on the PDF. Use whenever he wants to polish, strengthen, quantify, or ATS-verify an existing tailored resume - "run the impact pass on X", "make the X resume stronger", "add metrics", "polish it", "check it's ATS-safe" - even if he doesn't name this skill. Not for creating a new tailored resume from a JD; that is tailor-resume.
---

# Resume Impact Pass

Improve a resume that `tailor-resume` has already produced. This skill **does not re-tailor**: the JD targeting, bullet selection, headline, and Skills order from the tailored version stay as they are unless a change is needed to fix an issue found here. It works on top of the existing files in the company folder.

Two jobs:

1. **Impact** - make the kept bullets show outcomes, not duties, using only facts Fazil confirms.
2. **Verification** - prove the PDF is ATS-readable and visually clean.

## Absolute rule: no fabrication

Same rule as `tailor-resume`, restated because this skill adds new facts:

- Every claim must be verifiable against `Backend_2026/Fazil_Backend_9Yrs.md` or confirmed by Fazil in this session. Never invent achievements, metrics, scope, technologies, employers, titles, or dates.
- **Request only numbers he can verify** (from dashboards, tickets, reports, or firm memory). No estimates, no rounding up. Keep qualifiers exact ("over", "~", "+").
- **A newly confirmed fact goes into the base `.md` first**, then into the tailored files. The base stays the single source of truth, so future `tailor-resume` runs get it too.
- Anything plausible but unconfirmed is **awaiting confirmation**: left out of the resume and listed in the report.
- Job titles, company names, dates, education, and contact details never change.

## Inputs

- **Company folder** - `<slug>/` at the Resume root (`/Users/fazil/Resume`). If he doesn't name it, list folders that contain a `Fazil_Resume.pdf` and ask which one. It must contain the tailored `.md`, the tailored `.tex`, `Fazil_Resume.pdf`, and `Job_Description.md`. If the tailored files are missing, stop and suggest running `tailor-resume` first.
- **Focus** (optional) - which roles or bullets he most wants strengthened. If not given, prioritize by JD relevance.

## Constraints inherited from tailor-resume

- Layout: the tailored `.tex` preamble, geometry, margins, fonts, and custom commands are never changed.
- **One page**, verified with `pdfinfo`. Trim content, never fonts or margins.
- The PDF stays named `Fazil_Resume.pdf` (`xelatex -jobname=Fazil_Resume`).
- Standard section headings, action-verb bullets, one tense per bullet (past roles past tense; CoinRoutes may be present).
- Hyphens, not em-dashes, in any prose.

## Model usage

All judgment (auditing bullets, choosing questions, rewriting, deciding trims, judging check results) stays in the main session - run it on Opus. Mechanical work (writing the `.tex` with escaping, compiling, running `pdfinfo`, `pdftotext`, `pdffonts`, `pdftoppm`) may be delegated to a `model: sonnet` subagent that returns tool output verbatim and never re-words or re-selects content.

## Steps

1. **Snapshot before changing anything.** Copy the current tailored `.md`, `.tex`, and `Fazil_Resume.pdf` into `<slug>/versions/vN/` (next unused N; the first pass creates `v1`). Add a `VERSION.md` there with the date and one line: "State before impact pass". Never modify or delete an existing `versions/` entry. This way a pass can always be rolled back.

2. **Read everything.** The tailored `.md` and `.tex`, `Job_Description.md`, and the base `.md`. Re-derive the JD's must-have keywords from `Job_Description.md` (the JD's exact wording), and note which ones the base resume supports - these are checked again in step 7.

3. **Audit every kept bullet.** For each one, check which of these it shows: **ownership** (what he personally drove), **problem** (why it mattered), **action** (what he did, with what tech), **beneficiaries** (who used it or gained), **scale** (volume, users, services, money), **result** (what changed). Classify:
   - **Strong** - action + a real result or scale. Leave it alone unless the wording is weak.
   - **Duty** - describes a responsibility with no outcome ("Responsible for...", "Worked on...", "Maintained..."). Candidate for a question.
   - **Weak wording** - real outcome but buried, vague verb, or passive voice. Fix by rewording only.

   Rank duty bullets by JD relevance. The top 3-5 go to step 4.

4. **Ask focused questions (the core of this skill).** Ask about the ranked duty bullets in one grouped message, a few bullets at a time, with 1-2 concrete questions each. Make questions specific enough to answer from memory. For example:

   > **CoinRoutes - "Built the order-routing service for institutional clients"**
   > - Roughly how many orders or how much notional volume did it handle per day?
   > - Did latency, fill rate, or error rate change after it shipped? From what to what?

   Good question targets: before/after measures, volume, adoption (clients, users, teams), revenue or cost, time saved, reliability (uptime, incidents), team scope (people led or mentored).

   Follow up once on vague answers ("a lot", "much faster") to get a number or a concrete outcome. If he can't give a reliable number, use a **concrete qualitative outcome** instead, e.g. "replacing a manual end-of-day reconciliation" - never an estimate. If he says to skip a bullet, skip it.

5. **Update the base, then rewrite.**
   - Add each confirmed fact to the matching bullet in `Backend_2026/Fazil_Backend_9Yrs.md` (and the base `.tex` bullet if one exists), changing nothing else there.
   - Rewrite the targeted bullets in the tailored `.md` in XYZ form where it fits (action verb, then outcome with the exact metric, then method), else action verb + concrete method + qualitative outcome. Lead with the strongest evidence. No inflated words ("revolutionized", "spearheaded massive").
   - Fix weak-wording bullets by rewording only.
   - **Bold only short measurable-impact spans**: a metric, performance change, scale, time or cost saving, or leadership scope (e.g. `\textbf{cut p99 latency from 40 ms to 12 ms}`). Technologies, verbs, dates, and responsibilities stay regular weight. Remove any existing bold on plain technology names. Never reword just to create a bold span.
   - **No keyword stuffing**: don't add JD keywords that aren't true, and don't repeat one term across many bullets.
   - Every changed line must trace to a base line or a fact confirmed in step 4.

6. **Rebuild.** Apply the rewritten bullets to the tailored `.tex` (content only; escape `&`, `%`, `$`, `#`, `_`, `~`). Confirm tools exist first: `xelatex`, and `pdfinfo`, `pdftotext`, `pdffonts`, `pdftoppm` from poppler (`brew install poppler` if missing); report any blocker. Compile twice:

   ```bash
   cd <slug> && xelatex -interaction=nonstopmode -jobname=Fazil_Resume <texfile>.tex
   ```

   Read the `.log`: fix errors, and treat `Overfull \hbox` warnings as real (text running into the margin).

   ```bash
   pdfinfo Fazil_Resume.pdf | grep Pages     # must read "Pages: 1"
   ```

   Stronger bullets are often longer. If it overflows, trim in this order: least JD-relevant bullets in the oldest roles, then tighten wording of the longest bullets, then shorten the Summary. Never cut a bullet that carries a must-have keyword or a newly confirmed metric before cutting ones that don't. If still over, ask Fazil.

7. **Deep ATS and visual verification.**
   - **Text extraction** - `pdftotext -layout Fazil_Resume.pdf -`. Check: name, email, phone, and LinkedIn read correctly with no junk characters next to them; the four section headings appear as plain text; each role's title, company, and dates stay together in order; every confirmed metric appears intact.
   - **Keywords** - search the extracted text (case-insensitive) for each supported must-have from step 2. Any missing one is a regression from the rewrite; restore it through a real bullet or the Skills line.
   - **Ligatures** - these glyphs silently break keyword matches:

     ```bash
     pdftotext Fazil_Resume.pdf - | grep -nE 'ﬁ|ﬂ|ﬀ|ﬃ|ﬄ'   # should print nothing
     ```

   - **Split words** - look for keywords broken across lines by auto-hyphenation (e.g. "micro-" / "services" on the next line).
   - **Fonts** - `pdffonts Fazil_Resume.pdf`: every font should be embedded (`emb` yes) with a Unicode mapping (`uni` yes). `uni` no means that text extracts as garbage.
   - **Links** - LinkedIn and email should appear as readable text in the extraction, not only as hidden link targets.
   - **Visual** - render and look at the page:

     ```bash
     pdftoppm -png -r 110 Fazil_Resume.pdf page    # writes page-1.png
     ```

     Check for clipped or overlapping text, a role heading stranded at the bottom away from its bullets, misaligned dates, uneven spacing, and bold overuse (more than about one bold span per bullet reads as noise).

   Fix content-level problems here. Problems caused by the preamble (icons, ligatures, hyphenation, fonts) are **not** fixed in the tailored copy - record them for the report with the matching fix from "Base template ATS audit" below.

8. **Record the version.** Update `<slug>/versions/vN/VERSION.md` to add "Impact pass applied after this version: <one-line summary of what changed>". If `Gap_Analysis.md` exists, update only its **After** score line using the same rubric as `tailor-resume` (must-have found x6, nice-to-have found x2, domain fit 0-2), measured on the new PDF text.

9. **Report.**
   - **Changed bullets** as a short before/after list, with the source of each new fact ("confirmed by you" or "base resume").
   - **Base resume updates** - which bullets in `Fazil_Backend_9Yrs.md` gained new facts.
   - **Awaiting confirmation** - anything left out because it couldn't be confirmed.
   - **Check results** - pass/fail for page count, extraction, keywords, ligatures, fonts, links, and the visual check.
   - **Base template fixes recommended** (if any), from the audit below.
   - **Stale documents** - if `Outreach_Note.md` exists and quotes metrics or bullets that changed, say so; don't rewrite it unless he asks.
   - Where the snapshot is (`versions/vN/`) in case he wants to roll back.

## Base template ATS audit (reference)

Fixes for `Backend_2026/Fazil_Backend_9Yrs.tex` itself. Recommend the matching one when step 7 finds the problem. Only edit the base preamble when Fazil explicitly asks, and never in a tailored copy.

- **Icons in the contact line** (`fontawesome5`) extract as junk -> replace them with plain text labels or separators (e.g. `Dubai, UAE | +971 ... | email | linkedin.com/in/fazil-ma`), with links showing their real text.
- **Ligatures** break keywords -> disable common ligatures on the main font, e.g. `\setmainfont{...}[Ligatures=NoCommon]` with fontspec.
- **Words split across lines** break keyword matches -> stop automatic hyphenation with `\hyphenpenalty=10000` (explicit hyphens like "low-latency" are unaffected).
- **Multi-column or tabular content** scrambles reading order -> keep one text column; right-aligned dates via `\hfill` are fine as long as `pdftotext` keeps them on the role's line.
- **Role heading separated from its bullets** -> keep the heading with its first bullet (e.g. `\needspace` or `\nopagebreak` in the role macro).
- **Font without Unicode mapping** -> switch to a font that `pdffonts` reports with `uni` yes.

## Non-negotiables (quick reference)

- Snapshot to `versions/vN/` before touching anything; never modify old versions.
- New facts only from Fazil's confirmation, added to the base `.md` first. No estimates.
- Don't re-tailor: keep the JD targeting from `tailor-resume`.
- One page, `Fazil_Resume.pdf`, preamble untouched.
- Bold measurable impact only; no keyword stuffing.
- Every run ends with the full step 7 checks and the report.
