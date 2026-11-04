# Job agent

Collects jobs every morning, removes duplicates, scores each one against your resume,
tailors a resume for the strong matches, and shows everything in a local dashboard.

```
daily (Docker or launchd) ─► collectors ─► dedupe ─► rule filter ─► scorer (Claude) ─► tailor (Claude) ─► SQLite ─► dashboard
                         LinkedIn, Bayt, NaukriGulf (Apify)
                         career pages (Greenhouse / Lever / Ashby / SmartRecruiters)
                         UAE recruiters: Hays, Michael Page, Charterhouse, Halian, Salt,
                           Marc Ellis, Discovered, Umatr, Jex Digital (plain HTTP)
                         Remote boards: Himalayas, Remotive, Working Nomads, NoDesk,
                           Jobspresso, Remote Rocketship (optional, paid)
                         Open web: company sites and smaller boards (Agent Reach)
```

Only the scorer and tailor use Claude, through the Claude Code CLI you're already
logged into. Everything else is plain Python.

## Setup (once)

```bash
cd ~/Projects/job-agent
brew install uv                 # if you don't have it
uv sync                         # creates .venv and installs deps
cp .env.example .env            # then paste your Apify token into .env
```

Then check `config.yaml`:

1. `paths.resume_workspace` is `/Users/fazil/Resume` and `paths.base_resume` is
   `Backend_2026/Fazil_Backend_9Yrs.md` inside it. Change both if your Resume folder moved.
2. `application_profile`: notice period, work authorization, salary expectation.
   These go into each job's `application_answers.md`.
3. `tailor.extras`: add `gap_analysis`, `outreach_note` or `cover_letter` if you want
   the skill to write those too. The default is the resume only.

### Tailoring prerequisites

Tailoring runs the `anthropic-skills:tailor-resume` skill through the Claude Code CLI
(`claude -p`, model Sonnet) inside `/Users/fazil/Resume`, one job at a time. It needs:

- `claude` on your PATH and logged in, with the tailor-resume skill visible to it.
  Test once by hand: `cd ~/Resume && claude` and ask "which skills do you have?".
- `xelatex` (MacTeX) and `pdfinfo` / `pdftotext` (`brew install poppler`).

The run is unattended, so the prompt tells the skill the scope (resume only) and a
folder name per job (`<company>_<role>`), and blocks its "ask the user" tool. To keep
Claude usage down, the agent itself creates the folder and saves `Job_Description.md`,
and Claude writes the tailored `.tex` once (no tailored `.md`, no verification scorecard,
no `Tailoring_Log.md` row). Each job folder ends up with `Job_Description.md`, the
tailored `.tex`, `Fazil_Resume.pdf`, `application_answers.md` (Claude's four short answers
plus your fixed fields from `application_profile`) and `skill_report.md` (Claude's
report, including keyword coverage). For the full manual flow (Markdown copy, scorecard,
log), run the skill yourself in Claude Code.

### Impact pass and your clarifications file

Each tailoring also applies the `anthropic-skills:resume-impact-pass` skill in the same session,
to the resume content before the `.tex` is written and compiled once: weak bullets are
reworded and bold is kept to real impact. The PDF then gets text-only checks (`pdfinfo`, `pdftotext`,
`pdffonts`); no page images are rendered, to keep Claude usage down. It never asks you
anything during a run.

Instead, the questions it would have asked (real numbers behind a bullet) are collected in
one shared file: **`/Users/fazil/Resume/Impact_Questions.md`**. Each bullet appears once,
however many jobs raised it. Answer under **Answer:** whenever you have time (`SKIP` if
there's no reliable number). Every tailoring after that uses your answers as confirmed
facts. To make them permanent for every future resume, add them to
`Backend_2026/Fazil_Backend_9Yrs.md` (or run the impact-pass skill on one folder
interactively; it does that for you).

Check the career-page list:

```bash
uv run jobagent check-companies
```

Fix or delete any company marked `not found`. The token is the slug in the company's
careers URL, e.g. `boards.greenhouse.io/<token>`, `jobs.lever.co/<token>` or
`jobs.ashbyhq.com/<token>`.

### Recruitment agencies

The `agencies` source reads nine UAE recruiters with plain HTTP requests and HTML
parsing: no browser and no Apify. It's adapted from
[wazifa](https://github.com/ameer-khan-ashraf/wazifa) (MIT License, © 2026 Ameer Khan).
Agencies rarely name the client, so the company shows as e.g. "Hays (recruiter)",
or as the real company when the job page names it. For new jobs whose title passes
your filter, the job page is read once to get the full description.

These scrapers depend on each site's page layout, so test them now and then:

```bash
uv run jobagent check-agencies          # all nine; or e.g. `check-agencies hays salt`
```

A site returning 0 jobs usually means its layout changed.

### Open-web search

`sources.web_search` searches the web through the [Agent Reach](https://github.com/Panniantong/Agent-Reach)
CLI, with no login, and finds postings on sites no other source reads. A page counts only if it has
schema.org job data posted in the last `max_age_days`. The Docker image includes Agent Reach; on the
Mac, install it yourself or the source is skipped. Search runs through Exa's free tier, so a rate limit
can end a run's searching early, and the last queries are the ones dropped.

These pages can come from anywhere, so their text is untrusted. They are never tailored automatically:
click Tailor to run one, and that run has no web access. Their text never replaces a description you
already have, and the dashboard shows the site name next to the link. Check it before you apply.

### Remote boards

Himalayas, Remotive, Working Nomads, NoDesk and Jobspresso are read through their free
public APIs and feeds. Each job's location shows who it's open to, e.g. "Remote (Worldwide)"
or "Remote (USA Only)", so your location rule drops the ones you can't take from Dubai.
Himalayas is searched only for jobs open to candidates in the UAE.

Remote Rocketship has a paid API ($29/month). It stays off until you put
`REMOTEROCKETSHIP_API_KEY=...` in `.env`. Remote OK and We Work Remotely are switched off
in `config.yaml` (`enabled: false`); set them back to `true` to use them again.

## Run it in Docker (recommended)

One container holds everything: the dashboard, the daily run, Claude Code, LaTeX and
poppler. It restarts with Docker Desktop, and a run missed while the Mac was asleep or
Docker was closed happens as soon as it's back (once a day).

**1. Get a Claude token (once a year).** On the Mac:

```bash
claude setup-token
```

It opens the browser, then prints a token. It uses your Claude plan, not an API key.
Paste it into `.env`:

```
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
```

**2. Build and start.**

```bash
cd ~/Projects/job-agent
uv run jobagent unschedule        # remove the old launchd job, so it doesn't run twice
docker compose up -d --build      # first build takes 10-20 min (LaTeX is big)
```

The dashboard is at http://localhost:8765. The run happens every day at 07:30 Dubai time
(change `JOBAGENT_DAILY_AT` in `docker-compose.yml`, then `docker compose up -d`).

**3. Check it once.**

```bash
docker compose exec jobagent claude -p "say ok"                        # Claude login works
docker compose exec jobagent jobagent run --only career_pages --no-tailor   # a cheap run
docker compose exec jobagent jobagent tailor <job id>                  # one full tailoring
```

What lives where:

| On the Mac | In the container |
|---|---|
| `.env`, `data/`, `logs/` | `/app/...` (same files) |
| `config.yaml` | `/host/config.yaml` (the project folder, read-only) |
| `/Users/fazil/Resume` | `/Users/fazil/Resume` (same path, so resume paths match) |
| `docker/skills/` | Claude Code's skills folder |

Other commands work the same way: `docker compose exec jobagent jobagent <command>`.
Logs: `docker compose logs -f`, plus `logs/scheduled_run.log` for each daily run.
Stop with `docker compose down`. After changing the code, `docker compose up -d --build`.
Edits to `config.yaml` apply from the next run, without a rebuild.

Notes:

- **Skills.** The container uses its own copies of `tailor-resume` and `resume-impact-pass`
  in `docker/skills/`, since the token can't download your claude.ai skills. After you
  change either skill on claude.ai, open Claude Code on the Mac once, then run
  `./docker/update-skills.sh`. No rebuild needed.
- **Apply button.** The container can't open Finder, so the job's details show an
  **Open resume PDF** button instead, and the toast after Apply shows where the PDF is.
- **One copy at a time.** Don't run `uv run jobagent serve` on the Mac while the container
  is up; both would write the same database.
- **Token expiry.** The token lasts a year. When Claude calls start failing with a login
  error, run `claude setup-token` again, update `.env`, then `docker compose up -d`.

## Daily use (without Docker)

```bash
uv run jobagent run              # the daily pipeline (collect -> score -> tailor)
uv run jobagent serve            # dashboard at http://127.0.0.1:8765
uv run jobagent schedule         # run every day at 07:30 via launchd
uv run jobagent unschedule       # remove that launchd job
```

Useful flags: `jobagent run --only career_pages remoteok --no-tailor` for a cheap test,
`jobagent tailor <job id>` to tailor one job by hand.

## System page

Click **System** at the top of the dashboard (or open http://localhost:8765/system) to see:

- **Daily run**: the schedule, when the next run is, whether one is running now, and buttons
  for **Run now**, **Collect + score** (no tailoring) and **Collect only** (no Claude at all).
- **Log**: the live output of scheduled runs, Run-now runs, or all activity. It refreshes
  every few seconds while a run is going.
- **Run history**: the last 30 runs: how long each took, jobs found, new, filtered out, scored,
  tailored, and which sources failed. Click a run for a per-source breakdown.
- **Claude**: which models score and tailor, how many resumes are ready, queued or failed (with
  the error), plus usage per day and model and the most expensive tailorings.
- **Apify budget**: spend this cycle and today's per-search budget (click **Check now**).
- **Impact questions**: how many are waiting for your answer.
- **Maintenance**: buttons for `refilter`, `rescore`, `check-companies`, `check-agencies` and
  `apify-usage`, with their output on the page.

## Time limits and the circuit breaker

Every step gets a time limit learned from its own history: **3 times its usual time** (the
median of its recent successful runs; the median, so one freak slow run doesn't stretch it).
For example, if a source usually takes 60s, it's cut off after 3 minutes, not 15. Minimums
keep this safe: 2 minutes per source, 10 minutes for the Apify sources (LinkedIn, Bayt,
NaukriGulf; stopping them early wouldn't stop the paid search on Apify), 2 minutes per scoring
call and 15 minutes per tailoring.

- A source that **fails 3 runs in a row** is skipped for 48 hours, then tried once more. One
  success puts it back to normal. The System page shows skipped sources as "off".
- If a scoring call times out, scoring stops for that run; the rest is scored next run.
- If **2 tailorings in a row** time out, tailoring stops for that run.
- Times are measured with a clock that stops while the Mac sleeps, so sleep never cuts a step
  off. The run history shows how long the Mac was asleep during each run ("asleep 2h").

Tune it in `config.yaml` under `run`: `timeout_factor`, `circuit_breaker_failures`,
`circuit_breaker_hours`.

## Company career boards: Workable search and discovery

Greenhouse, Lever and Ashby have no "search all companies": a company's board can only be read
once its name on the platform is known (the `companies` list in config.yaml). Two things widen that:

- **Workable** (`sources.workable`) is searched across every company that uses it, through
  Workable's public job site (jobs.workable.com), by keyword and location (UAE by default).
  Many UAE startups and mid-size companies hire through it. Free, no login.
- **Discovery** (`sources.discover`): after each run, up to 40 UAE employers seen on LinkedIn,
  Bayt and the other boards (busiest first, recruitment agencies skipped) are checked for a
  Greenhouse, Lever or Ashby board. A board counts only if it lists a UAE job right now, so a
  same-named company elsewhere isn't picked up. Found boards are read every run with the
  configured companies; misses are re-checked after a month. The System page's run details
  show what was found.

Not covered: Workday, SAP SuccessFactors and Oracle (used by many large UAE employers). Their
setups differ per company and often block automated reading; add those one employer at a time.

## LinkedIn without Apify (public search)

The `linkedin_public` source reads LinkedIn's public job search, the one a logged-out visitor
sees: free, no login, no Apify credit. Copy a LinkedIn search link from your browser into
`sources.linkedin_public.urls` in `config.yaml`; its filters (keywords, place, date posted,
remote, experience level) are reused. Your personal "how you fit" list can't be reproduced,
since that needs your login, and logged-in scraping risks your LinkedIn account.

LinkedIn blocks fast requests ("429 too many requests"), so requests are spaced out
(`delay_seconds`), it pauses once if told to slow down, and otherwise stops for the day with
what it has. Full descriptions are read only for jobs that are new and pass your title rules
(`details_per_run` per run). It runs alongside the Apify LinkedIn source; matching jobs merge.

## Add a job by link

Click **+ Add job** in the dashboard header and paste a job link, e.g. a LinkedIn job the daily
searches missed (`linkedin.com/jobs/view/…`, or a search link with `currentJobId=`), or any
career-site or job-board page. The job is read, merged with any copy already collected (so a
Bayt job then shows its LinkedIn link), and scored by Haiku in the background (~20s). You chose
it, so the filter rules don't drop it. If the page can't be read (blocked, login wall), the box
asks you to paste the title, company, location and description instead.

## Duplicates across sources

The same job found on several sites is one row (matched on company + title + location, or the
same URL). Title extras that only say where or how ("- Dubai", "(Remote)", "| Full-time",
"(m/f/d)", "Job ID 123") are ignored, but the specialty part is kept, so
"Senior Software Engineer - Java Full Stack" and "Senior Software Engineer - .NET" stay separate. The card shows the best copy's link: the company's own career page first, then
LinkedIn, then Bayt, NaukriGulf, agencies and remote boards. The chips list every site that had it.

LinkedIn, Bayt and NaukriGulf searches share one limit on Apify (`apify.max_concurrent_runs`,
4; the free plan allows 5 at a time), with LinkedIn first in line. If Apify still says it's
full, the search waits and tries again instead of failing.

## AI-training gigs and excluded companies

Contract work that trains or evaluates AI models (G2i "Senior Software Engineer, AI Training",
Xperteez, micro1, Askable and the like) is kept but tagged **AI gig**. Tagged jobs:

- never appear under **Only strong matches**, whatever their score;
- are never tailored automatically (click Tailor if you want a resume for one);
- are listed on their own with the **AI gigs** toggle in the dashboard header.

Tagging is free: keyword rules on the title, or on the description when it also says contract /
hourly / per-task. A full-time job that *builds* AI-training infrastructure isn't tagged. The
scorer also marks gigs the rules miss.

To drop every job from a company, e.g. aggregators that repost other companies' jobs, list it
under `filter.company_exclude` in `config.yaml` (Jobgether is there by default). The next run
(or `jobagent refilter`) moves existing ones to filtered-out; jobs you've applied to stay put.

## How the lists work

There's one jobs table, and every job has a status. The tabs are filters over that status:

| Tab | Shows |
|---|---|
| All jobs | every job as a sortable table: score, role, company, posting link, Tailor button. Tick "Include filtered-out" to see what the rules dropped |
| New today | jobs first found in the latest run |
| Active | everything you haven't acted on |
| Applied | applied / interviewing, with follow-up flags after 7 days |
| Skipped | dismissed, with your reason |
| Closed | postings not seen for 14 days, rejections, closed out |

Each run only appends jobs it has never seen. A job you skipped or applied to keeps
its status even if a board shows it again.

Click a role in **All jobs** to open its full job description on its own page, with
"Open original posting" and "Tailor resume" buttons. Tailor works for any job, including
filtered-out ones. Clicks queue up and run one at a time (a few minutes each).

**Apply** copies your prepared answers to the clipboard, opens the posting in a new tab,
and shows the tailored PDF in Finder so you can drag it into the upload field. The job
moves to "opened", and the dashboard asks "Did you submit?" until you confirm.

**Skip** asks for a quick reason. Recent skip reasons go into the scorer's prompt, so it
learns what you don't want.

## Costs

- Apify: about $0.40 per 1,000 LinkedIn jobs and about $1 per 1,000 Bayt/NaukriGulf jobs.
  The default searches come to a few dollars a month.
- Claude (counts against your Claude plan):
  - Scoring: Haiku, the cheapest model, 20 jobs per call.
  - Tailoring: Sonnet, in a single session with no subagents. Automatic only for scores
    of 80+, at most 3 a day (`tailor.max_per_run`); click Tailor for anything else.
    Each tailoring is about 30 Claude steps, and every step re-reads the whole conversation,
    so the prompt asks for as few steps as possible. For a role you really want,
    set `tailor.model: opus` in config.yaml for that one tailoring, then switch back.
  - If your plan's usage limit is hit, the run stops calling Claude instead of failing job
    after job, and those jobs are retried automatically on the next run.
  - Every Claude call is logged (tokens, turns, minutes and Claude Code's cost estimate).
    `uv run jobagent claude-usage` shows it per day, plus the most expensive tailorings.

## Files

- `data/jobs.db`: SQLite. Tables: `jobs`, `status_history`, `runs`, `claude_usage`.
- `data/raw/`: raw source payloads, kept 14 days. If a source suddenly maps nothing,
  look here for the field names and add them to `collectors/apify_boards.py`.
- `logs/`: run logs (`jobagent.log`; `scheduled_run.log` and `manual_run.log` for runs
  started by the schedule or the Run now button).
- `Dockerfile`, `docker-compose.yml`, `docker/`: the Docker setup.
