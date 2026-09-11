"""Process-owned terminal replay bounded by UTF-8 bytes."""

from dataclasses import dataclass
from threading import RLock

from backend.jobs.output_buffer import OutputBuffer

MAX_TERMINAL_CHUNK_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class TerminalOutputChunk:
    sequence: int
    data: str
    segments: tuple[dict[str, object], ...] = ()


class MemoryTerminalOutputStream:
    def __init__(self) -> None:
        self._buffers: dict[str, OutputBuffer] = {}
        self._lock = RLock()

    def append(self, terminal_id: str, data: str) -> list[TerminalOutputChunk]:
        with self._lock:
            buffer = self._buffers.setdefault(terminal_id, OutputBuffer())
            chunks = []
            raw = data.encode("utf-8")
            offset = 0
            while offset < len(raw):
                end = min(len(raw), offset + MAX_TERMINAL_CHUNK_BYTES)
                while end < len(raw) and raw[end] & 0xC0 == 0x80:
                    end -= 1
                part = raw[offset:end]
                buffer.append(part)
                chunks.append(TerminalOutputChunk(buffer.total, part.decode("utf-8")))
                offset = end
            return chunks

    def after(self, terminal_id: str, sequence: int) -> list[TerminalOutputChunk]:
        with self._lock:
            buffer = self._buffers.get(terminal_id)
            if buffer is None or buffer.total <= sequence:
                return []
            parts, position = buffer.snapshot_parts()
            segments: list[dict[str, object]] = []
            cursor = sequence
            omitted = False
            for part in parts:
                if part.end <= sequence:
                    continue
                start = max(sequence, part.start)
                if start > cursor:
                    segments.append({"omitted_bytes": start - cursor})
                    omitted = True
                segments.append({"data": part.data[start - part.start :].decode("utf-8", errors="replace")})
                cursor = part.end
            if cursor < position:
                segments.append({"omitted_bytes": position - cursor})
                omitted = True
            text = "".join(
                str(part["data"]) if "data" in part else f"\n[... {part['omitted_bytes']} bytes omitted ...]\n"
                for part in segments
            )
            return [TerminalOutputChunk(position, text, tuple(segments) if omitted else ())]

    def delete(self, terminal_id: str) -> None:
        with self._lock:
            self._buffers.pop(terminal_id, None)

    def close(self) -> None:
        with self._lock:
            self._buffers.clear()
