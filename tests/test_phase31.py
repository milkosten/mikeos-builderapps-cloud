"""Offline tests for phase 31 — blue/green deploys + the deploy-failure feedback loop.

Four properties, each chosen because getting it wrong is both expensive and SILENT:

1. **The candidate colour is unroutable until it passes the gate.** This is the whole of
   "a failed deploy never touches the live app". If the normalizer ever puts the new colour on
   `deploy_default` at creation time, Caddy can reach a container that has not been health-
   gated — and a broken deploy becomes a user-visible outage again, with nothing failing loudly
   to tell us.

2. **The isolation guardrails survived the rewrite.** The normalizer is the security boundary,
   not a formatter. Blue/green touched every line of it.

3. **The failure signature is stable across noise and different across bugs.** It decides
   whether "the identical failure twice -> stop" rule fires. Too sensitive (pids, ports,
   timestamps) and the rule never fires and the agent burns its whole budget; too blunt and a
   genuinely new error is mistaken for a repeat and a fixable failure gets escalated.

4. **Secrets do not ride out in the evidence.** The envelope is a crash dump going to an LLM
   and to a web page, and a crash dump loves to print DATABASE_URL.

    python3 -m tests.test_phase31
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def check(name, cond, why=""):
    (PASS if cond else FAIL).append((name, why))


TEMPLATE = """
services:
  app:
    build: .
    environment:
      PORT: "3000"
  db:
    image: postgres:16
    volumes:
      - pgdata:/var/lib/postgresql/data
  redis:
    image: redis:7
volumes:
  pgdata:
"""


def test_normalizer():
    import yaml
    from server import deployer

    for colour in ("blue", "green"):
        doc = yaml.safe_load(deployer.normalize_compose(TEMPLATE, "abc123", None, colour))
        svcs = doc["services"]
        check(f"[{colour}] the app service is colour-qualified",
              f"app-{colour}" in svcs and "app" not in svcs,
              "same service name would make compose RECREATE the running colour — the exact "
              "downtime blue/green removes")
        app = svcs[f"app-{colour}"]
        check(f"[{colour}] the container is colour-named",
              app["container_name"] == f"abc123-app-{colour}")
        # THE property. A candidate on deploy_default is reachable by Caddy before the gate.
        check(f"[{colour}] the candidate is NOT on the shared network at creation",
              app["networks"] == ["proj-abc123"],
              "it joins deploy_default only at the flip, after the health gate")
        check(f"[{colour}] db and redis are single and unqualified",
              svcs["db"]["container_name"] == "abc123-db"
              and svcs["redis"]["container_name"] == "abc123-redis",
              "both colours share ONE database — that is why migrations must be additive")
        check(f"[{colour}] datastores stay on the private network only",
              svcs["db"]["networks"] == ["proj-abc123"]
              and svcs["redis"]["networks"] == ["proj-abc123"])
        check(f"[{colour}] resource caps still injected",
              app.get("mem_limit") and app.get("pids_limit") == 512)
        check(f"[{colour}] the datastores' absolute volume dirs survived",
              str(svcs["db"]["volumes"][0]).startswith("/")
              and "--stop-writes-on-bgsave-error" in svcs["redis"]["command"])

    # deny-by-default is the point of this module; prove it still bites.
    for bad, why in (
        ("    ports:\n      - '8080:3000'", "published host port"),
        ("    privileged: true", "privileged"),
        ("    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock", "docker socket"),
        ("    network_mode: host", "host networking"),
    ):
        raw = TEMPLATE.replace('  app:\n    build: .', '  app:\n    build: .\n' + bad)
        try:
            deployer.normalize_compose(raw, "abc123", None, "blue")
            check(f"normalizer rejects {why}", False, "it did NOT raise")
        except deployer.NormalizeError:
            check(f"normalizer rejects {why}", True)
        except Exception as e:  # noqa: BLE001
            check(f"normalizer rejects {why}", False, f"wrong exception {e!r}")

    try:
        deployer.normalize_compose(TEMPLATE, "abc123", None, "chartreuse")
        check("normalizer rejects an unknown colour", False, "it did NOT raise")
    except deployer.NormalizeError:
        check("normalizer rejects an unknown colour", True)


def test_signature():
    from server import repair

    crash = {"stage": "health_gate", "app_logs":
             "npm info ok\n"
             "node:internal/modules/cjs/loader:1215\n"
             "  throw err;\n"
             "Error: Cannot find module './lib/nope'\n"
             "    at Module._resolveFilename (node:internal/modules/cjs/loader:1212:15)\n"}
    check("the error line is the one a human would grep for",
          "Cannot find module" in repair.first_error_line(crash),
          f"got {repair.first_error_line(crash)!r}")

    # Same bug, different run: pids, ports and line offsets move. It must still be "the same".
    noisy = dict(crash, app_logs=crash["app_logs"].replace("1212:15", "998:22")
                 .replace("loader:1215", "loader:1009"))
    check("a repeat of the same bug produces the SAME signature",
          repair.signature_for(crash) == repair.signature_for(noisy),
          "otherwise 'identical failure twice -> stop' never fires and the budget always burns")

    other = {"stage": "health_gate",
             "app_logs": "Error: connect ECONNREFUSED 10.0.0.4:5432\n"}
    check("a different bug produces a DIFFERENT signature",
          repair.signature_for(crash) != repair.signature_for(other),
          "otherwise a fixable second failure is escalated as if nothing had changed")

    build = {"stage": "build", "build_log": "Step 5/9 : RUN npm ci\nnpm ERR! 404 no-such-pkg\n"}
    check("a build failure is signed from the build log",
          "npm err" in repair.signature_for(build).lower())
    check("the stage is part of the signature",
          repair.signature_for(dict(crash, stage="up")) != repair.signature_for(crash),
          "the same message at a different stage is a different failure")

    pub = {"stage": "public_check", "health": {"http": 502, "body": "bad gateway"}}
    check("a public-check failure still yields a usable line",
          "502" in repair.first_error_line(pub))


def test_no_secret_leak():
    from server import deployer

    secrets = {"db_password": "hunter2-correct-horse", "app_secret": "s3cr3t-app-value-xyz"}
    dump = ("Error: password authentication failed\n"
            "DATABASE_URL=postgresql://app:hunter2-correct-horse@abc123-db:5432/app\n"
            "REDIS_URL=redis://abc123-redis:6379\n"
            "APP_SECRET=s3cr3t-app-value-xyz\n"
            "also postgres://someone:unknown-other-pw@host:5432/db\n")
    out = deployer._redact_logs(dump, secrets)
    check("the project's db password is gone from the evidence",
          "hunter2-correct-horse" not in out, out)
    check("the app secret is gone from the evidence",
          "s3cr3t-app-value-xyz" not in out, out)
    check("credentials we do NOT know are stripped too",
          "unknown-other-pw" not in out,
          "belt-and-braces: any url with user:pass@ is scrubbed by pattern")
    check("the actual ERROR survives redaction",
          "password authentication failed" in out,
          "a redactor that eats the diagnosis is worse than no envelope at all")

    tail = deployer._tail("x" * 50_000, 8192)
    check("the log tail is capped", len(tail) < 8400 and tail.endswith("x"),
          "the TAIL is kept — the crash is at the end, not the beginning")

    # Measured on the first real failure: one MODULE_NOT_FOUND stack, printed four times by a
    # crash-looping container, ate 3.5 KB of an 8 KB envelope.
    loop = ("Error: Cannot find module './lib/notes-store'\n"
            "    at Module._load (node:internal/modules/cjs/loader:1038:27)\n"
            "Node.js v20.20.2\n") * 12 + "MEANWHILE: redis connection refused\n"
    squashed = deployer._collapse_repeats(loop)
    check("a crash loop is collapsed", squashed.count("Cannot find module") == 2,
          f"kept {squashed.count('Cannot find module')} copies")
    check("the collapse is reported, not silent", "collapsed" in squashed,
          "'it printed this 30 times' IS the diagnosis — the container is crash-looping")
    check("a line the repeats would have pushed out survives",
          "redis connection refused" in squashed,
          "the whole point: the budget goes to DISTINCT evidence")


def test_bounds_are_declared():
    from server import repair, shipper

    check("the repair budget is 2 attempts", repair.MAX_REPAIR_ATTEMPTS == 2)
    src = open(repair.__file__).read()
    check("an identical failure stops immediately, before the budget",
          'decision["repeat"]' in src and src.index('decision["repeat"]')
          < src.index('decision["attempts"] >= MAX_REPAIR_ATTEMPTS'),
          "checked FIRST — retrying an unchanged error is superstition, not debugging")
    check("escalation pauses the assistant and flags the project",
          '"paused"' in src and '"needs_attention"' in src)
    check("the repair beat waits for the failed run to finish",
          "runner.is_active" in src,
          "no deploy storm: a beat that starts early is told 'busy' and wasted")
    check("the failure is delivered BOTH ways",
          "append_message" in src and "start_beat" in src,
          "in the thread for the human AND as the beat's task for the agent")

    ship = open(shipper.__file__).read()
    check("the ship path no longer reverts the repo",
          "roll_back_to" not in ship,
          "fix forward: an automatic revert would fight the repair the agent is writing")
    check("the ship path hands failures to the repair loop",
          "repair.on_deploy_failed(" in ship)
    check("a green deploy closes the episode",
          "repair.on_deploy_healthy(" in ship,
          "otherwise the budget is per-lifetime and the second ever failure escalates")


def test_migration_contract():
    from server.harness import codegen

    for text, where in ((codegen.PLATFORM_CONTRACTS, "the assistants' coding agent"),
                        (codegen.NO_SAAS_RULE, "the build pipeline's codegen")):
        check(f"{where} is told migrations must be backward-compatible",
              "BACKWARD-COMPATIBLE" in text and "DROP COLUMN" in text,
              "both colours share one database; a destructive migration breaks the LIVE app "
              "before the candidate has even been gated")


def main():
    test_normalizer()
    test_signature()
    test_no_secret_leak()
    test_bounds_are_declared()
    test_migration_contract()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for name, why in FAIL:
        print(f"  FAIL {name}" + (f": {why}" if why else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
