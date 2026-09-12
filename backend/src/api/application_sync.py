"""Application changes use the same bounded, replayable transport as Turns."""

import asyncio
import json
from uuid import uuid4

from backend.storage.runtime_event_stream import RuntimeEventCursorExpired

CHANNEL = "application-state"


class ApplicationSync:
    def __init__(self, stream) -> None:
        self.stream = stream
        self.epoch = uuid4().hex

    def publish(self, kind: str, **values) -> None:
        if self.stream.closed:
            return
        self.stream.publish(
            event_id=uuid4().hex,
            turn_id=CHANNEL,
            thread_id=CHANNEL,
            sequence=0,
            payload={"type": kind, **values},
        )

    async def events(self, cursor: str | None, request):
        latest = self.stream.latest_thread_id(CHANNEL)
        parts = (cursor or "").split(":")
        valid = len(parts) == 2 and parts[0] == self.epoch and parts[1].isdecimal() and int(parts[1]) <= int(latest)
        position = parts[1] if valid else latest
        if not valid:
            yield self.encode(position, {"type": "sync.reset", "reason": "restart" if cursor else "startup"})
        ready = False
        while not self.stream.closed and not await request.is_disconnected():
            try:
                entries = await asyncio.to_thread(self.stream.read_thread, CHANNEL, position, block_ms=1000)
            except RuntimeEventCursorExpired:
                position = self.stream.latest_thread_id(CHANNEL)
                ready = False
                yield self.encode(position, {"type": "sync.reset", "reason": "expired"})
                continue
            if not entries:
                yield self.encode(position, {"type": "sync.heartbeat"})
            for entry in entries:
                position = entry.stream_id
                yield self.encode(position, entry.payload)
            if not ready and position == self.stream.latest_thread_id(CHANNEL):
                ready = True
                yield self.encode(position, {"type": "sync.ready"})

    def encode(self, position: str, payload: dict) -> str:
        return f"id: {self.epoch}:{position}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
