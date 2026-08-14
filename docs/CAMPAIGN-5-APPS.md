# The 5-app acceptance gate

**Mike's bar (verbatim intent):** *"Tell me when you have built 5 (FIVE) working bigger applications
— where you have sent minimum 5 prompts in via chrome-pool. Only fix the pipeline, never the
applications, they are disposable."*

**Do not report success until 5/5 are green.** Partial progress is not the deliverable.

## The 5 prompts — our own start-page examples
If the suggestions we ship on the landing page don't build, nothing does. So the campaign uses
exactly the 5 chips a real user would click:

1. A URL shortener with click analytics and a dashboard of my links
2. A team standup tool: each member posts yesterday/today/blockers, with a daily digest
3. A personal expense tracker with categories, a monthly chart, and CSV export
4. A public changelog / release-notes site with an admin editor and RSS feed
5. A simple job board where companies post roles and candidates apply

## Rules (non-negotiable)

1. **Submit through the real UI via chrome-pool** — log in at `https://builderapps.osmike.com/`,
   type the prompt into the entry screen, press **Build it**. No `curl` to the API. The user's path
   is the path under test.
2. **ONE build at a time.** Concurrency caused a false failure on a real user build (Postgres initdb
   under load 16 blew the healthcheck). Serialize, and don't start one while any other run is active.
3. **NEVER touch a generated app.** No container restarts, no editing its repo, no re-running its
   pipeline to rescue it, no DB surgery. Apps are disposable probes.
4. **A build that needed human intervention is a FAILURE**, even if the app ends up working.
5. **On failure:** root-cause it → fix the **pipeline** → commit + deploy → then run a **FRESH prompt**
   (new project id). Never "retry until it happens to pass" without a fix.
6. **Verification is semantic, not cosmetic.** For each app: exercise its primary flow in the browser,
   create a record, **reload**, confirm it persisted server-side, confirm the expected content is
   *rendered*, and confirm zero console errors. "No console errors" alone is NOT proof — a caught
   error rendered as text leaves the error log empty (that's how `links.forEach` slipped through).
7. **Reap** every app once verified (3 containers each; 242 runs ~190). Keep at most one as a demo.

## Scoreboard

| # | Prompt | Project | Submitted via UI | Build | Verified in browser | Pipeline fixes needed |
|---|---|---|---|---|---|---|
| 1 | URL shortener + analytics | `iukghl` (run 34) | yes | **done** — 12/12 features, 0 skipped, ~28 min | **PASS** — created `/mikecamp1` in the UI, followed the redirect (landed on wikipedia), reloaded the dashboard: row persisted with `clicks=1`; `/links/4` stats page renders totals + 30-day chart + the recent-click row; `/health` `{"status":"ok","db":"ok","redis":"ok"}`; zero console/network errors | **none** (unattended) |
| 2 | Team standup + digest | — | — | — | — | — |
| 3 | Expense tracker + chart + CSV | — | — | — | — | — |
| 4 | Changelog + admin + RSS | — | — | — | — | — |
| 5 | Job board + applications | — | — | — | — | — |

**Status: 1 / 5**

## Pipeline fixes made during the campaign
(append: symptom → root cause → fix → commit)

### Observations that did NOT need a fix
- **App 1, `runtime_qa` reported `live-with-warnings`** — the critic claimed the
  "create link and verify on detail page" flow was broken (`/links/:id` renders "Link not found").
  Manual browser check says the detail page is fine (it renders totals, the 30-day chart and the
  recent-clicks table). The QA critic was wrong, not the app. Watch whether this repeats — a critic
  that cries wolf is the thing that once wasted a repair round.
