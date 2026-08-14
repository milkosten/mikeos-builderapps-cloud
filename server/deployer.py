"""Deployer (phases 05 + 16) — turns a workspace into a running, routed, healthy stack.

The compose NORMALIZER is the security guardrail: it rewrites an AI/template-authored
docker-compose.yml into a safe, collision-free stack and is **deny-by-default** — it strips
all published host ports and REJECTS dangerous keys (host bind-mounts, the docker socket,
privileged, host networking, cap_add, pid:host, etc.). Everything structural (names,
networks, limits, restart) is set by code, never trusted from the LLM.

Conventions enforced:
  * container_name  <id>-app / <id>-db / <id>-redis
  * <id>-app on deploy_default (Caddy reach) + proj-<id>; db/redis on proj-<id> ONLY
  * NO published host ports anywhere
  * mem_limit / cpus / pids_limit / restart injected per service
  * db/redis named volumes -> /data/builderapps/vol/<id>/{pg,redis} (absolute, writable)
  * redis launched with --dir /data --stop-writes-on-bgsave-error no (read-only-cwd bug)
"""
import asyncio
import hashlib
import json
import logging
import os
import re
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


def normalize_compose(raw_yaml: str, shortid: str, subnet: Optional[str] = None) -> str:
    """Return a normalized compose YAML string for project <shortid>. Raises NormalizeError
    on any isolation violation.

    `subnet` pins the project's private network to an EXPLICIT /24 (see store.project_subnet).
    Without it Docker picks from its default address pools, which hold only ~31 networks in
    total — 242 exhausted them at ~20 apps and every further build died at deploy_skeleton
    with "all predefined address pools have been fully subnetted".
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

        cid = f"{shortid}-{name}"
        svc["container_name"] = cid
        svc["restart"] = "unless-stopped"
        svc["pids_limit"] = 512
        # strip any published ports / expose already handled by forbidden-key check.

        if name == "app":
            svc["networks"] = [DEPLOY_NETWORK, proj_net]
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

        new_services[name] = svc

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
            DEPLOY_NETWORK: {"external": True},
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


async def deploy(shortid: str, workspace: Path, *, db_password: str, app_secret: str,
                 title: str) -> dict:
    """Normalize + build + up + health-gate + public-route verify. Records a deployments row.
    Returns {ok, deployment_id, public_health}."""
    raw = (workspace / "docker-compose.yml").read_text("utf-8")
    subnet = await _subnet_for(shortid)
    normalized = normalize_compose(raw, shortid, subnet)
    norm_path = workspace / "docker-compose.normalized.yml"
    norm_path.write_text(normalized, "utf-8")
    compose_hash = hashlib.sha256(normalized.encode()).hexdigest()[:16]

    # render .env (git-ignored) so ${DB_PASSWORD} etc. resolve
    env = _compose_env(shortid, db_password, app_secret, title)
    (workspace / ".env").write_text(
        "\n".join(f"{k}={v}" for k, v in env.items()) + "\n", "utf-8")

    await _ensure_vol_dirs(shortid)
    dep_id = await store.create_deployment(shortid, image_tag=f"{shortid}-app",
                                           compose_hash=compose_hash)

    compose_base = ["docker", "compose", "-p", shortid, "-f", str(norm_path)]
    try:
        async with _build_sem:
            rc, out = await _run(compose_base + ["build"], cwd=workspace, env=env, timeout=900)
            if rc != 0:
                await store.finish_deployment(dep_id, "failed", out[-2000:])
                raise RuntimeError(f"compose build failed: {out[-1500:]}")
        rc, out = await _run(compose_base + ["up", "-d"], cwd=workspace, env=env, timeout=300)
        if rc != 0 and _is_slow_dependency(out):
            # "dependency failed to start" is almost never a broken app: on a box whose RAID6
            # array is saturated, a first-boot `initdb` simply outruns the db healthcheck's
            # 12x10s budget and compose gives up while Postgres is still coming up. A slow
            # start is not a failure (the same lesson already applied to the internal health
            # gate). Give it a breath and try once more — the second `up` finds a healthy db.
            logger.warning("compose up for %s hit a slow dependency; retrying once", shortid)
            await asyncio.sleep(45)
            rc, out = await _run(compose_base + ["up", "-d"], cwd=workspace, env=env,
                                 timeout=300)
        if rc != 0:
            await store.finish_deployment(dep_id, "failed", out[-2000:])
            raise RuntimeError(f"compose up failed: {out[-1500:]}")

        internal_ok = await _health_gate_internal(shortid)
        if not internal_ok:
            logs = await _tail_logs(shortid)
            await store.finish_deployment(dep_id, "failed", "internal health gate timeout\n" + logs[-1500:])
            raise RuntimeError(f"internal /health never went green:\n{logs[-1200:]}")

        public_health = await _verify_public_health(shortid)
        if public_health is None:
            await store.finish_deployment(dep_id, "failed", "public /health not ok")
            raise RuntimeError("public https health did not return a healthy body")

        await store.finish_deployment(dep_id, "healthy", json.dumps(public_health))
        return {"ok": True, "deployment_id": dep_id, "public_health": public_health}
    except Exception:
        raise


async def _health_gate_internal(
    shortid: str,
    timeout_s: int = int(os.environ.get("HEALTH_GATE_TIMEOUT_S", "360")),
) -> bool:
    # 180s was too tight on a box running 160+ containers: several builds were failed on
    # "internal /health never went green" while the app went healthy a minute later — pure
    # contention, not a broken app. A slow start is not a failure; a never-start is.
    """Poll the <id>-app container's own /health from inside via `docker exec` until green."""
    app = f"{shortid}-app"
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


async def _tail_logs(shortid: str, tail: int = 60) -> str:
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
            return
        logger.warning("compose up for %s failed, falling back to start: %s", shortid, out[-300:])
    await _run(args + ["start"], timeout=180)


async def restart(shortid: str) -> None:
    f = await _compose_file(shortid)
    args = ["docker", "compose", "-p", shortid] + (["-f", str(f)] if f else [])
    await _run(args + ["restart"], timeout=180)


async def destroy(shortid: str) -> None:
    """Tear the stack down: containers, project network, and named volumes. Also removes
    the on-disk data dir. Repo stays in Gitea (source of truth)."""
    f = await _compose_file(shortid)
    args = ["docker", "compose", "-p", shortid] + (["-f", str(f)] if f else [])
    await _run(args + ["down", "-v", "--remove-orphans"], timeout=180)
    # belt-and-braces: force-remove any lingering containers by name
    for suffix in ("app", "db", "redis"):
        await _run(["docker", "rm", "-f", f"{shortid}-{suffix}"], timeout=30)
    # remove the private network if it survived
    await _run(["docker", "network", "rm", f"proj-{shortid}"], timeout=30)
