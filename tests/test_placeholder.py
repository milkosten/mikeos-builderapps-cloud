"""Offline tests for the build-placeholder guard.

The template ships `public/index.html` as a "your app is being built" holding page so the
subdomain shows something friendly during the build. `express.static("public")` is mounted
BEFORE the app's own routes, so an app whose home page is server-rendered (`app.get("/")`)
keeps serving the holding page forever if the agent never touched that file.

That is exactly how the campaign's changelog site shipped: `/health` green, `finalize` saying
"14 of 14 features built", `/admin` working — and the PUBLIC changelog, the whole product,
showing "Your app is being built" to every visitor.

    python3 -m tests.test_placeholder
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="phtest-")
os.environ["WORKSPACES_ROOT"] = os.path.join(_TMP, "workspaces")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import workspace  # noqa: E402

_passed = 0
_failed: list[str] = []

PLACEHOLDER = ("<!doctype html><html><head><title>Building…</title></head>"
               "<body><h1>Your app is being built</h1></body></html>")
REAL_PAGE = "<!doctype html><html><body><h1>My Links</h1><script src='/app.js'></script></body></html>"


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed.append(name)
        print(f"  FAIL {name}  {detail}")


def make(pid: str, index: str | None, server_js: str) -> None:
    root = Path(workspace.path_for(pid))
    (root / "public").mkdir(parents=True, exist_ok=True)
    if index is not None:
        (root / "public" / "index.html").write_text(index, "utf-8")
    (root / "server.js").write_text(server_js, "utf-8")


SSR = 'app.use(express.static("public"));\napp.get("/", async (_req, res) => res.send(html));\n'
SPA = 'app.use(express.static("public"));\napp.get("/api/links", h);\n'


def main() -> int:
    print("[placeholder state]")
    make("ssr", PLACEHOLDER, SSR)
    check("an untouched placeholder over a server-rendered / is 'shadowing'",
          workspace.placeholder_state("ssr") == "shadowing", workspace.placeholder_state("ssr"))

    make("spa", REAL_PAGE, SPA)
    check("a real single-page frontend is 'gone' (nothing to do)",
          workspace.placeholder_state("spa") == "gone")

    make("dead", PLACEHOLDER, SPA)
    check("a placeholder with NO / route at all is 'only' — the home page was never built",
          workspace.placeholder_state("dead") == "only")

    make("noidx", None, SSR)
    check("no index.html is 'gone'", workspace.placeholder_state("noidx") == "gone")

    print("\n[dropping it]")
    workspace.drop_placeholder("ssr")
    check("the holding page is deleted so the app's own / wins",
          not (Path(workspace.path_for("ssr")) / "public" / "index.html").exists())
    check("state is clean afterwards", workspace.placeholder_state("ssr") == "gone")
    workspace.drop_placeholder("ssr")   # idempotent
    check("dropping twice does not raise", True)

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        print("failed: " + ", ".join(_failed))
    return 1 if _failed else 0


if __name__ == "__main__":
    import shutil
    try:
        rc = main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(rc)
