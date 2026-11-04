---
name: "tailor-resume"
description: Tailor Fazil's resume to a specific company and job description as an ATS-friendly, one-page PDF. Use whenever the user shares a job description (JD), a job posting URL, or a company + role and wants a resume, CV, or application for it - including "tailor my CV", "apply to X", "here's a JD", "customize my resume for this role" - even if he doesn't say "tailor". Creates a company folder, copies the base Markdown resume from Backend_2026, and generates a JD-tailored LaTeX (.tex) resume compiled to Fazil_Resume.pdf, with optional gap analysis, outreach note, and cover letter.
---

# Tailor Resume

Generate a company- and JD-tailored, ATS-friendly resume for Fazil Meethale Anandi. Two goals, in priority order:

1. **ATS-readable and keyword-matched** - an applicant tracking system can extract the PDF's text cleanly and finds the JD's keywords that Fazil genuinely has.
2. **Relevant first** - a human reader sees the most JD-relevant real facts first.

Both goals are always subject to the no-fabrication rule below.

## Absolute rule: no fabrication

**Never invent, imply, or embellish.** Every word in the headline, summary, skills, and bullets MUST be factually verifiable against the base sources (`Backend_2026/Fazil_Backend_9Yrs.md` / `.tex`). You may only re-order, re-weight, and re-word facts that already exist there.

- **Do not add skills, tools, languages, frameworks, or cloud services that are not already present in the base resume** - even if the JD asks for them. If the JD wants a technology Fazil doesn't have, leave it out; do not add it, hint at it, or imply adjacent experience.
- Do not inflate scope, seniority, metrics, dates, team sizes, or impact. Keep every number exactly as stated in the base resume, including qualifiers like "over", "~", and "+".
- Do not imply experience by clever wording (e.g. listing a JD keyword next to a real skill so it reads as owned).
- **Never change job titles, company names, employment dates, education, or contact details** - not even to match the JD's job title. These must survive a background check; keyword matching never justifies altering them.
- When unsure whether a claim is supported, leave it out and, if it matters for the JD, tell Fazil rather than guessing.

## ATS-friendly experience bullets (XYZ format)

**Prefer XYZ, but it is not mandatory.** XYZ = _Accomplished **[X]**, measured by **[Y]**, by doing **[Z]**_: lead with a strong action verb, then the result/impact, then the method or tech used. Reach for this shape first on every bullet. Some real facts don't fit it - a responsibility, an integration, or a capability with no natural result/metric - and forcing them into XYZ produces awkward or padded phrasing. When XYZ doesn't fit cleanly, fall back to a strong action-verb + concrete-method bullet. Never invent a result or metric just to satisfy the pattern.

**The metric (Y) must already exist in the base resume.** Never invent, estimate, or round a number to complete the pattern. Handle the two cases honestly:

- **Bullet already has a metric** - keep the number exact (with its qualifiers) and make it the Y. Reorder so the impact leads.
  - Base: _"Converted inter-service communication from WebSocket to low-latency IPC using gRPC and Protobuf wherever feasible, cutting inter-service latency by over 30% from a ~35 ms baseline."_
  - XYZ: _"**Cut inter-service latency by over 30%** (from a ~35 ms baseline) by converting WebSocket communication to low-latency gRPC/Protobuf IPC."_
- **Bullet has no metric** - do NOT fabricate one. Use the X-by-Z form (action + concrete method/scope), keeping any real qualifier already present (e.g. "5 backend microservices", "sub-100 ms", "1M+ users").
  - Base: _"Built reactive, non-blocking services using Spring WebFlux for high-concurrency request handling."_
  - XYZ: _"**Built reactive, non-blocking services with Spring WebFlux** to handle high-concurrency request loads."_

**ATS parsing rules (the layout source already satisfies these - do not break them):**

- Keep the standard section headings (`Professional Summary`, `Skills`, `Work Experience`, `Education`) - ATS parsers key off them.
- Start each bullet with an action verb; no first-person pronouns, tables, columns, text boxes, or graphics.
- Use the JD's exact phrasing **only where it names a skill Fazil genuinely has** (e.g. write "REST APIs" instead of "RESTful services" if that's the JD's term). This is keyword matching, not fabrication - the underlying fact must already be true.
- Spell out an acronym once with its abbreviation when the JD uses one form and the base resume the other (e.g. "role-based access control (RBAC)"), so both forms are searchable.
- One verb tense per bullet: past roles in past tense; the current role (CoinRoutes) may use present tense.
- **No keyword stuffing.** Each JD keyword should appear where it is genuinely true - usually once in Skills and once in a real bullet. Don't repeat a term across many bullets or cram the Skills line; modern ATS tools and recruiters both penalize it.

## Inputs

- **Company name** (required) - used to name the output folder.
- **Job description (JD)** (required) - the text of the role. If not provided, ask for it (or a URL / key requirements).
- **Additions / highlights** (optional) - anything Fazil wants added or emphasized for this JD (a project, skill, metric, role, or angle). Ask for this up front alongside the scope question if he hasn't said. Honor it by reordering and re-weighting so the requested item leads. It **never overrides the no-fabrication rule**: if he asks to feature something not in the base sources, tell him it isn't in the base resume and offer to add it there first, then re-tailor.

## Scope: ask what to generate (do this first)

Once you have the company name and JD, and before any tailoring work, ask Fazil which outputs he wants using the `AskUserQuestion` tool (multi-select):

- **Tailored resume** (`.tex` + one-page `Fazil_Resume.pdf`) - the core deliverable; effectively always wanted.
- **Gap analysis** (`Gap_Analysis.md`, step 10) - optional.
- **Outreach note** (`Outreach_Note.md`, step 11) - optional.
- **Cover letter** (appended to `Outreach_Note.md`, step 11) - optional; never a separate file.

**Default to resume only** unless he selects an extra. If he already stated the scope (e.g. "just a quick resume", "resume + cover letter"), honor it and don't ask again. Ask about additions/highlights in the same question round. The JD is always saved verbatim (step 2) regardless of scope.

## Base sources (do not edit these)

- **Format source (authoritative): `Backend_2026/Fazil_Backend_9Yrs.tex`** - defines the exact layout, margins, geometry, fonts, section styling, and custom LaTeX commands. Always start from a copy of this `.tex`; never introduce a different layout.
- **Content source: `Backend_2026/Fazil_Backend_9Yrs.md`** - the canonical content (all roles, full bullet lists). It intentionally holds more bullets than fit one page: treat it as a superset to select from.
- Prior tailored examples for reference: `alpha_data/`, `big_data/`, `open-innovation/`.

All paths are relative to the Resume root (`/Users/fazil/Resume`).

## Model usage (reasoning vs. execution)

**High-end model = Opus.** Refer to models by alias (`opus`, `sonnet`) rather than pinned version IDs so this section doesn't go stale.

The skill cannot switch the model of the main session - the main reasoning runs on whatever model the session was started with, so run this skill in an Opus session. The only model the skill controls is the subagent's.

- **All reasoning stays in the main session:** JD keyword extraction, choosing what to foreground, rewording bullets, the headline and Summary, the source check, keyword coverage judgment, match scores, gap analysis, outreach/cover-letter prose, and **deciding what to trim** when the PDF overflows.
- **Delegate only mechanical execution to a `model: sonnet` subagent** (Agent tool): write the finalized `.tex` with correct LaTeX escaping, run `xelatex -jobname=Fazil_Resume`, run `pdfinfo` and `pdftotext`, and return their outputs verbatim. Pass it the finalized text **verbatim** - it formats and compiles; it does not re-word, re-select, or trim content. On overflow or a failed check it reports back; the main session decides the fix.

Steps 3, 4, 5, 9 (judgment part), 10, 11 are reasoning; steps 6-8 and the command part of step 9 are execution.

## Steps

1. **Derive a folder slug** from the company name: lowercase, spaces/punctuation to hyphens or underscores (e.g. "Open Innovation" -> `open-innovation`, "Alpha Data" -> `alpha_data`). If the folder already exists, reuse it and overwrite the generated files (confirm with Fazil first if it contains unrelated content).

2. **Create the folder** `<slug>/` at the Resume root and **save the JD verbatim** to `<slug>/Job_Description.md` (the raw text as given, or extracted text if a URL was provided). Do not edit or summarize it.

3. **Extract JD keywords.** Build two lists from the JD, using the JD's exact wording for each term:
   - **Must-have** - anything marked required, essential, "you have", "must", or listed under core requirements.
   - **Nice-to-have** - preferred, bonus, "plus", "familiarity with".

   Mark each term against the base resume:
   - ✅ **supported** - the base resume shows it. If the base uses different wording (e.g. JD "event streaming", base "Kafka pipelines"; JD "REST APIs", base "RESTful services"), note both - these are the biggest ATS wins, because the skill is real but a literal keyword search would miss it.
   - ❌ **not in base resume** - never goes into the resume. It feeds the gap analysis if requested.

   The ✅ terms drive everything that follows: what leads the headline, Summary, and Skills, and which bullets are kept.

4. **Tailor the Markdown.** Copy the base `.md` into the folder, named after the role focus, e.g. `Fazil_<CompanyOrFocus>.md` (mirror `open-innovation/Fazil_OpenInnovation_MLInfra.md`). Then edit the copy:
   - **Headline** (the title line under the name, if present) - reposition toward the JD using only real domains and strengths. E.g. for a payments role, lead with distributed systems and payments/remittance rather than trading. Never turn it into the JD's exact job title if that isn't a real title Fazil has held.
   - **Professional Summary** - lead with the JD's top ✅ must-haves, using Fazil's real background.
   - **Skills** - reorder so the JD's ✅ stack appears first (a Python/Django role leads with Python; a Java role leads with Java). Use the JD's exact term where it names a skill he has. **Never add a skill that isn't in the base resume.**
   - **Bullets** - for each role, keep only the bullets whose content the JD calls for, ranked by relevance, and rewrite each in ATS form (see above). Recent, on-target roles keep more bullets; older or off-target roles (Wipro, Al Ansari) drop to the one or two most relevant. Never pad a role with irrelevant bullets to fill space.
   - Keep all six roles and the education section.

5. **Source check (before any LaTeX).** Build a table mapping every tailored bullet, every Summary claim, and the headline to the base-resume line it comes from:

   | Tailored text | Source in base `.md` |
   |---|---|
   | Cut inter-service latency by over 30%... | CoinRoutes, bullet on gRPC/Protobuf IPC |

   Remove anything with no source row. Check every number and qualifier matches the base exactly. If a claim is plausible but you can't pin it to a base line, don't guess - leave it out and list it in the report as **awaiting confirmation**. This table is a working check; in the final report, only mention items that were removed or changed because of it.

6. **Generate the tailored `.tex`** by copying `Backend_2026/Fazil_Backend_9Yrs.tex` and editing only the content - **never change the preamble, geometry, margins, `\titleformat`, or custom commands**:
   - Apply the headline, Summary, Skills, and bullets from step 4.
   - Bold (`\textbf{...}`) only short spans that show **measurable impact**: a metric, a performance change, scale, time or cost savings, or leadership scope (e.g. `\textbf{cut inter-service latency by over 30\%}`, `\textbf{1M+ users}`). Keep technologies, action verbs, dates, and responsibilities in regular weight - the ATS finds keywords regardless of bold, and bolding tech names dilutes the impact spans a recruiter skims for. Never reword a bullet just to create something to bold.
   - Keep the name, contact line, and education exactly as in the base.
   - Escape LaTeX special characters in new text: `&` -> `\&`, `%` -> `\%`, `$`, `#`, `_`, `~` (use `\textasciitilde{}`), etc.

7. **Compile to PDF** with xelatex (required for `fontawesome5` + native unicode). The PDF is **always** named `Fazil_Resume.pdf`; the `.md` and `.tex` keep their descriptive names:

   ```bash
   cd <slug> && xelatex -interaction=nonstopmode -jobname=Fazil_Resume <texfile>.tex
   ```

   Before the first compile, confirm the tools exist: `xelatex`, `pdfinfo`, and `pdftotext` (the last two from poppler; `brew install poppler` if missing). If something can't be installed, report the specific blocker.

   Run twice if needed for layout. Read the `.log` after each run: fix errors, and treat `Overfull \hbox` warnings as real problems (text running into the margin), not noise.

8. **Enforce ONE PAGE (hard requirement).**

   ```bash
   pdfinfo Fazil_Resume.pdf | grep Pages     # must read "Pages: 1"
   ```

   (Fallback: `mdls -name kMDItemNumberOfPages Fazil_Resume.pdf`.) If 2+ pages, trim in this order and recompile after each change:
   1. Remove the least JD-relevant bullets, oldest roles first (Wipro, then Al Ansari, then forward).
   2. Shorten the Summary.
   3. Reduce the oldest roles to their title / company / dates line with **no bullets** (Wipro first, then Al Ansari).
   4. If it still overflows, stop and ask Fazil what to cut.

   Never drop a role entirely, never shrink fonts, and never change margins/geometry. Prefer cutting bullets that carry no ✅ must-have keyword.

9. **ATS text-extraction and keyword coverage check.** An ATS reads the PDF's extracted text, not its layout. Check it:

   ```bash
   pdftotext -layout Fazil_Resume.pdf -
   ```

   (`pdftotext` ships with poppler, the same package as `pdfinfo`; `brew install poppler` if missing.) Verify in the output:
   - **Contact details** - name, email, phone, and LinkedIn read correctly, with no junk characters next to them (icon fonts sometimes extract as garbage).
   - **Headings** - all four section headings appear as plain text.
   - **Words intact** - no broken ligatures (e.g. "configured" extracting as "con gured" or "conﬁgured"), which would fail keyword searches.
   - **Order** - each role's title, company, and dates stay together and in sequence.

   Then search the extracted text (case-insensitive) for every ✅ keyword from step 3. A ✅ **must-have** missing from the text is a tailoring miss: work it in through a real bullet or the Skills line, recompile, and re-verify one page. A ✅ nice-to-have may be left out if space doesn't allow it.

   If extraction is broken because of the preamble (icons, font ligatures, layout), **do not edit the preamble in the tailored copy** - report it to Fazil as a base `.tex` fix (the `resume-impact-pass` skill lists the specific fixes), since it affects every tailored resume.

10. **Gap analysis (only if requested) - write `<slug>/Gap_Analysis.md`.** A private self-assessment; **never paste any of it into the CV, cover letter, outreach note, or LinkedIn.** Start with:

    ```markdown
    # Gap Analysis - <Company> (<Role>)

    _Private reference for Fazil. Do NOT paste any of this into the CV, cover letter, or LinkedIn - it is a self-assessment only._
    ```

    Then include, in order:
    - **Match Score: X -> Y / 10 - verdict: strong / stretch / reach.** Score with this rubric so numbers are comparable across companies:
      - Must-have keywords found: (found / total must-haves) x 6
      - Nice-to-have keywords found: (found / total nice-to-haves) x 2
      - Domain and seniority fit: 0-2 (2 = same domain and level, 1 = adjacent, 0 = unrelated)

      **Before** = keywords found literally in the base `.md` text. **After** = keywords found in the tailored PDF's `pdftotext` output. Round to one decimal. Verdict: 8+ strong, 6-7.9 stretch, below 6 reach. Add an **honest ceiling** line: the score if every ✅ term appeared, and which ❌ terms cap it.
    - **Strong points (with CV evidence)** - 3-5 areas where his real experience directly meets the JD, each citing the role/metric.
    - **Gaps** - the ❌ terms from step 3 (only if genuinely absent, not present under another name):
      - **🔴 Critical** - must-haves he lacks: core language, framework, or years-in-domain.
      - **🟡 Important** - preferred / strongly preferred items.
      - **🟢 Minor** - buzzwords, tooling, or soft requirements with little bearing on shortlisting.

      For each, name the JD requirement and, where useful, the closest adjacent experience (context only - never written into any document).
    - **Action before applying** - if any 🔴 gap is actually true and merely missing from the base resume, add it to `Backend_2026/Fazil_Backend_9Yrs.md` first and re-tailor. Never add it only to the tailored copy.

    If there are no meaningful gaps, still write the file and say so.

11. **Outreach note and/or cover letter (only if requested) - write `<slug>/Outreach_Note.md`.** Grounded only in real experience, hyphens not em-dashes. Start with `*Paste into a LinkedIn message, email body, or application form. Grounded only in real experience. Hyphens, not em-dashes.*`, then include only the selected sections.

    **Tone: humble and understated.** Let the facts and metrics do the work. No boasting or total-coverage claims ("that is essentially your stack, end to end", "I check every box"). Prefer measured framing ("there is a good overlap with the stack in your posting"). When in doubt, understate.

    **Outreach note** - two versions:
    - **LinkedIn / short (~90 words)** - greeting to `[Name]`, one line on the role, 2-3 sentences of his most JD-relevant real experience (lead with a headline metric like sub-100 ms), a light call to action. Sign off "Best, Fazil".
    - **Email (~130 words)** - a `Subject:` line, 3-4 concrete real achievements across roles, "My CV is attached.", and a sign-off with `<mobile number from Backend_2026/Personal_Details.md> | linkedin.com/in/fazil-ma`.

    **Cover letter** - under a `## Cover Letter` heading (~250-350 words, four short paragraphs):
    - **Opening** - the role and company by name, and the single strongest reason he fits.
    - **Body (1-2 paragraphs)** - 3-4 achievements mapped to the JD's top requirements, each traceable to a base-resume bullet with its exact metric.
    - **Close** - why this role (based only on the JD text - never invent knowledge of the company), plus a call to action.
    - Address to `[Hiring Manager]` when no name is known; sign off `Sincerely, Fazil Meethale Anandi` then `<mobile number from Backend_2026/Personal_Details.md> | linkedin.com/in/fazil-ma`.

    Foreground the same JD keywords the CV emphasizes, never introduce a skill the CV doesn't contain, and **never pull anything from `Gap_Analysis.md`**.

12. **Report (last step).** Tell Fazil:
    - The folder and the outputs actually generated: `Job_Description.md`, the tailored `.md`, the `.tex`, the one-page `Fazil_Resume.pdf`, and (only if requested) `Gap_Analysis.md` and `Outreach_Note.md` (naming which sections were written).
    - A short note on what was emphasized for this JD, including how the headline was positioned.
    - **Keyword coverage**, from step 9:

      ```
      Must-have:     Java ✅  Kafka ✅  microservices ✅  Kubernetes ❌ (not in base resume)
      Nice-to-have:  gRPC ✅  Redis ✅  Terraform ❌ (not in base resume)
      ```

    - Anything removed by the source check (step 5), any facts **awaiting confirmation**, and any extraction problem that needs a base `.tex` fix (step 9).
    - One closing line: for stronger, quantified bullets and a deeper ATS/visual check, the `resume-impact-pass` skill can run on this folder next.

## Non-negotiables (quick reference)

- No fabrication: only facts, skills, and exact numbers from the base resume. Titles, companies, dates, education, and contact details never change.
- One page, verified with `pdfinfo`. Trim content, never fonts or geometry.
- ATS check with `pdftotext` every run; every ✅ must-have keyword should appear in the extracted text. Deeper checks (fonts, ligatures, visual render) live in `resume-impact-pass`.
- Bold measurable impact, not technologies. No keyword stuffing.
- PDF is always `Fazil_Resume.pdf`; the `.md` and `.tex` carry company-specific names.
- Base sources and the preamble are never edited during a tailoring run.
