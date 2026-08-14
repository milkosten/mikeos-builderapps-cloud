# builderctl — builderapps from a terminal

```bash
./cli/builderctl.py login
./cli/builderctl.py create "A tiny online notepad: write a note, save it, get a unique link to it"
./cli/builderctl.py assistant-create abc123 --template developer --start
./cli/builderctl.py beat abc123 7
./cli/builderctl.py usage abc123
```

## The CLI is a client, not a second engine

**Every command is a thin wrapper over exactly the HTTP endpoint the web app calls for the
same action** — same URL, same request body, same auth, same server-side code path. There is
no pipeline in this directory. No build logic, no QA logic, no deploy logic, no retry
cleverness. `builderctl.py` turns arguments into a request and a response into terminal
output, and that is all of it.

**Anything this CLI can do, the browser can do identically. Anything the browser can do, the
CLI can do identically.** They are two front ends over one API.

This is a requirement, not a coincidence.

A CLI that reimplemented any part of the pipeline would be a *second implementation* to keep
in step with the first. The moment they drifted — and they always drift — the thing we test
and demo with would stop being the thing users actually get. That is not a hypothetical
here: this project has already shipped a QA pass that reported green on a broken app, and
the fix for that class of problem is structural, not a promise to be careful. So the CLI is
deliberately incapable of being wrong about the pipeline, because it does not contain one.

If you find something in `builderctl.py` that is not a call to an endpoint published in
`https://builderapps-api.osmike.com/openapi.json`, that is a bug. The `get` subcommand
exists to make the point concrete:

```bash
./cli/builderctl.py get /api/projects/abc123/assistant-activity
```

Every other command is one of those with nicer formatting.

## What it can do

| | |
|---|---|
| `login` / `whoami` | OAuth 2.0 code + PKCE against account.osmike.com — the same public client (`builderapps-web`) the SPA uses |
| `create "<prompt>"` | `POST /api/projects`, streaming the pipeline's SSE steps as they happen |
| `update <id> "<change>"` | `POST /api/projects/{id}/update` — the build pipeline's change path |
| `watch <id>` | re-attach to a run already in flight (`/events`), exactly like a browser reload does |
| `projects` / `show` / `steps` / `logs` / `commits` / `deployments` | read the project's state |
| `usage <id>` | token + cost accounting |
| `templates` | the assistant starter templates and the capability vocabulary |
| `assistants <id>` / `assistant <id> <aid>` / `soul` | inspect assistants |
| `assistant-create <id> --template developer --start` | start one |
| `beat <id> <aid>` | run one beat NOW and follow what it does, line by line |
| `activity <id> -f` | tail the assistant activity feed |
| `pause` / `start` | control an assistant's heartbeat |

## `beat` and `activity` — watching an assistant work

`activity` reads `GET /api/projects/{id}/assistant-activity`, which is the **same endpoint,
returning the same rows, that the `/builder` page's left pane renders.** What scrolls past in
the terminal is what a user watching the web page sees, in the same order:

```
Developer · Developer · beat 41 (manual)
  16:10:04 ⟲ refreshing my checkout of the repository
  16:10:07 📖 reading the project's documents — read VISION.md, TECHNICAL-PLAN.md, UX.md
  16:10:09 🤔 deciding what is worth doing this beat
  16:10:31 🧠 The notepad has no accounts, so notes cannot be owned…
  16:10:32 🛠 starting on: add self-hosted accounts and sign-in…
  16:10:41 👀 reading server.js
  16:11:02 ✎ editing server.js
  16:11:20 $ node --check server.js
  16:12:05 ✎ changed 4 file(s): server.js, migrations/002_users.sql, public/login.html
  16:12:07 ✓ committed 3f9a1c2 "feat: accounts and sign-in"
  16:12:08 🚀 asking the control plane to build and health-gate this commit
  16:14:51 🟢 health gate green — the change is live
  ✓ beat done  $0.0412
```

Every one of those lines corresponds to something that actually happened — a tool the coding
agent really invoked, a file it really wrote, a health gate that really returned a verdict.
Nothing is synthesised to make the output look busy.

## Auth

`login` caches the token in `~/.builderapps/credentials.json` (mode 0600) and refreshes it
automatically. There is no local callback server: the browser lands on the SPA's registered
callback and you paste the `code` back, which is what makes it work over SSH on a headless
box. You can also skip `login` entirely:

```bash
export BUILDERAPPS_TOKEN=<account.osmike.com access token>   # or
export BUILDERAPPS_API_KEY=<legacy hive agent key>
```

Both are the two halves of the control plane's dual-auth, so both work everywhere.

## Configuration

| env | default |
|---|---|
| `BUILDERAPPS_API` | `https://builderapps-api.osmike.com` |
| `BUILDERAPPS_ISSUER` | `https://account.osmike.com` |
| `BUILDERAPPS_CLIENT_ID` | `builderapps-web` |
| `BUILDERAPPS_CREDENTIALS` | `~/.builderapps/credentials.json` |

Standard library only — no `pip install`, runs on any box with Python 3.10+.
