"""Run ONE `codex exec` on Mike's flat-rate ChatGPT plan, inside a throwaway jail container.

Shared by the 242 app builders (buildapps, builderapps). Mike's standing rule: flat-rate
subscriptions only (Codex / ZAI), never a pay-per-request API key.

The jail (`osmike-codex-runner:latest`, built from /opt/osmike-codex-runner on 242):
  * Codex CLI 0.166.0 + codex-code-mode-host. Account failover is done HERE (same rule as the
    fleet `codex-failover` wrapper: a usage-limit answer reruns the prompt on the other ChatGPT
    account, and the account that works stays sticky). The wrapper itself is in the image too,
    but it buffers all output until the run ends, which would kill live streaming in the UIs.
  * Tokens: since 2026-11-25 the jail holds NO real ChatGPT token. It mounts a dummy CODEX_HOME
    (CODEX_PROXY_HOME, e.g. /opt/osmike-codex/clients/<service>: auth.json with a per-service
    proxy key, config.toml pointing openai_base_url at osmike-codex-proxy on 172.17.0.1:8780).
    The proxy holds the two logins, injects the real token, refreshes it and does the A->B
    switch on a usage limit (osmike-cli/codex-proxy). Fallback (CODEX_PROXY_FALLBACK=1, the
    default for now): if the proxy is not healthy, the old way below is used.
  * Old way (no CODEX_PROXY_HOME, or proxy down): the two ChatGPT logins are bind-mounted RW
    /opt/osmike-codex/a -> /root/.codex-osmike, /opt/osmike-codex/b -> /root/.codex-westisland.
  * ONLY the one workspace the run is about is mounted (at /work, read-only when asked). No docker
    socket, no service env, no database, default bridge network, all capabilities dropped.

A run is stateless (`--ephemeral`): the caller puts everything the model needs in the prompt.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

IMAGE = os.environ.get("CODEX_RUNNER_IMAGE", "osmike-codex-runner:latest")
AUTH_A = os.environ.get("CODEX_AUTH_A", "/opt/osmike-codex/a")
AUTH_B = os.environ.get("CODEX_AUTH_B", "/opt/osmike-codex/b")
STATE_DIR = os.environ.get("CODEX_STATE_DIR", "/opt/osmike-codex/state")
MAX_PARALLEL = int(os.environ.get("CODEX_MAX_PARALLEL", "3"))
MEMORY = os.environ.get("CODEX_RUNNER_MEMORY", "2g")
CPUS = os.environ.get("CODEX_RUNNER_CPUS", "2")
SOURCE_ID = os.environ.get("CODEX_TELEMETRY_SOURCE", "codex-service")
PROXY_HOME = os.environ.get("CODEX_PROXY_HOME", "")          # host path of the dummy CODEX_HOME
PROXY_URL = os.environ.get("CODEX_PROXY_URL", "http://172.17.0.1:8780").rstrip("/")
PROXY_FALLBACK = os.environ.get("CODEX_PROXY_FALLBACK", "1") == "1"
_proxy_ok_until = 0.0


def _use_proxy() -> bool:
    """Proxy mode when configured and healthy (health cached 30 s). Without the fallback the
    proxy is used even when it looks down, so a run fails instead of touching real tokens."""
    global _proxy_ok_until
    if not PROXY_HOME:
        return False
    if not PROXY_FALLBACK:
        return True
    now = time.monotonic()
    if now < _proxy_ok_until:
        return True
    try:
        with urllib.request.urlopen(PROXY_URL + "/healthz", timeout=3) as r:
            ok = r.status == 200 and json.loads(r.read()).get("ok")
    except Exception:  # noqa: BLE001
        ok = False
    if ok:
        _proxy_ok_until = now + 30
        return True
    logger.error("osmike-codex-proxy at %s not healthy; falling back to mounted logins", PROXY_URL)
    return False

_sem: Optional[asyncio.Semaphore] = None


def _semaphore() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(MAX_PARALLEL)
    return _sem


@dataclass
class CodexResult:
    text: str = ""                       # the LAST agent message (the final answer)
    messages: List[str] = field(default_factory=list)  # every agent message, in order
    usage: Dict[str, int] = field(default_factory=dict)
    thread_id: str = ""
    error: str = ""
    returncode: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.messages) and not self.error


def _report_tokens(usage: Dict[str, int], source_id: str) -> None:
    """Fleet telemetry (grafana.osmike.com), best-effort, off the event loop."""
    key = os.environ.get("SVC_TELEMETRY_KEY")
    if not key or not usage:
        return
    payload = {"source_type": "service", "source_id": source_id,
               "box": os.environ.get("SVC_TELEMETRY_BOX", "242"), "model": "codex",
               "usage": {"input_tokens": int(usage.get("input_tokens") or 0),
                         "cached_input_tokens": int(usage.get("cached_input_tokens") or 0),
                         "output_tokens": int(usage.get("output_tokens") or 0),
                         "reasoning_output_tokens": int(usage.get("reasoning_output_tokens") or 0)}}

    def send() -> None:
        try:
            req = urllib.request.Request(
                "https://telemetry-api.osmike.com/v1/ingest", data=json.dumps(payload).encode(),
                headers={"X-API-KEY": key, "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read(256)
        except Exception:  # noqa: BLE001 — accounting must never break a build
            pass

    threading.Thread(target=send, name="codex-telemetry", daemon=True).start()


HOMES = ["/root/.codex-osmike", "/root/.codex-westisland"]  # account A, account B
PROXY_JAIL_HOME = "/root/.codex"  # the dummy CODEX_HOME inside the jail (proxy mode)
_current = 0  # sticky: index into HOMES of the account that last worked
_LIMIT_WORDS = ("usage limit", "hit your usage")


def _docker_argv(name: str, home: str, workdir: Optional[str], readonly: bool,
                 effort: Optional[str], schema: Optional[Dict[str, Any]]) -> List[str]:
    argv = ["docker", "run", "--rm", "-i", "--name", name, "-e", f"CODEX_HOME={home}",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", MEMORY, "--cpus", CPUS, "--pids-limit", "512",
            "--label", "osmike.role=codex-run"]
    if home == PROXY_JAIL_HOME:
        # dummy login only; the proxy adds the real token outside the jail
        argv += ["-v", f"{PROXY_HOME}:{PROXY_JAIL_HOME}",
                 "-e", f"CODEX_REFRESH_TOKEN_URL_OVERRIDE={PROXY_URL}/oauth/token"]
    else:
        argv += ["-v", f"{AUTH_A}:/root/.codex-osmike",
                 "-v", f"{AUTH_B}:/root/.codex-westisland",
                 "-v", f"{STATE_DIR}:/data"]
    if workdir:
        argv += ["-v", f"{workdir}:/work" + (":ro" if readonly else "")]
    codex = ["exec", "--json", "--ephemeral", "--dangerously-bypass-approvals-and-sandbox",
             "--skip-git-repo-check", "-C", "/work"]
    if effort:
        codex += ["-c", f'model_reasoning_effort="{effort}"']
    if schema is not None:
        codex += ["--output-schema", "/tmp/schema.json"]
        argv += ["-e", "CODEX_SCHEMA=" + json.dumps(schema), "--entrypoint", "sh", IMAGE, "-c",
                 'printf %s "$CODEX_SCHEMA" > /tmp/schema.json && exec /usr/local/bin/codex "$@"',
                 "codex"] + codex + ["-"]
    else:
        argv += ["--entrypoint", "/usr/local/bin/codex", IMAGE] + codex + ["-"]
    return argv


async def _kill(name: str) -> None:
    try:
        p = await asyncio.create_subprocess_exec("docker", "kill", name,
                                                 stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(p.wait(), timeout=20)
    except Exception:  # noqa: BLE001
        pass


async def run(prompt: str, *, workdir: Optional[str] = None, readonly: bool = False,
              timeout: float = 900.0, effort: Optional[str] = None,
              schema: Optional[Dict[str, Any]] = None,
              on_event: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
              source_id: Optional[str] = None) -> CodexResult:
    """One Codex run. `workdir` is a HOST path (same path inside the calling container) mounted
    at /work; omit it for a pure text answer. `on_event(item)` is awaited for every completed
    item (agent_message, command_execution, file_change, ...). Never raises on a model failure:
    check `result.ok` / `result.error`."""
    global _current
    if await asyncio.to_thread(_use_proxy):
        async with _semaphore():
            res = await _run_once(PROXY_JAIL_HOME, prompt, workdir, readonly, timeout, effort,
                                  schema, on_event)
        _report_tokens(res.usage, source_id or SOURCE_ID)
        return res
    async with _semaphore():
        first = _current
        res = await _run_once(HOMES[first], prompt, workdir, readonly, timeout, effort, schema,
                              on_event)
        if not res.messages and any(w in res.error.lower() for w in _LIMIT_WORDS):
            other = 1 - first
            logger.warning("codex account %s hit its usage limit; retrying on %s",
                           HOMES[first], HOMES[other])
            res2 = await _run_once(HOMES[other], prompt, workdir, readonly, timeout, effort,
                                   schema, on_event)
            if res2.messages:
                _current = other
            res2.usage = {k: res.usage.get(k, 0) + res2.usage.get(k, 0)
                          for k in set(res.usage) | set(res2.usage)}
            res = res2
    _report_tokens(res.usage, source_id or SOURCE_ID)
    return res


async def _run_once(home: str, prompt: str, workdir: Optional[str], readonly: bool,
                    timeout: float, effort: Optional[str], schema: Optional[Dict[str, Any]],
                    on_event: Optional[Callable[[Dict[str, Any]], Awaitable[None]]]) -> CodexResult:
    res = CodexResult()
    name = f"codexrun-{uuid.uuid4().hex[:12]}"
    argv = _docker_argv(name, home, workdir, readonly, effort, schema)
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024)
    stderr_tail: List[bytes] = []

    async def feed() -> None:
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
        finally:
            proc.stdin.close()

    async def drain_err() -> None:
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            stderr_tail.append(chunk)
            del stderr_tail[:-4]

    async def read_out() -> None:
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            try:
                ev = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            t = ev.get("type")
            if t == "thread.started":
                res.thread_id = ev.get("thread_id") or ""
            elif t == "item.completed":
                item = ev.get("item") or {}
                if item.get("type") == "agent_message" and (item.get("text") or "").strip():
                    res.messages.append(item["text"])
                if on_event:
                    try:
                        await on_event(item)
                    except Exception:  # noqa: BLE001 — a UI hiccup must not kill the run
                        logger.debug("codex on_event failed", exc_info=True)
            elif t == "turn.completed":
                u = ev.get("usage") or {}
                for k, v in u.items():
                    if isinstance(v, int):
                        res.usage[k] = res.usage.get(k, 0) + v
            elif t in ("error", "turn.failed"):
                err = ev.get("message") or (ev.get("error") or {}).get("message") or str(ev)
                res.error = str(err)[:1000]

    tasks = [asyncio.create_task(feed()), asyncio.create_task(drain_err()),
             asyncio.create_task(read_out())]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)
        res.returncode = await asyncio.wait_for(proc.wait(), timeout=30)
    except asyncio.TimeoutError:
        await _kill(name)
        res.error = res.error or f"codex run timed out after {int(timeout)}s"
        res.returncode = -1
    except asyncio.CancelledError:
        await _kill(name)
        raise
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        if proc.returncode is None:
            await _kill(name)
    # A late error event (e.g. a failed first account) is superseded by a real answer.
    if res.messages and res.error and res.returncode == 0:
        res.error = ""
    if not res.messages and not res.error:
        tail = b"".join(stderr_tail).decode("utf-8", "replace").strip()[-600:]
        res.error = f"codex returned no answer (exit {res.returncode}): {tail}"
    res.text = res.messages[-1] if res.messages else ""
    return res


def parse_json(text: str) -> Any:
    """The JSON object in a model answer (tolerates ``` fences and leading prose)."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        pass
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j > i:
        return json.loads(t[i:j + 1])
    raise ValueError("no JSON object in the answer")
