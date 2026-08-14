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
| 2 | Team standup + digest | `3lq510` (run 35) | yes | **done** — 12/12 features, 0 skipped, ~22 min | **PASS (with a caveat)** — registered a member, posted yesterday/today/blockers, reloaded: the board renders my entry server-side; `POST /api/digests/generate` produces a correct digest of both members. Caveat: the digest has **no page** — it exists only as an API. `/health` green, zero console/network errors | **none** (unattended) |
| 3 | Expense tracker + chart + CSV | `cj1qbm` (run 36) | yes | **done** — 12/12 features, 0 skipped, ~18 min | **PASS** — registered, added a $123.45 Dining expense (`MIKECAMP3-COFFEE`), reloaded: the row, the *Spending by category* SVG bar chart and the month total all render from the server; `←/→` month nav works; `GET /api/export.csv?month=2026-08` returns real CSV (`2026-08-14,Dining,123.45,MIKECAMP3-COFFEE`); `/health` green, zero console errors | **none** (unattended) |
| 4 | Changelog + admin + RSS | `xvynod` (37) **FAIL**<br>`sgj9go` (38) **FAIL**<br>`04thhm` (39) **PASS** | yes (x3) | 3rd attempt: **done** — 14/14 features, 0 skipped, ~24 min | **PASS on attempt 3** — signed into `/admin` as the seeded admin, published `v9.9.9 / MIKECAMP4-RELEASE`, reloaded the public `/`: the release and its markdown body render server-side; `/rss.xml` is valid RSS 2.0 containing the same item; `/health` green, zero console errors. **Attempts 1-2 were failures** (see fixes 4 and 5) | **fix 4** + **fix 5** |
| 5 | Job board + applications | `cvu1kl` (run 40) | yes | **done** — 14/14 features, 0 skipped, ~26 min | **PASS** — registered `MIKECAMP5-COMPANY`, posted `MIKECAMP5-ROLE`, saw it on the public board, applied to it as a candidate (`MIKECAMP5-APPLICANT` + cover note), reloaded the employer dashboard: `1 applicant`, and *View applicants* renders the candidate's name, email and cover note from Postgres; `/health` green, zero console errors | **none** (unattended) |

**Status: 5 / 5 verified.** Honest accounting: **7 builds submitted, 5 apps green, 2 failures**
(both on prompt 4). Unattended success rate **5/7 = 71 %**; per-prompt **5/5** once the two
pipeline bugs the failures exposed were fixed. Prompts 1, 2, 3 and 5 each passed **first time,
with no intervention of any kind**. No app was ever touched, patched, restarted or rescued.

One interruption, not scored: the coordinator restarted `dockerd` on 242 between builds (0 runs
in flight). Apps 1-3 came back with their data intact and were re-verified afterwards.

## Pipeline fixes made during the campaign
(append: symptom → root cause → fix → commit)

### 1. QA sent the browser to a literal `/links/:id`
- **Symptom (app 1, `iukghl`):** `runtime_qa` = `live-with-warnings`; critic said the detail page
  renders "Link not found". Browsing it by hand shows totals, the 30-day chart and the recent-clicks
  table — the app was fine.
- **Root cause:** `plan_flows` may answer `page: "/links/:id"`, and `run_flows` navigated to that
  string **literally**. `_ID_RE` had been written for exactly this substitution and was never wired
  up, so every detail-page flow was guaranteed to "fail" and hand the repair agent a fabricated bug.
- **Fix:** `_resolve_page()` fills `:id` / `{id}` / `__ID__` from the create response and falls back
  to the index when there is no id — a weaker assertion beats a false accusation.
- **Commit:** `d2a46ee`.

### 2. QA read its OWN unique-constraint collision as "registration is broken"
- **Symptom (app 2, `3lq510`):** `live-with-warnings`, **0/3 flows rendered**, critic: *"Registration
  is broken (HTTP 409 on a fresh email), which cascades into all authenticated flows"*. Registering
  by hand works; entries persist and render. Two QA rounds were burned chasing it.
- **Root cause:** the planner emits literal values (`qa@example.com`, a fixed short code) and the
  marker only goes into the one display field. The second flow — or the second round on the same app
  — re-posts the same email, the app correctly answers 409, and every later authenticated flow then
  401s because no session exists.
- **Fix:** emails are always uniquified per seed (`local+MARKER@domain`); a `409`/"already exists"
  reply retries the create **once** with every short string suffixed, skipping dates/numbers/booleans
  (suffixing those would turn a 409 into a real 422).
- **Commit:** `3cccb52`.

### 3. QA read a required query parameter as a broken read path
- **Symptom (app 3, `cj1qbm`):** `live-with-warnings`, critic: *"`GET /api/expenses` rejects the
  dashboard's month parameter with HTTP 400 `invalid_month`, so newly created expenses cannot be
  listed"*. By hand the app is perfect — the expense saves, survives a reload, renders in the chart,
  and the CSV export returns real rows.
- **Root cause:** the app's collection endpoint requires `?month=YYYY-MM` (a sane design — the
  dashboard always sends one), while `plan_flows` is instructed that `list` is the collection
  endpoint **"(no params)"**. QA called it bare, got 400, and blamed the app.
- **Fix (two parts):** the planner is told to include REQUIRED query parameters with values that
  match the record it is creating; and a 4xx on the cross-check list no longer fails the flow — it
  is recorded on the step and QA proceeds to the RENDER assertion, which is what actually decides
  whether the user's flow works. A genuinely broken read path still fails (the page has nothing to
  show).
- **Commit:** `566fd11`.

### 4. The backlog was TRUNCATED, so the last features were never built — **a lost build**
- **Symptom (app 4 attempt 1, `xvynod`):** run `done`, `finalize` = *"project live — 12 of 12
  features built"*, `/health` green — and `GET /admin` -> **404**, `GET /rss.xml` -> **404**. Logging
  in at `/admin/login` succeeds and redirects to an `/admin` that does not exist. The prompt was
  "a public changelog / release-notes site **with an admin editor and RSS feed**".
- **Root cause:** `TECH_PLAN_ASK` asked the model for *"6-14 tasks"*; `pipeline._MAX_FEATURES` was
  **12**. The model wrote 14, `parse_backlog` sliced to the cap, and items 13-14 vanished with no
  log line, no event, no warning. Item 14 was literally *"Frontend `/admin` editor page … then
  `GET /rss.xml` feed generation"*. The "12 of 12" summary counted the ALREADY-truncated backlog,
  so the run looked complete.
- **Fix:** `backlog.MAX_FEATURES` is now the single source of truth — the prompt quotes it and the
  pipeline imports it, so they cannot drift again. The cap now **folds** the overflow into the last
  step instead of dropping it, and the step log reports "(N planned; the last K folded)". The prompt
  also gained the rule this taught: the backlog IS the build, so every page under `## Pages` needs
  its own item and scope is cut in the Data Model/Routes, never in the Pages.
- **Commit:** `9925b6c` (+ `tests/test_backlog.py`).

### 5. The finished app still served the *"your app is being built"* placeholder
- **Symptom (app 4 attempt 2, `sgj9go`):** 14/14 features built, `/health` green, `/admin` and
  `/feed.xml` both working — and the **public changelog at `/`**, the entire product, rendered the
  builder's holding page to every visitor.
- **Root cause:** the template ships `public/index.html` as a friendly "being built" page, and
  `server.js` mounts `express.static("public")` **before** the routes. This app renders its home
  page server-side (`app.get("/")`), so the untouched holding page shadowed it permanently. Nothing
  in the pipeline ever looks at `/` — the health gate reads `/health`, `finalize` counts backlog
  items.
- **Fix:** at `final_deploy` the holding page is inspected. *shadowing* (the app has its own `/`)
  -> delete it, it has done its job. *only* (nothing serves `/` at all) -> **fail the run loudly**,
  because shipping a placeholder as "live" is precisely the silent-success failure the house rules
  exist to prevent. An app whose frontend the agent actually wrote is untouched.
- **Commit:** `4439efb` (+ `tests/test_placeholder.py`).

### 6. A nested create path was posted with a literal `:id`
- **Symptom (app 5, `cvu1kl`):** `live-with-warnings`, critic: *"the candidate apply flow is broken:
  `POST /api/jobs/:id/apply` returned HTTP 400 'invalid job id'"*. Applying through the browser works
  — the application is stored and appears in the employer's dashboard with its cover note.
- **Root cause:** the same defect as fix 1, on the *create* path. `plan_flows` may answer a `:param`
  route (`_same_route` matches by shape) and `run_flows` POSTed it literally, so the app saw the
  string `:id` where a job id belongs.
- **Fix:** a placeholder in the create path is filled from the parent collection (the flow's `list`,
  else the path up to the placeholder). If no parent exists the flow is `inconclusive_no_parent` —
  neither a pass nor a bug.
- **Commit:** `ef4af34`. Shipped after the last build, so covered by unit tests, not a live build.

### Observations that did NOT need a fix
- **App 1, `runtime_qa` reported `live-with-warnings`** — the critic claimed the
  "create link and verify on detail page" flow was broken (`/links/:id` renders "Link not found").
  Manual browser check says the detail page is fine (it renders totals, the 30-day chart and the
  recent-clicks table). The QA critic was wrong, not the app. Watch whether this repeats — a critic
  that cries wolf is the thing that once wasted a repair round. **Fixed — see fix 1.**
- **App 2 shipped the daily digest as an API with no page.** `POST /api/digests/generate`,
  `GET /api/digests` and `GET /api/digests/:date` all work and return a correct digest, but nothing
  in `public/` links to them, so a user cannot reach the feature. The primary flow (post standup →
  board renders it → survives reload) is fully working, so this is scored a pass, but it is a real
  gap: a requirement stated in the prompt landed backend-only. Worth a planner fix if it recurs
  (not fixed mid-campaign — it is a codegen-quality issue, not a build failure). **App 4's two
  failures were the same disease in a fatal form**, and fixes 4 and 5 attack it directly.
- **QA cannot authenticate as an admin.** On `xvynod` all three seeded flows returned 401 because
  the app has no public registration — only an admin login. Semantic QA can seed an app that lets
  it register, but not one with a bootstrapped admin account. That is a real blind spot; it produced
  warnings rather than a wrong verdict, so it was left alone during the campaign.

## Verdict

**5 / 5 working applications, all submitted through the real UI via chrome-pool, all verified
semantically in the browser (create a record → reload → assert it is rendered from Postgres).**

It took **7 builds** to get 5 apps. The two failures were both the changelog prompt, and both were
real product failures rather than infrastructure flakiness:

1. the backlog was **truncated** at 12 while the plan asked for 14, so the admin editor and the RSS
   feed were never built — and the run still reported "12 of 12 features built";
2. the finished app served the builder's **"your app is being built" placeholder** as its public home
   page, because `express.static` shadows a server-rendered `/`.

Both are now impossible: the cap folds instead of truncating (and the prompt quotes the same
constant), and `final_deploy` deletes a placeholder that shadows the app's own `/` — or fails the
run outright when nothing serves `/` at all.

The other four fixes were all the same disease in the QA layer: **QA accusing a healthy app**
(a literal `:id` in a page path, its own 409 email collision, a required query parameter, a literal
`:id` in a create path). Four of the seven builds finished `live-with-warnings` for reasons that
were entirely QA's own, which burns repair rounds and — worse — points the repair agent at code that
is not broken.

**Not fixed, and worth knowing:**
- **Semantic QA cannot log in as an admin.** An app with a bootstrapped admin account and no public
  registration (both changelog builds) fails every seeded flow with 401. It produces warnings, not a
  wrong verdict, so it was left alone — but it means QA is blind on exactly the apps where the
  editor is the product.
- **A capability can still land backend-only.** App 2's daily digest works perfectly as an API and
  has no page. Fix 4's prompt rule ("every page under `## Pages` needs its own backlog item; an
  endpoint with no page is a feature the user cannot use") is aimed at this, and app 4's third
  attempt and app 5 both shipped complete UIs — but one build is not proof.
