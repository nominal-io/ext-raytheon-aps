"""Generic JSON telemetry: one JSON object per UDP datagram, per TCP line, or per line
of a file. Every numeric / string field becomes a channel. This is the quickest way to
bring in a custom sensor, adapter, or skill that can print JSON."""
from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

from ..core import Source, register_source
from ..flatten import flatten
from ..util import iso_to_ns, now_ns


def dig(doc, path: str):
    for part in path.split("."):
        if not isinstance(doc, dict) or part not in doc:
            return None
        doc = doc[part]
    return doc


def to_ns(value) -> int | None:
    """Accept ISO-8601 text or epoch seconds / milliseconds / microseconds / nanoseconds."""
    if isinstance(value, str):
        return iso_to_ns(value)
    if isinstance(value, (int, float)) and value > 0:
        for limit, mult in ((1e11, 1e9), (1e14, 1e6), (1e17, 1e3)):
            if value < limit:
                return int(value * mult)
        return int(value)
    return None


@register_source("json_lines")
class JsonLinesSource(Source):
    DEFAULTS = {
        "stream": "sensor",
        "transport": "udp",              # udp | tcp | file
        "bind": "0.0.0.0",               # udp
        "host": "127.0.0.1",             # tcp
        "port": None,
        "path": None,                    # file
        "channel_prefix": "custom",
        "timestamp_field": None,         # dotted path; receive time when absent
        "tag_fields": {},                # tag name: dotted path in the document
        "drop_fields": [],               # top-level keys to ignore
        "max_fields": 200,
    }

    @classmethod
    def validate(cls, cfg):
        problems = super().validate(cfg)
        tr = cfg.get("transport", "udp")
        if tr not in ("udp", "tcp", "file"):
            problems.append("transport must be udp, tcp or file")
        elif tr == "file" and not cfg.get("path"):
            problems.append("transport 'file' needs `path`")
        elif tr != "file" and not cfg.get("port"):
            problems.append("missing required key 'port'")
        return problems

    def handle(self, text: str | bytes) -> None:
        try:
            doc = json.loads(text)
        except ValueError:
            return
        if not isinstance(doc, dict):
            return
        ts = None
        if self.cfg["timestamp_field"]:
            ts = to_ns(dig(doc, self.cfg["timestamp_field"]))
        ts = ts or now_ns()
        tags = {}
        for tag, path in (self.cfg["tag_fields"] or {}).items():
            v = dig(doc, path)
            if v is not None:
                tags[tag] = str(v)
        skip = frozenset(self.cfg["drop_fields"] or [])
        tag_paths = set((self.cfg["tag_fields"] or {}).values()) | {self.cfg["timestamp_field"]}
        prefix = self.cfg["channel_prefix"]
        for channel, value, row_tags in flatten(doc, prefix, skip_keys=skip, keep_ids=True,
                                                max_fields=int(self.cfg["max_fields"])):
            if channel[len(prefix) + 1:] in tag_paths:
                continue
            self.ctx.emit(channel, value, ts_ns=ts, tags={**tags, **row_tags})

    async def run(self) -> None:
        tr = self.cfg["transport"]
        if tr == "file":
            for i, line in enumerate(Path(self.cfg["path"]).read_text().splitlines()):
                self.handle(line)
                if i % 500 == 0:
                    await asyncio.sleep(0)
        elif tr == "udp":
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.cfg["bind"], int(self.cfg["port"])))
            sock.setblocking(False)
            self.ctx.log(f"listening on udp/{self.cfg['port']}")
            loop = asyncio.get_running_loop()
            try:
                while True:
                    self.handle(await loop.sock_recv(sock, 65535))
            finally:
                sock.close()
        else:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.cfg["host"], int(self.cfg["port"])), 5)
            self.ctx.log(f"connected to {self.cfg['host']}:{self.cfg['port']}")
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        raise ConnectionError("feed closed by peer")
                    self.handle(line)
            finally:
                writer.close()
