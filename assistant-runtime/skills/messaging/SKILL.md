---
name: messaging
description: Send a direct message to another assistant on this project with the `msg` command, and read the ones sent to you. Sending WAKES the recipient — a whole beat starts so they can read it, which costs real money — so use it to hand work to the one colleague who can do what you cannot ("I filed bug #42, can you fix it?" / "fixed and deployed, please retest"), and never to acknowledge, thank or confirm. Use `--ref <item_id>` so they receive the whole workspace item rather than your summary of it.
---

# Talking to your colleagues: `msg`

`ws` is the noticeboard everyone reads. `msg` is tapping one person on the shoulder.

```
Tester    → Developer:  "Found it. Filed #42 with the repro. Can you take it?"
Developer → Tester:     "Fixed in a1b2c3d and deployed. Please retest."
```

## The one thing to understand first

**A message wakes somebody up.** No assistant is sitting idle waiting for mail — your
colleagues only exist while they are running a beat. So a message is stored and then an
entire beat is started for the recipient: a container, a reasoning round, possibly a coding
agent. That costs the project real money.

That is not a reason to avoid messaging. It is the reason to send messages that are **worth a
beat**.

## Send one when

- You need a colleague to **do something you cannot do yourself**. You found a bug and cannot
  edit code. You shipped a fix and need someone else to verify it.
- You are **blocked** on an answer only they have.

## Do not send one to

- **Acknowledge, thank, or confirm receipt.** "Thanks, I'll take a look" costs a container.
  Silence is the correct answer to good news — **"no reply needed" is a real, preferred
  outcome**, not a failure to be polite.
- **Report what you did.** Put it on the board (`ws update 42 --status done`). Everyone reads
  the board and it wakes nobody.
- **Ask something that is already written down.** `ws list`, `ws search "signup"` first.

## `--ref` is the point of the tool

```
msg send --to Developer --ref 42 \
  --body "The signup form silently fails on a duplicate email — filed as #42 with the
repro steps and the console output. Can you fix it? Message me when it is deployed and
I will retest."
```

With `--ref 42` the Developer wakes up holding **the whole of item #42** — the body, every
comment, the full history, everything it is linked to — before its first thought. Without it,
they get your one-line summary of a bug report and have to go looking for the rest.

So the pattern that works is always two steps: **file it with `ws`, then point at it with
`msg`.**

## The bounds are real

| Bound | What happens |
|---|---|
| **Chain depth** | A conversation stops after a fixed number of replies (`msg who` prints it). Past that, your message is stored and visible to the human but is **not delivered** and nobody is woken. |
| **Daily budget** | A project that has spent its daily cap delivers nothing until midnight UTC. |
| **Duplicate wakes** | Messaging someone twice before they wake costs **one** beat, not two — both messages arrive in the same inbox. |

A blocked send **exits 1**. Do not retry it — retrying is exactly the runaway loop the bound
exists to stop. Put the thing on the board instead; a board item gets picked up without
anyone having to be woken.

## The commands

```
msg who                    who is here, their roles, what they are allowed to do
msg inbox                  what has been sent to you and not yet read
msg send --to Developer --ref 42 --body "..."
msg thread 17              the whole conversation, in order
```

`msg --help` has the rest. Exit codes: `0` sent · `1` refused or bounded · `2` the tool could
not run.

**You cannot forge a sender.** Who the message is from comes from this container's own
credential, not from anything you pass — the same rule as the workspace board. You choose the
recipient; that is all.
