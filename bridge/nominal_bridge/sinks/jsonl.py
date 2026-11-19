"""JSONL sink: one JSON object per point/event, in rotating files. Use it to record on
a network with no route to Nominal and replay or upload the files later."""
from __future__ import annotations

import json
from pathlib import Path

from ..core import Event, Point, Sink, register_sink
from ..util import RotatingLog


@register_sink("jsonl")
class JsonlSink(Sink):
    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self._log = RotatingLog(Path(cfg.get("dir", "/capture/points")), "points",
                                max_bytes=int(cfg.get("max_mb", 256)) << 20,
                                keep=int(cfg.get("keep", 20)), suffix=".jsonl")

    def write(self, p: Point) -> None:
        self._log.write((json.dumps({"s": p.stream, "c": p.channel, "t": p.ts_ns,
                                     "v": p.value, "g": p.tags},
                                    separators=(",", ":")) + "\n").encode())

    async def event(self, ev: Event) -> None:
        self._log.write((json.dumps({"event": ev.name, "type": ev.type, "t": ev.ts_ns,
                                     "source": ev.source, "description": ev.description,
                                     "properties": ev.properties},
                                    separators=(",", ":")) + "\n").encode())

    async def close(self) -> None:
        self._log.close()
