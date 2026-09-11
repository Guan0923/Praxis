"""Byte-bounded head/tail output with absolute read positions."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

OUTPUT_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class OutputPart:
    start: int
    source: str
    data: bytes

    @property
    def end(self) -> int:
        return self.start + len(self.data)


class OutputBuffer:
    def __init__(self, capacity: int = OUTPUT_BYTES) -> None:
        self.capacity = capacity
        self.total = 0
        self._head: list[OutputPart] = []
        self._tail: list[OutputPart] = []
        self._lock = RLock()

    def append(self, data: bytes, source: str = "stdout") -> None:
        if not data:
            return
        with self._lock:
            start = self.total
            self.total += len(data)
            split = max(0, min(len(data), self.capacity // 2 - start))
            while 0 < split < len(data) and data[split] & 0xC0 == 0x80:
                split -= 1
            if split:
                self._append_part(self._head, OutputPart(start, source, data[:split]))
            if split < len(data):
                self._append_part(self._tail, OutputPart(start + split, source, data[split:]))
            floor = max(0, self.total - self.capacity // 2)
            self._tail = [p for p in self._tail if p.end > floor]
            if self._tail and self._tail[0].start < floor:
                first = self._tail[0]
                offset = floor - first.start
                while offset < len(first.data) and first.data[offset] & 0xC0 == 0x80:
                    offset += 1
                self._tail[0] = OutputPart(first.start + offset, first.source, first.data[offset:])

    @staticmethod
    def _append_part(parts: list[OutputPart], part: OutputPart) -> None:
        if parts and parts[-1].source == part.source and parts[-1].end == part.start and len(parts[-1].data) < 16384:
            previous = parts[-1]
            parts[-1] = OutputPart(previous.start, previous.source, previous.data + part.data)
        else:
            parts.append(part)

    def read(self, after: int = 0) -> tuple[str, int]:
        text, position, _omitted = self.read_details(after)
        return text, position

    def snapshot_parts(self) -> tuple[tuple[OutputPart, ...], int]:
        with self._lock:
            return tuple(self._head + self._tail), self.total

    def read_details(self, after: int = 0) -> tuple[str, int, int]:
        parts, end = self.snapshot_parts()
        chunks: list[str] = []
        position = after
        omitted = 0
        source = "stdout"
        for part in parts:
            if part.end <= after:
                continue
            start = max(after, part.start)
            if start > position:
                omitted += start - position
                chunks.append(f"\n[... {start - position} bytes omitted ...]\n")
            data = part.data[start - part.start :]
            text = data.decode("utf-8", errors="replace")
            if part.source != source:
                if chunks and not chunks[-1].endswith("\n"):
                    chunks.append("\n")
                chunks.append(f"[{part.source}] ")
                source = part.source
            chunks.append(text)
            position = part.end
        if position < end:
            omitted += end - position
            chunks.append(f"\n[... {end - position} bytes omitted ...]\n")
        return "".join(chunks), end, omitted

    @property
    def retained_bytes(self) -> int:
        with self._lock:
            return sum(len(p.data) for p in self._head + self._tail)

    def clear(self) -> None:
        with self._lock:
            self._head.clear()
            self._tail.clear()
            self.total = 0
