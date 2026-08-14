#!/usr/bin/env python3
"""builderctl — drive builderapps from a terminal.

    ./cli/builderctl.py login
    ./cli/builderctl.py create "A tiny online notepad: write a note, save it, get a link"
    ./cli/builderctl.py assistant-create abc123 --template developer --start
    ./cli/builderctl.py beat abc123 7 --follow
    ./cli/builderctl.py usage abc123

## THE ONE THING TO UNDERSTAND ABOUT THIS TOOL

**It is a CLIENT, not a second engine.** Every command below is a thin wrapper over exactly
the HTTP endpoint the web SPA calls for the same action — same URL, same body, same auth,
same server-side code path. There is no pipeline here, no build logic, no QA logic, no
retry-or-repair cleverness: this file formats arguments into a request and formats the
response for a terminal, and that is the whole of it.

That is a requirement, not an accident. A CLI that reimplemented any part of the pipeline
would be a *second* implementation to keep in step, and the moment the two drifted, the one
we test with would stop being the one users get — a test harness that passes while the real
product is broken. We have already been burned by verification that lied; this is the
structural answer to it.

So: **anything this CLI can do, the browser can do identically, and vice versa.** If you
find something here that is not a call to a documented endpoint in `/openapi.json`, that is
a bug in this file.

## Auth

Same OAuth 2.0 / OIDC authorization server as the SPA (account.osmike.com), same public
PKCE client. `login` runs authorization-code + PKCE and caches the token in
`~/.builderapps/credentials.json` (0600). You can also pass a credential directly:

    BUILDERAPPS_TOKEN=<jwt>     an account.osmike.com access token
    BUILDERAPPS_API_KEY=<key>   a legacy hive agent key (the X-API-KEY half of dual-auth)

Standard library only, deliberately: this has to run on a box with nothing installed.
"""
import argparse
import base64
import getpass
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = os.environ.get("BUILDERAPPS_API", "https://builderapps-api.osmike.com")
ISSUER = os.environ.get("BUILDERAPPS_ISSUER", "https://account.osmike.com")
CLIENT_ID = os.environ.get("BUILDERAPPS_CLIENT_ID", "builderapps-web")
REDIRECT_URI = os.environ.get("BUILDERAPPS_REDIRECT_URI",
                              "https://builderapps.osmike.com/auth/callback")
SCOPE = os.environ.get("BUILDERAPPS_SCOPE", "openid profile email")
CRED_PATH = os.path.expanduser(os.environ.get("BUILDERAPPS_CREDENTIALS",
                                              "~/.builderapps/credentials.json"))

# ---- terminal dressing (degrades to nothing when piped) -------------------
_TTY = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _TTY else s


DIM, BOLD, RED, GRN, YEL, CYA = (lambda x: c("2", x)), (lambda x: c("1", x)), \
    (lambda x: c("31", x)), (lambda x: c("32", x)), (lambda x: c("33", x)), \
    (lambda x: c("36", x))


def die(msg: str, code: int = 1):
    print(RED("error: ") + msg, file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------
def _load_creds() -> dict:
    try:
        with open(CRED_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_creds(d: dict) -> None:
    os.makedirs(os.path.dirname(CRED_PATH), exist_ok=True)
    fd = os.open(CRED_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=2)


def auth_headers() -> dict:
    """Exactly the two credential forms the control plane's dual-auth accepts."""
    if os.environ.get("BUILDERAPPS_API_KEY"):
        return {"X-API-KEY": os.environ["BUILDERAPPS_API_KEY"]}
    tok = os.environ.get("BUILDERAPPS_TOKEN")
    if not tok:
        creds = _load_creds()
        tok = creds.get("access_token")
        exp = float(creds.get("expires_at") or 0)
        if tok and exp and time.time() > exp - 30:
            tok = _refresh(creds) or tok
    if not tok:
        die("not signed in — run `builderctl login` (or set BUILDERAPPS_TOKEN / "
            "BUILDERAPPS_API_KEY)")
    return {"Authorization": f"Bearer {tok}"}


def _token_request(body: dict) -> dict:
    data = urllib.parse.urlencode(body).encode()
    req = urllib.request.Request(ISSUER + "/oauth/token", data=data, method="POST",
                                 headers={"Content-Type":
                                          "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _store_token(tok: dict) -> None:
    tok["expires_at"] = time.time() + float(tok.get("expires_in") or 3600)
    _save_creds(tok)


def _refresh(creds: dict):
    if not creds.get("refresh_token"):
        return None
    try:
        tok = _token_request({"grant_type": "refresh_token",
                              "refresh_token": creds["refresh_token"],
                              "client_id": CLIENT_ID})
    except Exception:
        return None
    tok.setdefault("refresh_token", creds["refresh_token"])
    _store_token(tok)
    return tok.get("access_token")


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def cmd_login(args) -> int:
    """Authorization code + PKCE against the SAME public client the SPA uses.

    There is no local web server here on purpose: this often runs on a headless box, and a
    loopback redirect would need a port the authorization server has no reason to trust. The
    browser lands on the SPA's registered callback and the code is in the address bar —
    paste it back. Ordinary, and it works over SSH.
    """
    verifier = _b64(secrets.token_bytes(32))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    state = _b64(secrets.token_bytes(16))
    url = ISSUER + "/oauth/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI,
        "scope": SCOPE, "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256", "audience": "builderapps",
    })
    print("Open this in a browser and sign in:\n\n  " + CYA(url) + "\n")
    print("You will land on a page whose address bar contains `?code=...`.")
    raw = (args.code or getpass.getpass("Paste the code (or the whole URL): ")).strip()
    if "code=" in raw:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
        if q.get("state") and q["state"][0] != state:
            die("state mismatch — start over")
        raw = (q.get("code") or [""])[0]
    if not raw:
        die("no code")
    try:
        tok = _token_request({"grant_type": "authorization_code", "code": raw,
                              "redirect_uri": REDIRECT_URI, "client_id": CLIENT_ID,
                              "code_verifier": verifier})
    except urllib.error.HTTPError as e:
        die(f"token exchange failed: HTTP {e.code} {e.read().decode()[:300]}")
    _store_token(tok)
    print(GRN("signed in") + f" — credentials cached in {CRED_PATH}")
    return 0


# ---------------------------------------------------------------------------
# the HTTP client — every command below goes through here
# ---------------------------------------------------------------------------
def api(method: str, path: str, body=None, timeout: float = 60.0, raw: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json", **auth_headers()}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API_BASE + path, data=data, method=method,
                                 headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:600]
        die(f"{method} {path} -> HTTP {e.code}: {detail}")
    except Exception as e:
        die(f"{method} {path} failed: {e}")
    if raw:
        return resp
    payload = resp.read().decode("utf-8", "replace")
    return json.loads(payload) if payload.strip() else {}


def stream_sse(method: str, path: str, body=None, timeout: float = 3600.0):
    """Consume one of the control plane's SSE endpoints, yielding parsed events.

    The SPA reads these same streams with EventSource; this is the same bytes, parsed by
    hand because the standard library has no SSE client.
    """
    resp = api(method, path, body, timeout=timeout, raw=True)
    for line in resp:
        line = line.decode("utf-8", "replace").rstrip("\n")
        if not line or line.startswith(":"):
            continue
        if line.startswith("data: "):
            try:
                yield json.loads(line[6:])
            except Exception:
                continue


# ---------------------------------------------------------------------------
# rendering the pipeline stream — the same vocabulary the SPA renders
# ---------------------------------------------------------------------------
def render_run_event(ev: dict) -> None:
    t = ev.get("type")
    if t == "created":
        print(BOLD(f"project {ev.get('id')}") + f"  {ev.get('url')}  (run {ev.get('run_id')})")
    elif t == "run_start":
        print(DIM(f"run {ev.get('run_id')} — {ev.get('total_steps')} steps"))
    elif t == "step_start":
        print(f"  {CYA('▶')} [{ev.get('idx'):>2}] {ev.get('name')}", flush=True)
    elif t == "step_done":
        if ev.get("skipped"):
            print(DIM(f"       (already done: {ev.get('name')})"))
        else:
            ms = ev.get("ms") or 0
            print(f"  {GRN('✓')} [{ev.get('idx'):>2}] {ev.get('name')} "
                  + DIM(f"{ms // 1000}s — {(ev.get('detail') or '')[:110]}"))
    elif t == "step_skipped":
        print(f"  {YEL('∅')} [{ev.get('idx'):>2}] {ev.get('name')} "
              + YEL((ev.get("reason") or "")[:120]))
    elif t == "progress":
        print(DIM(f"       … {ev.get('stage')}: {(ev.get('detail') or '')[:140]}"))
    elif t == "repo":
        print(DIM(f"       repo {ev.get('full_name')}"))
    elif t == "commit":
        print(DIM(f"       commit: {(ev.get('message') or '')[:110]}"))
    elif t == "deploy":
        print(f"  {GRN('🚀')} live at {ev.get('url')}  {DIM(str(ev.get('health'))[:90])}")
    elif t == "qa":
        print(f"  {'✓' if ev.get('clean') else '⚠'} QA: "
              + (ev.get("critic") or "")[:160])
    elif t == "error":
        print(RED(f"  ✗ {ev.get('name') or ''}: {(ev.get('message') or '')[:400]}"))
    elif t == "done":
        print(BOLD(GRN("done")) + " — " + (ev.get("summary") or ""))


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_whoami(args) -> int:
    projects = api("GET", "/api/projects").get("projects", [])
    print(f"authenticated against {API_BASE}; {len(projects)} project(s)")
    return 0


def cmd_projects(args) -> int:
    rows = api("GET", "/api/projects").get("projects", [])
    if not rows:
        print(DIM("no projects yet"))
        return 0
    for p in rows:
        st = p.get("status", "")
        mark = GRN(st) if st == "live" else RED(st) if st == "failed" else YEL(st)
        print(f"{BOLD(p['id'])}  {mark:<22} {(p.get('title') or '')[:28]:<28} "
              + DIM((p.get("prompt") or "")[:60]))
    return 0


def cmd_create(args) -> int:
    """POST /api/projects — the identical call the SPA's composer makes."""
    print(DIM(f"POST {API_BASE}/api/projects"))
    for ev in stream_sse("POST", "/api/projects",
                         {"prompt": args.prompt, **({"title": args.title}
                                                    if args.title else {})}):
        render_run_event(ev)
    return 0


def cmd_update(args) -> int:
    for ev in stream_sse("POST", f"/api/projects/{args.project}/update",
                         {"request": args.request}):
        render_run_event(ev)
    return 0


def cmd_watch(args) -> int:
    """Re-attach to a run in flight — the same endpoint the SPA uses after a reload."""
    for ev in stream_sse("GET", f"/api/projects/{args.project}/events"):
        render_run_event(ev)
    return 0


def cmd_show(args) -> int:
    p = api("GET", f"/api/projects/{args.project}")
    print(f"{BOLD(p['id'])}  {p.get('title')}\n  status  {p.get('status')}\n"
          f"  url     {p.get('url')}\n  repo    {p.get('gitea_owner')}/{p.get('gitea_repo')}")
    run = p.get("latest_run") or {}
    if run:
        print(f"  run     {run.get('id')} [{run.get('kind')}] {run.get('status')} — "
              + (run.get("summary") or "")[:100])
        for s in (run.get("steps") or [])[-12:]:
            mark = {"done": GRN("✓"), "failed": RED("✗"),
                    "skipped": YEL("∅")}.get(s.get("status"), CYA("▶"))
            print(f"    {mark} {s.get('name'):<22} {DIM((s.get('log') or '')[:80])}")
    return 0


def cmd_steps(args) -> int:
    data = api("GET", f"/api/projects/{args.project}/steps")
    for s in data.get("steps", []):
        print(f"{s.get('idx'):>3} {s.get('status'):<8} {s.get('name'):<24} "
              + DIM((s.get("log") or "")[:100]))
    return 0


def cmd_usage(args) -> int:
    u = api("GET", f"/api/projects/{args.project}/usage")
    print(json.dumps(u, indent=2, default=str))
    return 0


def cmd_deployments(args) -> int:
    for d in api("GET", f"/api/projects/{args.project}/deployments").get("deployments", []):
        mark = GRN("healthy") if d.get("status") == "healthy" else RED(str(d.get("status")))
        who = f" assistant {d['assistant_id']} beat {d.get('beat_id')}" \
            if d.get("assistant_id") else ""
        print(f"{str(d.get('started_at'))[:19]}  {mark:<18} "
              f"{(d.get('git_sha') or '')[:8]:<9}{who}  {DIM((d.get('health') or '')[:60])}")
    return 0


def cmd_commits(args) -> int:
    for cm in api("GET", f"/api/projects/{args.project}/commits").get("commits", []):
        print(f"{(cm.get('sha') or '')[:8]}  {str(cm.get('date'))[:19]}  "
              f"{DIM((cm.get('author') or '')[:26]):<26}  {(cm.get('message') or '')[:80]}")
    return 0


def cmd_logs(args) -> int:
    for ln in api("GET", f"/api/projects/{args.project}/logs?tail={args.tail}").get("lines", []):
        print(ln)
    return 0


# ---- assistants -----------------------------------------------------------
def cmd_templates(args) -> int:
    cat = api("GET", "/api/assistants/catalog")
    for t in cat.get("templates", []):
        caps = ", ".join(t.get("capabilities") or [])
        print(f"{BOLD(t['key']):<26} {t['role']:<22} {DIM(caps)}")
    print("\n" + DIM("capabilities:"))
    for cap in cat.get("capabilities", []):
        flag = "" if cap.get("safe_default") else YEL("  [write]")
        print(f"  {cap['id']:<16} {cap['label']}{flag}")
    print(DIM("\nroles are open-ended — a template only pre-fills the fields you omit"))
    return 0


def cmd_assistants(args) -> int:
    rows = api("GET", f"/api/projects/{args.project}/assistants").get("assistants", [])
    if not rows:
        print(DIM("no assistants on this project"))
        return 0
    for a in rows:
        st = GRN("active") if a["status"] == "active" else DIM("paused")
        beating = CYA(" ● beating") if a.get("beating") else ""
        last = a.get("last_beat") or {}
        print(f"{BOLD(str(a['id'])):<4} {a['name'][:22]:<22} {a['role'][:18]:<18} {st}"
              f"  every {a['interval_minutes']}m{beating}")
        print(DIM(f"      caps: {', '.join(a.get('capabilities') or []) or 'none'}"))
        if last:
            print(DIM(f"      last: [{last.get('status')}] "
                      f"{(last.get('thought') or '')[:90]}  ${last.get('cost_usd', 0):.4f}"))
    return 0


def cmd_assistant_create(args) -> int:
    body = {"template": args.template, "start": bool(args.start)}
    for k in ("role", "name", "description"):
        if getattr(args, k, None):
            body[k] = getattr(args, k)
    if args.interval:
        body["interval_minutes"] = args.interval
    if args.capabilities:
        body["capabilities"] = [x.strip() for x in args.capabilities.split(",") if x.strip()]
    if args.soul:
        with open(args.soul) as f:
            body["soul_md"] = f.read()
    a = api("POST", f"/api/projects/{args.project}/assistants", body)
    print(f"{GRN('created')} assistant {BOLD(str(a['id']))} — {a['name']} ({a['role']}), "
          f"{a['status']}, every {a['interval_minutes']}m")
    print(DIM("  capabilities: " + (", ".join(a.get("capabilities") or []) or "none")))
    return 0


def cmd_assistant(args) -> int:
    a = api("GET", f"/api/projects/{args.project}/assistants/{args.assistant}")
    print(f"{BOLD(a['name'])} ({a['role']})  #{a['id']}  {a['status']}  "
          f"every {a['interval_minutes']}m")
    print(DIM(f"  capabilities: {', '.join(a.get('capabilities') or []) or 'none'}"))
    print(DIM(f"  soul: {a.get('soul_path')}"))
    for b in (a.get("beats") or [])[:args.limit]:
        _print_beat(b)
    return 0


def _print_beat(b: dict) -> None:
    mark = {"done": GRN("✓"), "failed": RED("✗"), "skipped": YEL("∅")}.get(
        b.get("status"), CYA("●"))
    print(f"\n{mark} beat {b.get('id')}  {str(b.get('ts'))[:19]}  "
          f"{DIM(b.get('trigger_kind') or '')}  "
          f"{b.get('tokens', 0)} tok  ${float(b.get('cost_usd') or 0):.4f}  "
          f"{(b.get('duration_ms') or 0) // 1000}s")
    if b.get("thought"):
        print("  " + (b["thought"] or "")[:400])
    for act in (b.get("actions") or []):
        res = act.get("result") or {}
        ok = GRN("ok") if res.get("ok") else RED("no")
        print(f"    {ok} {act.get('type')}: {str(res.get('detail') or '')[:120]}")


def cmd_beats(args) -> int:
    data = api("GET", f"/api/projects/{args.project}/assistants/{args.assistant}/beats"
                      f"?limit={args.limit}")
    for b in data.get("beats", []):
        _print_beat(b)
    return 0


def cmd_soul(args) -> int:
    print(api("GET", f"/api/projects/{args.project}/assistants/{args.assistant}/soul")
          .get("markdown", ""))
    return 0


def _render_activity(beats: list, seen: set) -> bool:
    """Print activity lines we have not printed yet. Returns True while anything is running.

    The de-dup key is (beat_id, index): the server's feed is append-only within a beat, so
    an index that has already been shown can never change under us.
    """
    running = False
    for b in beats:
        if b.get("status") == "running":
            running = True
        head = (b["beat_id"], -1)
        if head not in seen:
            seen.add(head)
            print(f"\n{BOLD(b.get('name') or 'assistant')} "
                  + DIM(f"· {b.get('role')} · beat {b['beat_id']} ({b.get('trigger_kind')})"))
        for i, line in enumerate(b.get("activity") or []):
            key = (b["beat_id"], i)
            if key in seen:
                continue
            seen.add(key)
            icon = line.get("icon") or "·"
            body = line.get("text") or ""
            if line.get("ok") is False:
                body = RED(body)
            elif line.get("ok") is True:
                body = GRN(body)
            elif line.get("kind") == "text":
                body = DIM(body)
            print(f"  {DIM(str(line.get('ts') or ''))} {icon} {body}")
            if line.get("detail"):
                print(DIM("        " + str(line["detail"])[:300].replace("\n", " ")))
        fin = (b["beat_id"], -2)
        if b.get("status") != "running" and fin not in seen:
            seen.add(fin)
            mark = {"done": GRN("✓ beat done"), "failed": RED("✗ beat failed"),
                    "skipped": YEL("∅ beat skipped")}.get(b.get("status"), b.get("status"))
            print(f"  {mark}  " + DIM(f"${float(b.get('cost_usd') or 0):.4f}"))
    return running


def cmd_activity(args) -> int:
    """Tail the assistant activity feed — the exact endpoint the /builder left pane reads.

    Same rows, same order, same text. What scrolls past here is what a user watching the web
    page sees, which is the point of the whole tool.
    """
    seen: set = set()
    deadline = time.monotonic() + args.timeout
    idle = 0
    while time.monotonic() < deadline:
        data = api("GET", f"/api/projects/{args.project}/assistant-activity?limit="
                          f"{args.limit}")
        running = _render_activity(data.get("beats", []), seen)
        if not args.follow:
            return 0
        if not running:
            idle += 1
            if idle > 3:
                return 0
        else:
            idle = 0
        time.sleep(args.interval)
    print(YEL(f"stopped following after {args.timeout}s"))
    return 0


def cmd_beat(args) -> int:
    """Kick one beat now, then (by default) follow what it does."""
    r = api("POST", f"/api/projects/{args.project}/assistants/{args.assistant}/beat", {})
    print(f"{GRN('beat')} {r.get('beat_id')} started")
    if args.no_follow:
        return 0
    args.limit, args.interval, args.follow = 3, 2.5, True
    args.timeout = args.wait
    time.sleep(2)
    return cmd_activity(args)


def cmd_pause(args) -> int:
    a = api("POST", f"/api/projects/{args.project}/assistants/{args.assistant}/pause", {})
    print(f"{a['name']} is {a['status']}")
    return 0


def cmd_start(args) -> int:
    a = api("POST", f"/api/projects/{args.project}/assistants/{args.assistant}/start", {})
    print(f"{a['name']} is {a['status']}, next beat {a.get('next_beat_at')}")
    return 0


def cmd_get(args) -> int:
    """Escape hatch: GET any documented endpoint. Proof that this tool is only a client —
    everything above is one of these with nicer formatting."""
    print(json.dumps(api("GET", args.path), indent=2, default=str))
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="builderctl",
        description="A thin client over the builderapps HTTP API — the same endpoints the "
                    "web app calls. Not a second engine.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login", help="sign in (OAuth code + PKCE)")
    s.add_argument("--code", help="paste the code non-interactively")
    s.set_defaults(fn=cmd_login)

    sub.add_parser("whoami", help="check the credential works").set_defaults(fn=cmd_whoami)
    sub.add_parser("projects", help="list your apps").set_defaults(fn=cmd_projects)
    sub.add_parser("templates", help="assistant starter templates + capabilities"
                   ).set_defaults(fn=cmd_templates)

    s = sub.add_parser("create", help="build a new app (streams the pipeline)")
    s.add_argument("prompt")
    s.add_argument("--title")
    s.set_defaults(fn=cmd_create)

    s = sub.add_parser("update", help="ask the build pipeline for a change")
    s.add_argument("project")
    s.add_argument("request")
    s.set_defaults(fn=cmd_update)

    for name, fn, helptext in (("show", cmd_show, "project detail + latest run"),
                               ("watch", cmd_watch, "re-attach to a running pipeline"),
                               ("steps", cmd_steps, "steps of the latest run"),
                               ("usage", cmd_usage, "token + cost accounting"),
                               ("deployments", cmd_deployments, "deploy history"),
                               ("commits", cmd_commits, "git log"),
                               ("assistants", cmd_assistants, "list assistants")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("project")
        s.set_defaults(fn=fn)

    s = sub.add_parser("logs", help="the app container's own logs")
    s.add_argument("project")
    s.add_argument("--tail", type=int, default=100)
    s.set_defaults(fn=cmd_logs)

    s = sub.add_parser("assistant-create", help="start an assistant on a project")
    s.add_argument("project")
    s.add_argument("--template", default="developer")
    s.add_argument("--role")
    s.add_argument("--name")
    s.add_argument("--description")
    s.add_argument("--capabilities", help="comma-separated; overrides the template")
    s.add_argument("--soul", help="path to a SOUL.md to use instead of the template's")
    s.add_argument("--interval", type=int)
    s.add_argument("--start", action="store_true", help="begin beating immediately")
    s.set_defaults(fn=cmd_assistant_create)

    s = sub.add_parser("assistant", help="one assistant + its recent beats")
    s.add_argument("project")
    s.add_argument("assistant", type=int)
    s.add_argument("--limit", type=int, default=5)
    s.set_defaults(fn=cmd_assistant)

    s = sub.add_parser("beats", help="beat history")
    s.add_argument("project")
    s.add_argument("assistant", type=int)
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(fn=cmd_beats)

    s = sub.add_parser("soul", help="print an assistant's SOUL.md")
    s.add_argument("project")
    s.add_argument("assistant", type=int)
    s.set_defaults(fn=cmd_soul)

    s = sub.add_parser("beat", help="run one beat now and watch it work")
    s.add_argument("project")
    s.add_argument("assistant", type=int)
    s.add_argument("--no-follow", action="store_true")
    s.add_argument("--wait", type=float, default=1800, help="seconds to follow")
    s.set_defaults(fn=cmd_beat)

    s = sub.add_parser("activity", help="what this project's assistants are doing")
    s.add_argument("project")
    s.add_argument("--follow", "-f", action="store_true")
    s.add_argument("--limit", type=int, default=4)
    s.add_argument("--interval", type=float, default=2.5)
    s.add_argument("--timeout", type=float, default=1800)
    s.set_defaults(fn=cmd_activity)

    for name, fn in (("pause", cmd_pause), ("start", cmd_start)):
        s = sub.add_parser(name, help=f"{name} an assistant's heartbeat")
        s.add_argument("project")
        s.add_argument("assistant", type=int)
        s.set_defaults(fn=fn)

    s = sub.add_parser("get", help="GET any API path (this tool is only a client)")
    s.add_argument("path")
    s.set_defaults(fn=cmd_get)
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print()
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
