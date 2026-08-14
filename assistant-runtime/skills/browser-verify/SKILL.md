---
name: browser-verify
description: Load the app in a REAL headless browser with `mikeweb` to see what a user sees — rendered text, JS console errors, failed network requests, screenshots. Use this before claiming a page, a form or a UI change works, and whenever something is reported broken but curl and /health look fine. curl proves a process answered; only this proves the page works.
---

# See the page before you say it works

You have a real browser in this container: the `mikeweb` command. Use it.

## Why this exists

An assistant on this platform was asked why a site was broken. It read the deploy record —
health green, every step clean — and concluded the problem was "certificate provisioning at
the platform ingress layer". It was wrong. The actual cause was a `Content-Security-Policy:
frame-ancestors 'none'` header it had added itself, which blocked the preview iframe. It
could not see what the user saw, so it invented a cause that fitted the data it had.

`curl` and `/health` tell you a process is listening. They cannot tell you that the list is
empty, the button does nothing, the script threw on line 12, or the page is a blank white
screen. A browser can.

## The one command

```
mikeweb check
```

Loads your app, clicks around like an impatient user, and reports the rendered text, the JS
console errors and any failed requests. Give it a URL to check a specific page:

```
mikeweb check https://<your-project>.builderapps.osmike.com/notes
```

**Exit code 0 = the page renders and runs clean. 1 = it is broken in a browser. 2 = the tool
itself could not run.** So you can use it as a gate, and you should.

## Driving a flow

When you changed a form, a login, a filter — exercise the thing you changed, not just the
home page:

```
mikeweb goto https://<your-project>.builderapps.osmike.com/
mikeweb type '#title' 'a test note'
mikeweb type '#body'  'written by the browser check'
mikeweb click 'button[type=submit]'
mikeweb text                                       # what does the page show NOW?
mikeweb eval 'document.querySelectorAll("li.note").length'
mikeweb console                                    # JS errors + failed requests
mikeweb close
```

`mikeweb --help` lists everything (`open`, `goto`, `text`, `eval`, `click`, `type`,
`console`, `screenshot`, `close`).

## Rules

- **Close what you open.** `mikeweb close` (or `mikeweb check`, which closes for you). The
  browser fleet is shared with the rest of the estate.
- **You may only browse your own project's site.** Anything else is refused by the control
  plane, which holds the browser credential — you do not have it and do not need it.
- **A few pages, not a crawl.** You have a small allowance per beat. Check the thing you
  changed.
- **A clean console is not a working feature.** Read the rendered text too: "HTTP 200 with an
  empty list" is the exact failure this tool exists to catch.
- **Never report a pass you did not observe.** If `mikeweb` could not reach the page, say
  that — do not fall back to "the health check is green, so it works".

## When to use it

- After you edit anything a user looks at, BEFORE you finish your turn.
- Whenever a human says something is broken and the logs look fine.
- Before writing a summary that claims a page, form or flow works.
