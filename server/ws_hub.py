"""The browser's live channel — a per-project fan-out of what already happened.

Read the direction carefully, because it is the whole design: **this is not how assistants
talk to each other.** Assistants talk through `assistant_messages` + a wake, entirely
server-side, whether or not a browser exists. This hub only pushes what has ALREADY been
written to Postgres out to whoever happens to be looking at `/builder`, replacing the ~2.5s
poll that used to ask "anything new?" forty times a minute and hear "no" thirty-nine times.

Consequences of that direction, and they are the good ones:

* **Nothing is lost when nobody is connected.** There is no queue here and no delivery
  guarantee, because there is nothing to guarantee — the database already holds it, and
  reopening `/builder` rebuilds the whole exchange through the same restore path that has
  always brought back beats and steps.
* **A publish can never fail an operation.** Every `publish` is best-effort and swallows its
  own errors. A dead socket must not roll back a message that was successfully stored, and a
  slow reader must not hold up a beat.
* **The poll stays.** The client keeps it as a fallback and re-arms it the moment the socket
  drops. A pane frozen behind a half-open TCP connection is worse than one that polls.

Process scope: one hub per control-plane process. There is exactly one control-plane
container, so that is the whole estate; if that ever stops being true this becomes a
Postgres LISTEN/NOTIFY fan-out and nothing else changes, because publishers already only
call `publish(project_id, event)`.
"""
import asyncio
import json
import logging
from typing import Any, Dict, Set

logger = logging.getLogger(__name__)

# project_id -> the sockets watching it. A plain dict of sets: subscriber counts here are
# "how many tabs does one person have open", not a scaling problem.
_subs: Dict[str, Set[Any]] = {}

# A slow client must not become the control plane's problem. Each socket gets a small queue
# and its own writer task; overflow drops the OLDEST frame and marks the client stale, and a
# stale client's next reconnect rebuilds from Postgres anyway.
_QUEUE_MAX = 64


class Client:
    """One browser socket. Owns its own send queue so a publisher never awaits a network."""

    __slots__ = ("ws", "project_id", "queue", "task", "dropped")

    def __init__(self, ws: Any, project_id: str):
        self.ws = ws
        self.project_id = project_id
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        self.task: Any = None
        self.dropped = 0

    def offer(self, payload: str) -> None:
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            # Drop the oldest, keep the newest: for a live pane the most recent state is the
            # one worth having, and the client resyncs on reconnect regardless.
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(payload)
            except Exception:  # noqa: BLE001
                pass
            self.dropped += 1

    async def pump(self) -> None:
        while True:
            payload = await self.queue.get()
            await self.ws.send_text(payload)


def subscribe(ws: Any, project_id: str) -> Client:
    c = Client(ws, project_id)
    _subs.setdefault(project_id, set()).add(c)
    c.task = asyncio.create_task(c.pump())
    return c


def unsubscribe(c: Client) -> None:
    peers = _subs.get(c.project_id)
    if peers:
        peers.discard(c)
        if not peers:
            _subs.pop(c.project_id, None)
    if c.task and not c.task.done():
        c.task.cancel()


def watchers(project_id: str) -> int:
    return len(_subs.get(project_id) or ())


def publish(project_id: str, event: dict) -> int:
    """Fan one event out to every browser watching this project. Never raises, never blocks.

    Synchronous on purpose: it is called from the middle of a beat, a send and a scheduler
    tick, and none of those should have to `await` — nor should any of them be able to fail
    because a browser went away.
    """
    peers = _subs.get(project_id)
    if not peers:
        return 0
    try:
        payload = json.dumps(event, default=str)
    except Exception:  # noqa: BLE001
        logger.debug("ws publish: unserializable event", exc_info=True)
        return 0
    for c in list(peers):
        c.offer(payload)
    return len(peers)


def publish_to(client: Client, event: dict) -> None:
    """Send to ONE socket — the connect-time snapshot, and the pong.

    Separate from `publish` because a snapshot must not go to every tab watching the project:
    a second browser connecting would otherwise repaint everyone else's pane for no reason.
    """
    try:
        client.offer(json.dumps(event, default=str))
    except Exception:  # noqa: BLE001
        logger.debug("ws publish_to failed", exc_info=True)


async def close_all() -> None:
    """Shutdown: stop the pumps. The sockets themselves are closed by the endpoint."""
    for peers in list(_subs.values()):
        for c in list(peers):
            if c.task and not c.task.done():
                c.task.cancel()
    _subs.clear()
