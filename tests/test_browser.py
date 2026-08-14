"""Offline tests for the assistant's browser tool (phase 31).

Four properties, each chosen because getting it wrong is silent and expensive:

1. **The allow-list actually denies.** It is the only thing stopping an assistant from
   browsing the internet on the estate's shared credential and IP. A default-allow bug here
   would never show up in normal use.

2. **No chrome-pool credential is reachable from the container side.** The whole point of the
   proxy is that the beat image holds no key; a test asserts the CLI's source contains no
   credential and reads none from the environment.

3. **The self-test fixture is genuinely a 200 that is broken.** The proof that a browser
   catches what curl cannot is only a proof if the fixture really does return valid-looking
   markup with a runtime fault — a fixture that started 500ing would quietly turn the
   demonstration into a tautology.

4. **`mikeweb`'s exit codes mean what the skill says they mean** (0 fine / 1 broken / 2 tool
   failure), because the agent is told to use it as a gate.

    python3 -m tests.test_browser
"""
import importlib.util
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS, FAIL = [], []


def check(name, cond, why=""):
    (PASS if cond else FAIL).append((name, why))
    print(("  ok   " if cond else "  FAIL ") + name + (f": {why}" if not cond and why else ""))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
print("\n1. the allow-list — an assistant browses its own app and nothing else")
# ---------------------------------------------------------------------------
os.environ.setdefault("SITES_BASE", "builderapps.osmike.com")
from server import browser_proxy as BP  # noqa: E402

A = {"id": 2, "project_id": "fq2h2f"}

allowed = [
    "https://fq2h2f.builderapps.osmike.com/",
    "https://fq2h2f.builderapps.osmike.com/notes?x=1",
    "http://fq2h2f.builderapps.osmike.com/deep/path",
    BP.SELFTEST_URL,
]
for u in allowed:
    ok, why = BP.allowed_url(A, u)
    check(f"allows {u[:60]}", ok, why)

denied = [
    ("another tenant's app", "https://nl9lxw.builderapps.osmike.com/"),
    ("the open internet", "https://example.com/"),
    ("a look-alike suffix", "https://fq2h2f.builderapps.osmike.com.evil.test/"),
    ("a look-alike prefix", "https://evilfq2h2f.builderapps.osmike.com/"),
    ("credentials in the host", "https://fq2h2f.builderapps.osmike.com@evil.test/"),
    ("loopback (that is the BROWSER's own container, not the app)", "http://127.0.0.1:8000/"),
    ("localhost by name", "http://localhost/"),
    ("link-local metadata", "http://169.254.169.254/latest/meta-data/"),
    ("file scheme", "file:///etc/passwd"),
    ("javascript scheme", "javascript:alert(1)"),
    ("data scheme", "data:text/html,<h1>x</h1>"),
    ("the control plane's other routes", "https://builderapps-api.osmike.com/api/health"),
    ("empty", ""),
]
for label, u in denied:
    ok, why = BP.allowed_url(A, u)
    check(f"denies {label}", not ok, f"ALLOWED {u!r}")

# A project id must not be able to widen its own allowance through the hostname.
ok, _ = BP.allowed_url({"id": 9, "project_id": ""}, "https://.builderapps.osmike.com/")
check("an assistant with no project gets nothing", not ok)

check("the bounds are small enough to matter",
      BP.MAX_SESSIONS_PER_BEAT <= 5 and BP.MAX_NAVS_PER_BEAT <= 60,
      f"{BP.MAX_SESSIONS_PER_BEAT} sessions / {BP.MAX_NAVS_PER_BEAT} navs")

# ---------------------------------------------------------------------------
print("\n2. no browser credential on the container side")
# ---------------------------------------------------------------------------
cli_path = os.path.join(ROOT, "assistant-runtime", "mikeweb")
cli_src = open(cli_path, encoding="utf-8").read()
beat_src = open(os.path.join(ROOT, "assistant-runtime", "beat.py"), encoding="utf-8").read()
dockerfile = open(os.path.join(ROOT, "assistant-runtime", "Dockerfile"), encoding="utf-8").read()
skill_src = open(os.path.join(ROOT, "assistant-runtime", "skills", "browser-verify",
                              "SKILL.md"), encoding="utf-8").read()

SECRET = "uB49VXwMDy7R2JE0H7mI"          # the shared chrome-pool/GPU password
for label, src in (("mikeweb", cli_src), ("beat.py", beat_src),
                   ("the Dockerfile", dockerfile), ("the skill", skill_src)):
    check(f"{label} contains no chrome-pool password", SECRET not in src)
    check(f"{label} never names CHROME_POOL_PASS", "CHROME_POOL_PASS" not in src)
check("mikeweb never talks to chrome-pool directly",
      "chrome-pool.osmike.com" not in cli_src.replace(
          "# chrome-pool", "").replace("chrome-pool's", "").replace("chrome-pool ", ""))
check("mikeweb authenticates with the per-assistant token only",
      "X-Assistant-Token" in cli_src and "ASSISTANT_TOKEN" in cli_src)
check("the proxy is the only holder of the credential",
      "CHROME_POOL_USER" in open(os.path.join(ROOT, "server", "browser_proxy.py"),
                                 encoding="utf-8").read())

# ---------------------------------------------------------------------------
print("\n3. the self-test fixture: HTTP 200, and broken in a browser")
# ---------------------------------------------------------------------------
html = BP._SELFTEST_HTML
check("the fixture is well-formed HTML a curl user would call fine",
      html.lstrip().startswith("<!doctype html>") and "<ul id=\"notes\">" in html)
check("the fixture mentions the notes it never renders", "Notes" in html)
check("the fixture's bug is a real runtime fault, not a syntax error",
      "n.titel" in html and "n.title" not in html.split("NOTES =")[1].split("render()")[0])
check("the fixture also produces a failed request",
      "selftest-missing-endpoint" in html)
check("the fixture route is registered without auth",
      any(getattr(r, "path", "") == BP.SELFTEST_PATH for r in BP.router.routes))

# ---------------------------------------------------------------------------
print("\n4. mikeweb — the interface the agent is told to rely on")
# ---------------------------------------------------------------------------
check("exit codes are 0 fine / 1 broken / 2 tool failure",
      re.search(r"OK,\s*BROKEN,\s*TOOL_ERROR\s*=\s*0,\s*1,\s*2", cli_src) is not None)
for sub in ("check", "open", "goto", "text", "eval", "click", "type", "console",
            "screenshot", "close"):
    check(f"mikeweb has a `{sub}` subcommand", f'sub.add_parser("{sub}"' in cli_src)
check("mikeweb closes the session it opened in `check`",
      'call("DELETE", f"{BASE}/session/{sid}"' in cli_src
      and "finally:" in cli_src.split("def cmd_check")[1].split("def ")[0])
check("`check` treats a page that renders nothing as broken",
      "len(text.strip()) < 40" in cli_src)
check("an uninstrumented page is reported as 'not seen', not as 'clean'",
      "nothing was seen" in cli_src)
check("mikeweb is stdlib-only (it must run with no pip install)",
      not re.search(r"^import (httpx|requests)", cli_src, re.M))
check("mikeweb compiles", __import__("py_compile").compile(cli_path, doraise=True) or True)

# ---------------------------------------------------------------------------
print("\n5. the tool is wired into the beat, deterministically")
# ---------------------------------------------------------------------------
check("the beat checks the browser after a deploy rather than hoping the LLM does",
      "browser_check(url" in beat_src and 'shipped.get("deployed")' in beat_src)
check("a page that is broken in a browser fails the beat",
      "BROKEN IN A BROWSER" in beat_src)
check("the beat closes its browser sessions in a finally",
      "close_browser()" in beat_src)
check("Pi is handed the skill explicitly (it survives --no-skills)",
      '"--skill", PI_SKILLS' in beat_src)
check("Pi is told which URL is its own app", "MIKEWEB_APP_URL" in beat_src)
check("the runtime image installs mikeweb", "COPY mikeweb /usr/local/bin/mikeweb" in dockerfile)
check("the runtime image ships the skill", "COPY skills /app/skills" in dockerfile)
check("the skill has the frontmatter Pi's loader requires",
      skill_src.startswith("---\nname: browser-verify\ndescription: ")
      and len(skill_src.split("description: ")[1].split("\n")[0]) <= 1024)

# ---------------------------------------------------------------------------
print("\n6. the session leak that was already there")
# ---------------------------------------------------------------------------
chrome_src = open(os.path.join(ROOT, "server", "chrome.py"), encoding="utf-8").read()
bp_src = open(os.path.join(ROOT, "server", "browser_proxy.py"), encoding="utf-8").read()
check("nothing CALLS the non-existent /session/{id}/close any more (the docstring may name "
      "it; a request must not)",
      not re.search(r"c\.post\(f?\"[^\"]*/session/\{[a-z_]+\}/close", chrome_src)
      and not re.search(r"c\.post\(f?\"[^\"]*/session/\{[a-z_]+\}/close", bp_src))
check("chrome.py closes with DELETE /session/{id}",
      'c.delete(f"{CHROME_POOL_URL}/session/{sid}")' in chrome_src)
check("the proxy closes with DELETE too",
      'c.delete(f"{chrome.CHROME_POOL_URL}/session/{sid}")' in bp_src)
api_src = open(os.path.join(ROOT, "server", "assistant_api.py"), encoding="utf-8").read()
check("a recorded beat releases its sessions",
      "close_beat_sessions(beat_id)" in api_src)
rt_src = open(os.path.join(ROOT, "server", "assistant_runtime.py"), encoding="utf-8").read()
check("a CRASHED beat releases its sessions too",
      "close_beat_sessions(beat_id)" in rt_src)

# ---------------------------------------------------------------------------
print("\n7. the habit is in the roles, not just in the tool")
# ---------------------------------------------------------------------------
from server import assistants as ASST  # noqa: E402

dev = ASST.TEMPLATES_BY_KEY["developer"]
tester = ASST.TEMPLATES_BY_KEY["tester"]
check("the Developer is granted the browser", "run_qa" in dev["capabilities"])
check("the Developer's SOUL says to open the page before calling it done",
      "mikeweb check" in dev["soul_md"] and "necessary, not sufficient" in dev["soul_md"])
check("the Developer must not diagnose from logs alone",
      "if I have not loaded it in a browser, I do not know" in dev["soul_md"])
check("the Tester's SOUL drives a real browser",
      "mikeweb" in tester["soul_md"] and "JS console" in tester["soul_md"])
check("a read-only role is NOT quietly given a browser",
      "run_qa" not in ASST.TEMPLATES_BY_KEY["expense"]["capabilities"]
      and "run_qa" not in ASST.TEMPLATES_BY_KEY["security"]["capabilities"])

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for n, why in FAIL:
        print(f"  FAILED: {n} {why}")
sys.exit(1 if FAIL else 0)
