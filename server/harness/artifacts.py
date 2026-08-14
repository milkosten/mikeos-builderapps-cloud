"""Never lose work (phase 27) — every raw LLM response + tool transcript hits disk.

Mike's explicit requirement, learned the hard way: a build that took 20 minutes and eleven
good features died because one response was judged unusable and *discarded*, leaving nothing
to salvage or debug. So the agentic loop writes each turn to

    /opt/builderapps/artifacts/<project>/<run>/<step>-<attempt>.jsonl

as JSON lines, **before** the response is parsed or acted on. One line per event:

    {"ts":…, "kind":"seed"|"llm_response"|"tool_call"|"result"|"error"|"end", …}

Properties that matter:
* **Best-effort, never fatal.** A full disk or a missing mount must not fail a build, so
  every write is wrapped — artifacts are a debugging aid, not a dependency.
* **Bounded.** Each recorded value is truncated (a 500 KB file body in a transcript helps
  nobody), and retention keeps only the last N runs per project so /opt cannot grow forever.
* **Owner-scoped.** The tree is created 0700: it contains generated source, never secrets —
  but it is not world-readable regardless.
"""
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

ARTIFACTS_ROOT = Path(os.environ.get("ARTIFACTS_ROOT", "/opt/builderapps/artifacts"))
KEEP_RUNS = int(os.environ.get("BUILDERAPPS_ARTIFACT_KEEP_RUNS", "12"))
MAX_VALUE_CHARS = 20000        # per recorded field
MAX_LINE_CHARS = 60000         # per JSONL line


def _clip(v: Any, limit: int = MAX_VALUE_CHARS) -> Any:
    if isinstance(v, str) and len(v) > limit:
        return v[:limit] + f"…[+{len(v) - limit} chars]"
    if isinstance(v, dict):
        return {k: _clip(x, limit) for k, x in v.items()}
    if isinstance(v, list):
        return [_clip(x, limit) for x in v[:50]]
    return v


class Recorder:
    """One step-attempt's transcript file."""

    def __init__(self, project_id: str, run_id: int, step: str, attempt: int = 0):
        self.project_id = project_id
        self.run_id = int(run_id or 0)
        self.step = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (step or "step"))
        self.attempt = int(attempt or 0)
        self.path: Optional[Path] = None
        try:
            d = ARTIFACTS_ROOT / project_id / str(self.run_id)
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path = d / f"{self.step}-{self.attempt}.jsonl"
        except Exception as e:  # noqa: BLE001 — artifacts must never fail a build
            logger.warning("artifacts unavailable (%s); continuing without them", e)

    def write(self, kind: str, **payload: Any) -> None:
        if self.path is None:
            return
        try:
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind}
            rec.update({k: _clip(v) for k, v in payload.items()})
            line = json.dumps(rec, ensure_ascii=False, default=str)[:MAX_LINE_CHARS]
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as e:  # noqa: BLE001
            logger.info("artifact write skipped: %s", e)

    def __str__(self) -> str:
        return str(self.path) if self.path else "(no artifact)"


def prune(project_id: str, keep: int = KEEP_RUNS) -> None:
    """Keep only the newest `keep` run directories for this project."""
    try:
        base = ARTIFACTS_ROOT / project_id
        if not base.is_dir():
            return
        runs = sorted((d for d in base.iterdir() if d.is_dir()),
                      key=lambda d: d.stat().st_mtime, reverse=True)
        for old in runs[max(1, keep):]:
            shutil.rmtree(old, ignore_errors=True)
    except Exception as e:  # noqa: BLE001
        logger.info("artifact prune skipped: %s", e)


def list_runs(project_id: str) -> list[str]:
    try:
        base = ARTIFACTS_ROOT / project_id
        return sorted((d.name for d in base.iterdir() if d.is_dir()), reverse=True)
    except Exception:  # noqa: BLE001
        return []
