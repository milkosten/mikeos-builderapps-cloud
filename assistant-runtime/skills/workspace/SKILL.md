---
name: workspace
description: Read and write the project's shared work-tracker with the `ws` command — features, bugs, tasks, test cases, documentation and knowledge-base notes, each with a status, comments and a full history of who changed what. Use it at the START of a beat to see what is already known and what other assistants are doing, and at the END to record what you built, found, decided or learned. It is the only memory that survives this container.
---

# The shared workspace: `ws`

This container is deleted when your beat ends. Everything you learned goes with it — unless
you put it in the workspace.

`ws` is the work-tracker this project's **pipeline, every assistant and the human all share**.
The build pipeline already filed every feature it built (and every one it had to skip, with
the reason). Your colleagues file what they find there. So do you.

## Start your beat by looking

```
ws list                            # everything on the board
ws list --kind bug --status open   # what is broken and unclaimed
ws search "login"                  # across features, bugs, docs and the knowledge base
```

If someone already filed the bug you were about to file, comment on theirs. If a feature is
already `in_progress`, do something else — two assistants rebuilding the same thing is the
most expensive way this platform can waste money.

## Finish your beat by writing

```
ws new --kind bug --title "Save button does nothing on /notes" \
       --body "Clicking Save posts /api/notes; the row never appears. Console shows a 500 from
the handler. Reproduced with mikeweb."
ws update 42 --status in_progress          # you are working on it
ws update 42 --status done                 # it is fixed AND you verified it
ws comment 42 "Fixed in a1b2c3d — the handler swallowed the DB error."
ws link 51 --to 42 --rel covers            # this test case covers that bug
```

`ws get 42` prints the whole item — body, every comment, the full history, and what it is
linked to — so you can act on it without hunting.

## The kinds, and what they are for

| kind | what belongs there |
|---|---|
| `feature` | something the app should do. The pipeline seeds these from the backlog. |
| `bug` | something that is broken. Include how to reproduce it and what you actually saw. |
| `task` | work that is not a feature or a bug (a refactor, a migration, a chore). |
| `testcase` | a check someone should be able to run: steps in, expected result out. |
| `doc` | how something works, written for whoever reads it next. |
| `kb` | a fact worth keeping: a decision, a gotcha, why a thing is the way it is. |

**Kinds and statuses are free text.** If `risk` or `decision` is the honest label, use it —
nothing has to be deployed for a new kind to exist. Stick to the conventions when they fit,
so the board stays readable.

Statuses: `open` · `in_progress` · `blocked` · `done` · `rejected`.

## Rules

- **Write the thing a stranger could act on.** "Save is broken" is noise. "POST /api/notes
  returns 500, handler swallows the DB error, reproduced on /notes with mikeweb" is a bug
  someone can fix — possibly a different assistant, tomorrow, with none of your context.
- **`done` means you verified it**, not that you wrote the code. If you changed a page, look
  at it with `mikeweb` first. A `done` that is not true poisons the board for everyone.
- **A blocked item needs its reason in the body.** "Blocked" with no explanation is a dead
  end for whoever picks it up.
- **Comment instead of duplicating.** One item per real thing.
- **Everything you do is attributed to you** — your name is on every item, comment and status
  change, and the human reads that trail. That is the point: it is how your work becomes
  visible instead of vanishing with the container.
- Do not dump whole files or whole logs into a body. Link, quote the relevant lines, keep it
  readable.

`ws --help` lists every command (`list`, `get`, `new`, `update`, `comment`, `link`,
`search`, `events`). Exit code 0 = done, 1 = the API refused, 2 = the tool could not run.

**Scope:** the key in this container is scoped to this project alone. You cannot see, and
cannot touch, any other project's workspace.
