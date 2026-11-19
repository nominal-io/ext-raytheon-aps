"""UCI over OWP (the Sleet / LA-CAL WebSocket), or a recorded UCI JSONL file.

Subscribe-only. The bridge never publishes or commands.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from pathlib import Path

from ..core import Source, register_source
from ..util import RotatingLog, iso_to_ns, now_ns
from .uci_handlers import UCI_HANDLERS, UciMessage, generic, header_tags

HELLO_WORLD_TOPICS = [
    "ESM_Activity", "NavigationReport", "ObservationMeasurementReport",
    "OperatorNotification", "PO_Activity", "PositionReport", "PositionReportDetailed",
    "RoutePlan", "SignalReport", "SubsystemStatus", "SystemStatus", "ServiceStatus",
]


def default_topic(message: str, prefix: str = "mission.") -> str:
    """ESM_Activity -> mission.esm-activity, PositionReport -> mission.position-report"""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", message.replace("_", "-"))
    return prefix + s.lower()


def parse_topics(items, prefix: str = "mission.") -> list[tuple[str, str]]:
    out = []
    for it in items or []:
        if isinstance(it, str):
            out.append((it, default_topic(it, prefix)))
        elif isinstance(it, dict) and it.get("message"):
            out.append((it["message"], it.get("topic") or default_topic(it["message"], prefix)))
        elif isinstance(it, (list, tuple)) and len(it) == 2:
            out.append((str(it[0]), str(it[1])))
        else:
            raise ValueError(f"bad topic entry {it!r}: use 'Message' or "
                             "{message: Message, topic: some.topic}")
    return out


@register_source("uci_owp")
class UciOwpSource(Source):
    DEFAULTS = {
        "stream": "uci",
        "url": "ws://127.0.0.1:21402/owp",
        "service_id": "nominal-bridge",      # must be authorised in Sleet's services.d
        "schema": "002.5.0",
        "owp_versions": ["1.0"],
        "topics": HELLO_WORLD_TOPICS,
        "topic_prefix": "mission.",
        "handlers": {},                      # Message: generic | off   (override typed handlers)
        "generic": {},                       # max_fields, max_list_items, keep_ids
        "presence": {},                      # Message: {label, timeout_s}
        "events_for": [],                    # messages that raise one (throttled) event each
        "timestamps": "header",              # header | receive
        "record": True,                      # raw JSONL to <capture_dir>/uci_raw
        "save_samples": True,                # first message of each type to <capture_dir>/samples
        "replay": None,                      # path to a recorded JSONL file instead of a socket
        "replay_speed": 0,                   # 0 = as fast as possible, 1 = real time
        "replay_timestamps": "original",     # original | now (shift the recording to start now)
        "max_message_bytes": 16_000_000,
    }

    @classmethod
    def validate(cls, cfg: dict) -> list[str]:
        problems = super().validate(cfg)
        try:
            parse_topics(cfg.get("topics", HELLO_WORLD_TOPICS))
        except ValueError as e:
            problems.append(str(e))
        if cfg.get("timestamps", "header") not in ("header", "receive"):
            problems.append("timestamps must be 'header' or 'receive'")
        if cfg.get("replay") and not Path(cfg["replay"]).is_file():
            problems.append(f"replay file not found: {cfg['replay']}")
        return problems

    def __init__(self, name, cfg, ctx):
        super().__init__(name, cfg, ctx)
        self.topics = parse_topics(self.cfg["topics"], self.cfg["topic_prefix"])
        self.counts: Counter[str] = Counter()
        self.state: dict = {}
        self._samples: set[str] = set()
        self._log = None
        self._last_counts = 0.0
        self._shift_ns = 0

    # one decoded envelope: {"<MessageName>": {...}}
    def handle(self, name: str, body: dict, raw: str, rx_ns: int) -> None:
        self.counts[name] += 1
        ts = rx_ns
        if self.cfg["timestamps"] == "header":
            hdr = iso_to_ns((body.get("MessageHeader") or {}).get("Timestamp"))
            ts = hdr + self._shift_ns if hdr else rx_ns
        msg = UciMessage(name, body, ts, header_tags(body), raw, self.state, self.cfg)

        choice = (self.cfg["handlers"] or {}).get(name)
        if choice in ("off", False):
            return
        handler = generic if choice == "generic" else UCI_HANDLERS.get(name, generic)
        try:
            handler(msg, self.ctx)
        except Exception as e:  # one malformed message must not stop the feed
            self.counts["_handler_errors"] += 1
            if self.counts["_handler_errors"] <= 10:
                self.ctx.log(f"handler error on {name}: {e!r}")
        if name in (self.cfg["events_for"] or []):
            self.ctx.event(f"UCI {name}", description=raw[:900],
                           properties={"message": name}, ts_ns=ts, key=f"uci:{name}")

    def _emit_counts(self, ts_ns: int | None = None) -> None:
        for mname, n in self.counts.items():
            if not mname.startswith("_"):
                self.ctx.emit(f"uci.msg_total.{mname}", float(n), ts_ns=ts_ns)
        self.ctx.emit("uci.handler_errors_total", float(self.counts["_handler_errors"]),
                      ts_ns=ts_ns)

    def _record(self, name: str, body: dict, raw: str, rx_ns: int) -> None:
        if self.cfg["record"]:
            if self._log is None:
                self._log = RotatingLog(self.ctx.capture_dir / "uci_raw", "uci",
                                        max_bytes=100 << 20, keep=4)
            self._log.write((json.dumps({"t_rx_ns": rx_ns, "message": name, "payload": body},
                                        separators=(",", ":")) + "\n").encode())
        if self.cfg["save_samples"] and name not in self._samples:
            self._samples.add(name)
            d = self.ctx.capture_dir / "samples"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{name}.json").write_text(raw)

    async def run(self) -> None:
        if self.cfg["replay"]:
            await self._replay(Path(self.cfg["replay"]))
        else:
            await self._live()

    async def _live(self) -> None:
        import websockets
        async with websockets.connect(self.cfg["url"], subprotocols=["owp"],
                                      max_size=int(self.cfg["max_message_bytes"])) as ws:
            await ws.send("INIT " + json.dumps({
                "versions": self.cfg["owp_versions"], "schema": self.cfg["schema"],
                "service_id": self.cfg["service_id"], "verbose": False},
                separators=(",", ":")))
            info = await ws.recv()
            self.ctx.log(f"connected to {self.cfg['url']}: {str(info)[:100]}")
            for i, (message, topic) in enumerate(self.topics):
                await ws.send(f"SUB s{i} {message} {topic}")
            self.ctx.log(f"subscribed to {len(self.topics)} topic(s)")
            while True:
                frame = await ws.recv()
                if not isinstance(frame, str):
                    continue
                if frame.startswith("-ERR") or frame.startswith("ERR"):
                    self.ctx.log("OWP error:", frame[:200])
                    continue
                if not frame.startswith("MSG "):
                    continue
                rx = now_ns()
                try:
                    _, _sid, payload = frame.split(" ", 2)
                    doc = json.loads(payload)
                    name = next(iter(doc))
                    body = doc[name]
                except Exception as e:
                    self.ctx.log(f"unparseable frame: {e!r}")
                    continue
                self._record(name, body, payload, rx)
                self.handle(name, body, payload, rx)
                if time.monotonic() - self._last_counts > 5.0:
                    self._last_counts = time.monotonic()
                    self._emit_counts()

    async def _replay(self, path: Path) -> None:
        speed = float(self.cfg["replay_speed"] or 0)
        first_rx = wall0 = None
        n = rx = 0
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if "message" in rec:                       # bridge capture format
                    name, body = rec["message"], rec["payload"]
                    rx = rec.get("t_rx_ns") or iso_to_ns(rec.get("t_rx")) or now_ns()
                else:                                      # bare {"<Message>": {...}} lines
                    name = next(iter(rec))
                    body, rx = rec[name], now_ns()
                if first_rx is None:
                    first_rx, wall0 = rx, time.monotonic()
                    if self.cfg["replay_timestamps"] == "now":
                        self._shift_ns = now_ns() - rx
                rx += self._shift_ns
                if speed > 0:
                    delay = (rx - self._shift_ns - first_rx) / 1e9 / speed - (time.monotonic() - wall0)
                    if delay > 0:
                        await asyncio.sleep(delay)
                self.handle(name, body, json.dumps(body, separators=(",", ":")), rx)
                n += 1
                if n % 500 == 0:
                    await asyncio.sleep(0)
        self._emit_counts(rx if n else None)
        self.ctx.log(f"replayed {n} messages from {path.name}")
