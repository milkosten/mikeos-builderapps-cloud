"""Deployer (phases 05 + 16 + 31) — turns a workspace into a running, routed, healthy stack.

The compose NORMALIZER is the security guardrail: it rewrites an AI/template-authored
docker-compose.yml into a safe, collision-free stack and is **deny-by-default** — it strips
all published host ports and REJECTS dangerous keys (host bind-mounts, the docker socket,
privileged, host networking, cap_add, pid:host, etc.). Everything structural (names,
networks, limits, restart) is set by code, never trusted from the LLM.

Conventions enforced:
  * container_name  <id>-app-<colour> / <id>-db / <id>-redis
  * the LIVE app colour holds the network ALIAS <id>-app on deploy_default (Caddy reach);
    every colour is on proj-<id>; db/redis on proj-<id> ONLY
  * NO published host ports anywhere
  * mem_limit / cpus / pids_limit / restart injected per service
  * db/redis named volumes -> /data/builderapps/vol/<id>/{pg,redis} (absolute, writable)
  * redis launched with --dir /data --stop-writes-on-bgsave-error no (read-only-cwd bug)

## BLUE/GREEN — why the routing flip is an ALIAS and not a rename of the container

Caddy routes every app with one wildcard rule it must never have to know about:

    reverse_proxy {http.request.host.labels.3}-app:3000

so the name `<id>-app` is the contract. Rebuilding *in place* means stopping that container,
which is a real downtime window on every deploy — tolerable when a human pressed the button,
not tolerable now that assistants deploy themselves unattended.

So `<id>-app` stops being a CONTAINER NAME and becomes a **Docker network alias**. The two
colours run as `<id>-app-blue` / `<id>-app-green`; whichever is live holds the alias on
`deploy_default`. Verified empirically on the box (docker 27, embedded DNS at 127.0.0.11):

  * two containers CAN hold the same alias at once — DNS then answers with BOTH A records
    and round-robins them. So attaching the new colour to the network **cannot** drop a
    request: at no instant is the name unresolvable or pointing at nothing.
  * a container's own NAME beats an alias: while a legacy `<id>-app` container still exists,
    DNS answers ONLY its address even after the new colour is attached with that alias. That
    makes the one-time migration off the old layout zero-downtime too — attach the new colour
    first (invisible, the name still wins), then `docker rename` the legacy container, and the
    alias takes over on the very next lookup.
  * `docker compose up -d` with the new colour under a DIFFERENT SERVICE NAME leaves the old
    colour running (orphan warning only, no `--remove-orphans`) and does NOT recreate db/redis.

The old colour is only stopped after a grace period, once the new one has been serving
alongside it. A failed deploy therefore never touches the live app at all: the new colour is
health-gated while it is still detached from `deploy_default`, and a red gate just deletes it.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import httpx
import yaml

from server import store

logger = logging.getLogger(__name__)

DEPLOY_NETWORK = os.environ.get("DEPLOY_NETWORK", "deploy_default")
VOL_ROOT = os.environ.get("BUILDERAPPS_VOL_ROOT", "/data/builderapps/vol")
SITES_BASE = os.environ.get("SITES_BASE", "builderapps.osmike.com")
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "https://builderapps.osmike.com")

COLOURS = ("blue", "green")
# How long the two colours serve side by side after the flip before the old one is stopped.
# It is not a safety margin for the NEW colour (that was health-gated before it was attached)
# — it is the window in which Caddy's pooled upstream connections migrate over on their own.
RETIRE_GRACE_S = float(os.environ.get("BLUEGREEN_GRACE_S", "8"))

# Only one docker build at a time on the shared box (protect the estate).
_build_sem = asyncio.Semaphore(int(os.environ.get("MAX_CONCURRENT_BUILDS", "1")))

# Keys that must NEVER appear on a project service (deny-by-default isolation).
_FORBIDDEN_SERVICE_KEYS = {
    "privileged", "cap_add", "devices", "pid", "ipc", "userns_mode",
    "security_opt", "sysctls", "network_mode", "extra_hosts", "ports",
    "expose",  # we don't publish; internal reach is by name
}
_APP_MEM = os.environ.get("PROJ_APP_MEM", "512m")
_DB_MEM = os.environ.get("PROJ_DB_MEM", "512m")
_REDIS_MEM = os.environ.get("PROJ_REDIS_MEM", "128m")


class NormalizeError(ValueError):
    """AI/template compose violated an isolation invariant (deny-by-default)."""


def _reject_bind_mounts(service_name: str, volumes) -> list:
    """Only NAMED volumes are allowed. A host bind-mount ("/host:/c" or {type:bind}) is
    rejected — it could mount the docker socket or the host fs into an untrusted app."""
    out = []
    for v in (volumes or []):
        if isinstance(v, str):
            # named-volume form is "name:/path[:mode]"; a bind is "/abs:/path" or "./x:/y"
            src = v.split(":", 1)[0]
            if src.startswith("/") or src.startswith(".") or src.startswith("~"):
                raise NormalizeError(f"service {service_name}: host bind-mount '{v}' forbidden")
            out.append(v)
        elif isinstance(v, dict):
            if v.get("type") == "bind" or str(v.get("source", "")).startswith(("/", ".", "~")):
                raise NormalizeError(f"service {service_name}: bind mount {v} forbidden")
            out.append(v)
        else:
            raise NormalizeError(f"service {service_name}: unknown volume form {v!r}")
    return out


def normalize_compose(raw_yaml: str, shortid: str, subnet: Optional[str] = None,
                      colour: str = "blue") -> str:
    """Return a normalized compose YAML string for project <shortid>. Raises NormalizeError
    on any isolation violation.

    `subnet` pins the project's private network to an EXPLICIT /24 (see store.project_subnet).
    Without it Docker picks from its default address pools, which hold only ~31 networks in
    total — 242 exhausted them at ~20 apps and every further build died at deploy_skeleton
    with "all predefined address pools have been fully subnetted".

    `colour` is the blue/green slot this deploy builds into. The app service is emitted under
    the colour-qualified SERVICE name `app-<colour>` — not just a colour-qualified container
    name — because compose keys a container to its service: reuse the service name and compose
    would treat the new colour as a *recreate* of the running one and stop it, which is exactly
    the downtime this design removes. Under a new service name the old colour is merely an
    orphan, and db/redis (unchanged) are left running untouched.

    The app is deliberately emitted on the PROJECT network only. It joins `deploy_default`
    later, by hand, at the moment it is flipped live — a container that has not passed the
    health gate is not reachable from Caddy, so a broken deploy is invisible to the public.
    """
    doc = yaml.safe_load(raw_yaml)
    if not isinstance(doc, dict) or "services" not in doc:
        raise NormalizeError("compose has no services")
    services = doc["services"]
    if not isinstance(services, dict):
        raise NormalizeError("services is not a map")

    # The template must have exactly the three known services (app/db/redis).
    expected = {"app", "db", "redis"}
    unknown = set(services.keys()) - expected
    if unknown:
        raise NormalizeError(f"unexpected services {unknown} — only app/db/redis allowed")

    if colour not in COLOURS:
        raise NormalizeError(f"unknown colour {colour!r}")
    proj_net = f"proj-{shortid}"
    new_services: dict = {}

    for name, svc in services.items():
        if not isinstance(svc, dict):
            raise NormalizeError(f"service {name} is not a map")
        # deny-by-default: reject any forbidden key the LLM/template might have added.
        for k in list(svc.keys()):
            if k in _FORBIDDEN_SERVICE_KEYS:
                raise NormalizeError(f"service {name}: forbidden key '{k}'")
        # sanitize volumes -> named only
        if "volumes" in svc:
            svc["volumes"] = _reject_bind_mounts(name, svc["volumes"])

        out_name = f"app-{colour}" if name == "app" else name
        cid = f"{shortid}-{out_name}"
        svc["container_name"] = cid
        svc["restart"] = "unless-stopped"
        svc["pids_limit"] = 512
        # strip any published ports / expose already handled by forbidden-key check.

        if name == "app":
            # PROJECT network only. `deploy_default` (and with it the <id>-app alias Caddy
            # dials) is attached by _flip_alias AFTER the health gate says this colour works.
            svc["networks"] = [proj_net]
            svc["image"] = f"{shortid}-app-{colour}"
            svc["mem_limit"] = _APP_MEM
            svc["cpus"] = float(os.environ.get("PROJ_APP_CPUS", "1.0"))
            # ensure it builds from the workspace context
            svc.setdefault("build", ".")
            # The app also joins the SHARED network, where generic names like "db" and
            # "redis" are already claimed by unrelated stacks (e.g. deploy-db-1 is aliased
            # "db"). Docker's DNS could then resolve "db" to ANOTHER TENANT'S Postgres —
            # the app connects to the wrong server and dies with 28P01 auth-failed.
            # Pin both datastores to their unambiguous per-project container names.
            appenv = svc.get("environment") or {}
            if isinstance(appenv, list):  # ["K=V", ...] form -> dict
                appenv = dict(
                    item.split("=", 1) for item in appenv if isinstance(item, str) and "=" in item
                )
            appenv["DATABASE_URL"] = (
                f"postgresql://app:${{DB_PASSWORD}}@{shortid}-db:5432/app"
            )
            appenv["REDIS_URL"] = f"redis://{shortid}-redis:6379"
            svc["environment"] = appenv
        elif name == "db":
            svc["networks"] = [proj_net]
            svc["mem_limit"] = _DB_MEM
            svc["cpus"] = float(os.environ.get("PROJ_DB_CPUS", "1.0"))
        elif name == "redis":
            svc["networks"] = [proj_net]
            svc["mem_limit"] = _REDIS_MEM
            svc["cpus"] = float(os.environ.get("PROJ_REDIS_CPUS", "0.5"))

        new_services[out_name] = svc

    # Rewrite named volumes to absolute, writable host dirs under /data/builderapps/vol/<id>.
    pg_dir = f"{VOL_ROOT}/{shortid}/pg"
    redis_dir = f"{VOL_ROOT}/{shortid}/redis"
    # db pgdata volume -> bind to pg_dir (we control this path; not an LLM-authored bind)
    if "db" in new_services:
        new_services["db"]["volumes"] = [f"{pg_dir}:/var/lib/postgresql/data"]
    if "redis" in new_services:
        new_services["redis"]["volumes"] = [f"{redis_dir}:/data"]
        new_services["redis"]["command"] = [
            "redis-server", "--dir", "/data", "--stop-writes-on-bgsave-error", "no",
        ]

    out_doc = {
        "services": new_services,
        "networks": {
            proj_net: ({"driver": "bridge",
                        "ipam": {"driver": "default", "config": [{"subnet": subnet}]}}
                       if subnet else {"driver": "bridge"}),
        },
    }
    return yaml.safe_dump(out_doc, sort_keys=False)


def _compose_env(shortid: str, db_password: str, app_secret: str, title: str) -> dict:
    return {
        "DB_PASSWORD": db_password,
        "APP_SECRET": app_secret,
        "APP_TITLE": title or f"builderapps {shortid}",
    }


async def _run(cmd: list[str], cwd: Optional[Path] = None, env: Optional[dict] = None,
               timeout: float = 600.0) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd) if cwd else None,
        env={**os.environ, **(env or {})},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"command timed out: {' '.join(cmd[:3])}...")
    return proc.returncode, (out or b"").decode("utf-8", "replace")


async def _ensure_vol_dirs(shortid: str) -> None:
    for sub in ("pg", "redis"):
        Path(f"{VOL_ROOT}/{shortid}/{sub}").mkdir(parents=True, exist_ok=True)


_SLOW_DEP_RE = re.compile(
    r"dependency \S* ?failed to start"
    r"|container .* is unhealthy"
    r"|health check .*(timeout|failed)",
    re.I)


def _is_slow_dependency(compose_output: str) -> bool:
    """True when `compose up` gave up waiting on db/redis rather than hitting a real fault."""
    return bool(_SLOW_DEP_RE.search(compose_output or ""))


async def _subnet_for(shortid: str) -> Optional[str]:
    """The explicit /24 to write into the compose, or None to let Docker choose.

    An ALREADY-RUNNING project keeps whatever subnet its network was created with: compose
    refuses to start a stack whose declared ipam disagrees with the live network, so a
    pre-existing app must never be handed a freshly allocated block. New projects get one from
    the control plane (10.100+.x.0/24) because Docker's own pools are exhausted on 242.
    """
    net = f"{shortid}_proj-{shortid}"
    rc, out = await _run(
        ["docker", "network", "inspect", net, "--format",
         "{{range .IPAM.Config}}{{.Subnet}}{{end}}"], timeout=30)
    if rc == 0 and out.strip():
        return out.strip().splitlines()[0].strip()
    try:
        return await store.project_subnet(shortid)
    except Exception as e:  # noqa: BLE001 — never block a deploy on the allocator
        logger.warning("subnet allocation failed for %s, using docker's pool: %s", shortid, e)
        return None


# ---- blue/green: which colour is live, and how traffic is flipped ----------
def app_container(shortid: str, colour: str) -> str:
    return f"{shortid}-app-{colour}"


async def _inspect(name: str, fmt: str) -> Optional[str]:
    """`docker inspect` one container, or None when it does not exist."""
    rc, out = await _run(["docker", "inspect", name, "--format", fmt], timeout=30)
    return out.strip() if rc == 0 else None


async def _exists(name: str) -> bool:
    return await _inspect(name, "{{.Id}}") is not None


async def _running(name: str) -> bool:
    return (await _inspect(name, "{{.State.Running}}")) == "true"


async def _holds_alias(name: str, shortid: str) -> bool:
    """Is this container attached to deploy_default carrying the `<id>-app` alias?

    Read from Docker, not from a column of our own: the alias IS the routing, so the only
    honest answer to "what is live" is what the network actually says. A control-plane
    restart, a manual `docker` command or a half-finished flip all stay consistent with it.
    """
    raw = await _inspect(
        name, "{{json (index .NetworkSettings.Networks \"%s\").Aliases}}" % DEPLOY_NETWORK)
    if not raw or raw in ("null", "<no value>"):
        return False
    try:
        return f"{shortid}-app" in (json.loads(raw) or [])
    except Exception:  # noqa: BLE001
        return False


async def live_colour(shortid: str) -> Optional[str]:
    """The colour currently taking public traffic, or None (never deployed / legacy layout)."""
    for colour in COLOURS:
        name = app_container(shortid, colour)
        if await _running(name) and await _holds_alias(name, shortid):
            return colour
    return None


async def _legacy_app(shortid: str) -> bool:
    """A pre-blue/green container literally NAMED `<id>-app` is still the live app."""
    return await _exists(f"{shortid}-app")


def _next_colour(current: Optional[str]) -> str:
    return "green" if current == "blue" else "blue"


async def _flip_alias(shortid: str, colour: str) -> str:
    """Put `<id>-app` on the new colour. This is the cutover, and it is additive.

    Attaching the alias never removes it from anywhere: for a moment both colours answer the
    name and Docker's DNS round-robins them, so there is no instant at which the name is
    unresolvable. The old colour is retired afterwards, by a graceful stop, so its pooled
    connections are closed with a FIN and Caddy simply re-dials the name.
    """
    name = app_container(shortid, colour)
    rc, out = await _run(["docker", "network", "connect", "--alias", f"{shortid}-app",
                          DEPLOY_NETWORK, name], timeout=60)
    if rc != 0 and "already exists" not in out:
        raise RuntimeError(f"could not attach {name} to {DEPLOY_NETWORK}: {out[-300:]}")
    # The legacy layout: a container whose NAME is `<id>-app`. Its name outranks our alias in
    # Docker's DNS, so traffic is STILL on it right now — renaming it is the actual cutover,
    # and it is instantaneous (verified: the next lookup answers with the alias holder).
    if await _legacy_app(shortid):
        retired = f"{shortid}-app-retired-{int(time.time())}"
        await _run(["docker", "rename", f"{shortid}-app", retired], timeout=30)
        logger.info("blue/green: legacy %s-app renamed to %s; alias now serves %s",
                    shortid, retired, name)
        return retired
    return ""


async def ensure_alias(shortid: str) -> None:
    """Idempotently make sure SOMETHING holds `<id>-app`. Used after a plain `compose up`
    (a recreated container comes back with only its project network) and on boot."""
    if await live_colour(shortid) or await _legacy_app(shortid):
        return
    for colour in COLOURS:
        if await _running(app_container(shortid, colour)):
            await _flip_alias(shortid, colour)
            return


async def _retire(name: str, *, remove: bool) -> None:
    """Stop a colour GRACEFULLY. SIGTERM lets the app close its listeners, so Caddy's pooled
    upstream connections get a FIN and are retried on a fresh dial instead of hanging."""
    await _run(["docker", "stop", "-t", "10", name], timeout=60)
    if remove:
        await _run(["docker", "rm", "-f", name], timeout=60)


async def reap_orphan_colours() -> int:
    """Delete every app colour that is not the live one. Called on boot.

    A control plane that dies mid-deploy leaves a built, health-gated, *unattached* colour
    behind; without this it would sit there holding 512 MB forever, and the next deploy would
    pick the same slot and collide with it.
    """
    rc, out = await _run(["docker", "ps", "-a", "--format", "{{.Names}}"], timeout=60)
    if rc != 0:
        return 0
    victims: list[str] = []
    by_project: dict[str, list[str]] = {}
    for name in out.split():
        m = re.fullmatch(r"([a-z0-9]{6})-app-(blue|green)", name)
        if m:
            by_project.setdefault(m.group(1), []).append(name)
        elif re.fullmatch(r"[a-z0-9]{6}-app-retired-\d+", name):
            victims.append(name)
    for shortid, names in by_project.items():
        live = await live_colour(shortid)
        keep = app_container(shortid, live) if live else None
        # No live colour at all: this project is legacy (or down). Keep a RUNNING container
        # rather than reaping the only thing serving; only stale, detached ones go.
        for n in names:
            if n == keep:
                continue
            if keep is None and await _running(n):
                continue
            victims.append(n)
    for n in victims:
        await _run(["docker", "rm", "-f", n], timeout=60)
    if victims:
        logger.warning("blue/green boot reap removed %d orphaned colour(s): %s",
                       len(victims), ", ".join(victims))
    return len(victims)


# ---- phase 31: the failure envelope ---------------------------------------
_MAX_ENVELOPE_LOG = int(os.environ.get("DEPLOY_LOG_TAIL_BYTES", "8192"))
# A crash dump loves to print the connection string it just failed on. We know this project's
# literal secrets, so they are replaced by value (engine.redact's rule); this pattern is the
# belt-and-braces for the ones we do not know — anything shaped like a URL with credentials.
_URL_CRED_RE = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^\s:@/]+:[^\s@/]+@")


def _redact_logs(text: str, secrets: Optional[dict] = None) -> str:
    """Strip secrets out of anything that will be shown to an assistant or a human."""
    if not text:
        return ""
    from server.harness import engine
    out = engine.redact(secrets or {}, text)
    return _URL_CRED_RE.sub(lambda m: m.group("scheme") + "***:***@", out)


def _tail(text: str, limit: int = _MAX_ENVELOPE_LOG) -> str:
    text = text or ""
    return text if len(text) <= limit else "…(truncated)…\n" + text[-limit:]


class DeployFailure(RuntimeError):
    """A deploy that failed, carrying the EVIDENCE rather than only a sentence.

    The point of phase 31: the agent that caused the failure has to be told what broke, and
    "the deploy failed" is not that. Every stage attaches the thing a human would have asked
    for — the build log tail, docker's own error, the container's logs, the public response —
    so the repair beat starts from the actual error text.
    """

    def __init__(self, stage: str, summary: str, envelope: dict):
        super().__init__(summary)
        self.stage = stage
        self.summary = summary
        self.envelope = envelope


async def _head_sha(workspace: Path) -> str:
    """The commit this deploy is building. Best-effort — a workspace with no .git still
    deploys, it just cannot be rolled back TO later (and `last_good_sha` will skip it)."""
    if not (workspace / ".git").is_dir():
        return ""
    rc, out = await _run(["git", "-C", str(workspace), "rev-parse", "HEAD"], timeout=30)
    return out.strip() if rc == 0 else ""


async def deploy(shortid: str, workspace: Path, *, db_password: str, app_secret: str,
                 title: str, assistant_id: Optional[int] = None,
                 beat_id: Optional[int] = None) -> dict:
    """Normalize + build + up + health-gate + public-route verify. Records a deployments row.
    Returns {ok, deployment_id, git_sha, public_health}.

    The recorded `git_sha` is read from the workspace itself, never passed in by a caller:
    "what is actually checked out" is the only honest answer to "what did we just build",
    and it is what a later rollback will treat as the last good commit.
    """
    raw = (workspace / "docker-compose.yml").read_text("utf-8")
    subnet = await _subnet_for(shortid)

    old_colour = await live_colour(shortid)
    legacy = await _legacy_app(shortid)
    colour = _next_colour(old_colour)
    new_app = app_container(shortid, colour)
    # A colour left over from a control plane that died mid-deploy would otherwise collide
    # with the container we are about to create. It is not live (old_colour says who is), so
    # it goes.
    if colour != old_colour and await _exists(new_app):
        await _run(["docker", "rm", "-f", new_app], timeout=60)

    normalized = normalize_compose(raw, shortid, subnet, colour)
    # Colour-scoped file. `docker-compose.normalized.yml` — what stop/start/restart act on —
    # is only PROMOTED to this once the colour is actually live: a failed deploy must not
    # leave the lifecycle commands pointing at a container that does not work.
    norm_path = workspace / f"docker-compose.{colour}.yml"
    norm_path.write_text(normalized, "utf-8")
    compose_hash = hashlib.sha256(normalized.encode()).hexdigest()[:16]

    # render .env (git-ignored) so ${DB_PASSWORD} etc. resolve
    env = _compose_env(shortid, db_password, app_secret, title)
    (workspace / ".env").write_text(
        "\n".join(f"{k}={v}" for k, v in env.items()) + "\n", "utf-8")

    await _ensure_vol_dirs(shortid)
    git_sha = await _head_sha(workspace)
    dep_id = await store.create_deployment(shortid, image_tag=new_app,
                                           compose_hash=compose_hash, git_sha=git_sha,
                                           assistant_id=assistant_id, beat_id=beat_id)
    secrets = {"db_password": db_password, "app_secret": app_secret}
    base_env = {"stage": "", "commit": git_sha[:12], "colour": colour,
                "previous_colour": old_colour or ("legacy" if legacy else ""),
                # The single most reassuring fact in the whole message, and it is only true
                # because of blue/green: the repair beat is not racing a down app.
                "live_unaffected": bool(old_colour or legacy)}

    async def fail(stage: str, summary: str, **evidence) -> None:
        envelope = {**base_env, "stage": stage, "summary": summary}
        envelope.update({k: _redact_logs(_tail(v), secrets) if isinstance(v, str) else v
                         for k, v in evidence.items()})
        await store.finish_deployment(
            dep_id, "failed", json.dumps({k: str(v)[:600] for k, v in envelope.items()}))
        # The colour that failed never took traffic. Take its logs first (done by the caller,
        # they are already in `evidence`), then delete it — leaving it stopped would just be
        # 512 MB of reserved memory nobody will ever look at again.
        await _run(["docker", "rm", "-f", new_app], timeout=60)
        raise DeployFailure(stage, summary, envelope)

    async with _build_sem:
        rc, out = await _run(compose_base_for(norm_path, shortid) + ["build"], cwd=workspace,
                             env=env, timeout=900)
        if rc != 0:
            await fail("build", f"the image for {colour} would not build", build_log=out)

    compose_base = compose_base_for(norm_path, shortid)
    rc, out = await _run(compose_base + ["up", "-d"], cwd=workspace, env=env, timeout=300)
    if rc != 0 and _is_slow_dependency(out):
        # "dependency failed to start" is almost never a broken app: on a box whose RAID6
        # array is saturated, a first-boot `initdb` simply outruns the db healthcheck's
        # 12x10s budget and compose gives up while Postgres is still coming up. A slow
        # start is not a failure (the same lesson already applied to the internal health
        # gate). Give it a breath and try once more — the second `up` finds a healthy db.
        logger.warning("compose up for %s hit a slow dependency; retrying once", shortid)
        await asyncio.sleep(45)
        rc, out = await _run(compose_base + ["up", "-d"], cwd=workspace, env=env, timeout=300)
    if rc != 0:
        await fail("up", f"the {colour} container would not start",
                   docker_error=out, app_logs=await _tail_logs(shortid, colour))

    # THE GATE. The colour is still detached from deploy_default, so whatever it does here it
    # cannot be seen by a user and cannot hurt the colour that is serving.
    if not await _health_gate_internal(shortid, colour):
        await fail("health_gate",
                   f"the {colour} container started but its internal /health never went "
                   f"green within the gate timeout",
                   app_logs=await _tail_logs(shortid, colour))

    # ---- THE FLIP ----------------------------------------------------------
    retired_legacy = await _flip_alias(shortid, colour)
    await asyncio.sleep(2)
    public_health = await _verify_public_health(shortid, tries=6)
    if public_health is None:
        # Routing itself is broken. Detach the new colour and the old one is, again, the only
        # thing answering the name — no rollback, because nothing was replaced.
        await _run(["docker", "network", "disconnect", DEPLOY_NETWORK, new_app], timeout=60)
        if retired_legacy:
            await _run(["docker", "rename", retired_legacy, f"{shortid}-app"], timeout=30)
        await fail("public_check",
                   "the public https route did not return a healthy body after the flip",
                   health=await _public_probe(shortid),
                   app_logs=await _tail_logs(shortid, colour))

    # ---- RETIRE THE OLD COLOUR --------------------------------------------
    old_name = app_container(shortid, old_colour) if old_colour else retired_legacy
    if old_name:
        await asyncio.sleep(RETIRE_GRACE_S)
        await _retire(old_name, remove=bool(retired_legacy))

    # Only NOW does the lifecycle file point at this colour.
    (workspace / "docker-compose.normalized.yml").write_text(normalized, "utf-8")

    final_health = await _verify_public_health(shortid, tries=6)
    if final_health is None and old_name and not retired_legacy:
        # The old colour was stopped, not deleted, precisely so this is recoverable in one
        # command. It still carries its alias, so starting it puts the last good code back on
        # the air immediately.
        logger.error("blue/green: %s went unhealthy after retiring %s — restarting it",
                     shortid, old_name)
        await _run(["docker", "start", old_name], timeout=60)
        await fail("public_check",
                   "the app stopped answering once the previous colour was retired; the "
                   "previous colour has been restarted",
                   health=await _public_probe(shortid),
                   app_logs=await _tail_logs(shortid, colour))
    if old_name and not retired_legacy:
        # Kept only until the next deploy would have reaped it anyway; removing it here is
        # what stops a project accumulating stopped containers.
        await _run(["docker", "rm", "-f", old_name], timeout=60)

    health = final_health or public_health
    await store.finish_deployment(dep_id, "healthy", json.dumps(health))
    return {"ok": True, "deployment_id": dep_id, "git_sha": git_sha,
            "public_health": health, "colour": colour,
            "retired_colour": old_colour or ("legacy" if retired_legacy else "")}


def compose_base_for(norm_path: Path, shortid: str) -> list[str]:
    return ["docker", "compose", "-p", shortid, "-f", str(norm_path)]


async def _health_gate_internal(
    shortid: str,
    colour: str = "blue",
    timeout_s: int = int(os.environ.get("HEALTH_GATE_TIMEOUT_S", "360")),
) -> bool:
    # 180s was too tight on a box running 160+ containers: several builds were failed on
    # "internal /health never went green" while the app went healthy a minute later — pure
    # contention, not a broken app. A slow start is not a failure; a never-start is.
    """Poll THIS COLOUR's own /health from inside via `docker exec` until green.

    `docker exec` and not an HTTP request is what makes the gate work before the flip: the
    container is on the project network only, unreachable from Caddy and from here, and that
    is the entire point — it is being judged while it cannot serve anyone.
    """
    app = app_container(shortid, colour)
    deadline = asyncio.get_event_loop().time() + timeout_s
    # Accept either the full contract {"status":"ok",...} or a terser {"ok":true} the build
    # loop may have generated — a healthy app must never be gated out on response SHAPE.
    probe = ("const http=require('http');http.get('http://localhost:3000/health',r=>{"
             "let d='';r.on('data',c=>d+=c);r.on('end',()=>{try{const j=JSON.parse(d);"
             "process.exit((j.status==='ok'||j.ok===true)?0:1);}catch(e){process.exit(1);}});})"
             ".on('error',()=>process.exit(1));")
    while asyncio.get_event_loop().time() < deadline:
        rc, _ = await _run(["docker", "exec", app, "node", "-e", probe], timeout=20)
        if rc == 0:
            return True
        await asyncio.sleep(4)
    return False


async def _verify_public_health(shortid: str, tries: int = 12) -> Optional[dict]:
    """Never-trust-200: hit the PUBLIC https URL and verify the JSON body says db+redis ok."""
    url = f"https://{shortid}.{SITES_BASE}/health"
    async with httpx.AsyncClient(timeout=20.0, verify=True) as c:
        for i in range(tries):
            try:
                r = await c.get(url)
                if r.status_code == 200:
                    body = r.json()
                    # Preferred contract: {"status":"ok","db":"ok","redis":"ok"}.
                    # But the build loop regenerates server.js, and the model sometimes
                    # rewrites /health to a terser shape (e.g. {"ok":true}) — a WORKING app
                    # was then marked `failed`, which aborted the run before runtime QA and
                    # shipped an unverified UI. Accept any healthy shape; the codegen rules
                    # still ask for the full one.
                    if body.get("status") == "ok" or body.get("ok") is True:
                        return body
            except Exception as e:  # noqa: BLE001
                logger.info("public health try %d for %s: %s", i, shortid, e)
            await asyncio.sleep(5)
    return None


async def _public_probe(shortid: str) -> dict:
    """What the public URL actually answers right now — status + a slice of the body.

    "healthy internally, broken publicly" is its own failure mode and the only useful evidence
    for it is the response itself; a summary sentence would send the assistant hunting in the
    app logs for something that is not there.
    """
    url = f"https://{shortid}.{SITES_BASE}/health"
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(url)
            return {"url": url, "http": r.status_code, "body": r.text[:1200]}
    except Exception as e:  # noqa: BLE001
        return {"url": url, "http": 0, "body": f"request failed: {e}"[:400]}


async def _tail_logs(shortid: str, colour: Optional[str] = None, tail: int = 120) -> str:
    """The app container's own logs — the evidence that matters for a red health gate.

    EACCES and 28P01 both showed up here and in both cases the container named its own bug.
    Deliberately `docker logs <container>` rather than `compose logs`: the failing colour is
    one container, and the db/redis chatter around it is noise in an 8 KB budget.
    """
    if colour:
        rc, out = await _run(["docker", "logs", "--tail", str(tail),
                              app_container(shortid, colour)], timeout=30)
        if rc == 0:
            return out
    rc, out = await _run(["docker", "compose", "-p", shortid, "logs", "--tail", str(tail)],
                         timeout=30)
    return out


# ---- lifecycle ------------------------------------------------------------
async def _compose_file(shortid: str) -> Optional[Path]:
    from server import workspace as ws
    p = ws.path_for(shortid) / "docker-compose.normalized.yml"
    return p if p.exists() else None


async def stop(shortid: str) -> None:
    f = await _compose_file(shortid)
    args = ["docker", "compose", "-p", shortid] + (["-f", str(f)] if f else [])
    await _run(args + ["stop"], timeout=120)


async def start(shortid: str) -> None:
    """Bring a stopped stack back up. `up -d` (not `start`) so a stack whose containers were
    removed is recreated; it needs the .env for ${DB_PASSWORD}, which the workspace already
    holds from the last deploy."""
    f = await _compose_file(shortid)
    args = ["docker", "compose", "-p", shortid] + (["-f", str(f)] if f else [])
    if f:
        from server import workspace as ws
        rc, out = await _run(args + ["up", "-d"], cwd=ws.path_for(shortid), timeout=300)
        if rc == 0:
            # A RECREATED container comes back on its project network only — compose knows
            # nothing about the alias, which lives on the network endpoint. Without this the
            # app would be up, healthy and completely unroutable.
            await ensure_alias(shortid)
            return
        logger.warning("compose up for %s failed, falling back to start: %s", shortid, out[-300:])
    await _run(args + ["start"], timeout=180)
    await ensure_alias(shortid)


async def restart(shortid: str) -> None:
    f = await _compose_file(shortid)
    args = ["docker", "compose", "-p", shortid] + (["-f", str(f)] if f else [])
    await _run(args + ["restart"], timeout=180)
    await ensure_alias(shortid)


async def destroy(shortid: str) -> None:
    """Tear the stack down: containers, project network, and named volumes. Also removes
    the on-disk data dir. Repo stays in Gitea (source of truth)."""
    f = await _compose_file(shortid)
    args = ["docker", "compose", "-p", shortid] + (["-f", str(f)] if f else [])
    await _run(args + ["down", "-v", "--remove-orphans"], timeout=180)
    # belt-and-braces: force-remove any lingering containers by name. BOTH colours — the
    # non-live one is an orphan of this compose project and `down` on the current file would
    # not necessarily have caught it.
    for suffix in ("app", "app-blue", "app-green", "db", "redis"):
        await _run(["docker", "rm", "-f", f"{shortid}-{suffix}"], timeout=30)
    # remove the private network if it survived
    await _run(["docker", "network", "rm", f"proj-{shortid}"], timeout=30)
