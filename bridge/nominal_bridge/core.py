"""Core abstractions: Point/Event, Source and Sink base classes, registries,
the Hub that fans points out to sinks, and the per-source Context."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .util import log, now_ns

Value = float | int | str


@dataclass(slots=True)
class Point:
    stream: str            # logical stream name, mapped to a dataset per target
    channel: str
    ts_ns: int
    value: Value
    tags: dict[str, str]


@dataclass(slots=True)
class Event:
    name: str
    type: str              # INFO | FLAG | ERROR | SUCCESS
    ts_ns: int
    source: str
    description: str | None = None
    properties: dict[str, str] = field(default_factory=dict)


# ── registries ───────────────────────────────────────────────────────────────
SOURCE_TYPES: dict[str, type["Source"]] = {}
SINK_TYPES: dict[str, type["Sink"]] = {}


def register_source(type_name: str) -> Callable[[type], type]:
    """Class decorator: make a Source subclass available as `type: <type_name>`."""
    def deco(cls):
        cls.type_name = type_name
        SOURCE_TYPES[type_name] = cls
        return cls
    return deco


def register_sink(type_name: str) -> Callable[[type], type]:
    def deco(cls):
        cls.type_name = type_name
        SINK_TYPES[type_name] = cls
        return cls
    return deco


def load_plugins(paths: list[str]) -> list[str]:
    """Import every *.py in the given directories (or single files). Plugins register
    themselves with @register_source / @uci_handler at import time."""
    loaded = []
    for raw in paths:
        p = Path(raw)
        files = sorted(p.glob("*.py")) if p.is_dir() else ([p] if p.is_file() else [])
        for f in files:
            if f.name.startswith("_"):
                continue
            mod_name = f"nominal_bridge_plugin_{f.stem}"
            if getattr(sys.modules.get(mod_name), "__file__", None) == str(f):
                continue                      # already loaded
            spec = importlib.util.spec_from_file_location(mod_name, f)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            loaded.append(str(f))
    return loaded


# ── sinks ────────────────────────────────────────────────────────────────────
class Sink:
    """Receives every point and event. `write` must not block."""
    type_name = "?"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    async def start(self, streams: set[str]) -> None: ...
    def write(self, point: Point) -> None: ...
    async def event(self, event: Event) -> None: ...
    async def close(self) -> None: ...


@register_sink("memory")
class MemorySink(Sink):
    """Keeps everything in lists. Used by the test-suite and handy in a REPL."""

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg or {})
        self.points: list[Point] = []
        self.events: list[Event] = []

    def write(self, point: Point) -> None:
        self.points.append(point)

    async def event(self, event: Event) -> None:
        self.events.append(event)

    def channels(self) -> set[str]:
        return {p.channel for p in self.points}

    def values(self, channel: str, **tags) -> list[Value]:
        return [p.value for p in self.points if p.channel == channel
                and all(p.tags.get(k) == v for k, v in tags.items())]


# ── hub ──────────────────────────────────────────────────────────────────────
class EventGate:
    """Stops a chatty source from flooding the event timeline.

    * the same `key` is allowed at most once per `min_interval_s`
    * at most `max_per_minute` events overall; the rest are counted and dropped
    """

    def __init__(self, max_per_minute: int = 30, min_interval_s: float = 5.0):
        self.max_per_minute = max_per_minute
        self.min_interval_s = min_interval_s
        self._last: dict[str, float] = {}
        self._window: deque[float] = deque()
        self.dropped = 0

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        while self._window and now - self._window[0] > 60.0:
            self._window.popleft()
        last = self._last.get(key)
        if (last is not None and now - last < self.min_interval_s) or \
                len(self._window) >= self.max_per_minute:
            self.dropped += 1
            return False
        self._last[key] = now
        self._window.append(now)
        return True


class Hub:
    """Fans points and events out to every sink and keeps per-source counters."""

    def __init__(self, sinks: list[Sink], events_cfg: dict | None = None,
                 capture_dir: str | Path = "/capture"):
        ev = events_cfg or {}
        self.sinks = sinks
        self.enabled_events = ev.get("enabled", True)
        self.gate = EventGate(ev.get("max_per_minute", 30), ev.get("min_interval_s", 5.0))
        self.capture_dir = Path(capture_dir)
        self.points_by_source: Counter[str] = Counter()
        self.streams: set[str] = set()
        self._presence: dict[tuple[str, str], dict] = {}
        self._tasks: set[asyncio.Task] = set()

    def write(self, source: str, point: Point) -> None:
        self.points_by_source[source] += 1
        for s in self.sinks:
            s.write(point)

    def event(self, event: Event, key: str | None = None) -> bool:
        if not self.enabled_events or not self.gate.allow(key or f"{event.source}:{event.name}"):
            return False
        log(f"EVENT [{event.source}] {event.name}")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return True
        for s in self.sinks:
            t = loop.create_task(s.event(event))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        return True

    # presence: "X acquired" on first activity, "X lost" after timeout_s of silence
    def presence(self, source: str, key: str, label: str, timeout_s: float,
                 ts_ns: int | None = None, properties: dict | None = None) -> None:
        st = self._presence.setdefault((source, key), {"active": False})
        st.update(label=label, timeout_s=timeout_s, last=time.monotonic(),
                  properties=properties or {})
        if not st["active"]:
            st["active"] = True
            self.event(Event(f"{label} acquired", "INFO", ts_ns or now_ns(), source,
                             properties=dict(st["properties"])), key=f"{source}:{key}:on")

    def check_presence(self) -> None:
        now = time.monotonic()
        for (source, key), st in self._presence.items():
            if st["active"] and now - st["last"] > st["timeout_s"]:
                st["active"] = False
                self.event(Event(f"{st['label']} lost", "INFO", now_ns(), source,
                                 description=f"No activity for {st['timeout_s']:g} s",
                                 properties=dict(st["properties"])), key=f"{source}:{key}:off")

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


class Context:
    """What a source uses to talk to the rest of the bridge."""

    def __init__(self, hub: Hub, name: str, stream: str, tags: dict[str, str] | None = None):
        self.hub, self.name, self.stream = hub, name, stream
        self.tags = dict(tags or {})
        hub.streams.add(stream)

    @property
    def capture_dir(self) -> Path:
        return self.hub.capture_dir

    def emit(self, channel: str, value: Value, *, ts_ns: int | None = None,
             tags: dict[str, str] | None = None, stream: str | None = None) -> None:
        if isinstance(value, bool):
            value = 1.0 if value else 0.0
        elif isinstance(value, int):
            value = float(value)
        merged = {**self.tags, **tags} if tags else self.tags
        self.hub.write(self.name, Point(stream or self.stream, channel,
                                        ts_ns or now_ns(), value, merged))

    def event(self, name: str, *, type: str = "INFO", description: str | None = None,
              properties: dict[str, Any] | None = None, key: str | None = None,
              ts_ns: int | None = None) -> bool:
        props = {k: str(v) for k, v in (properties or {}).items()}
        return self.hub.event(Event(name, type, ts_ns or now_ns(), self.name, description, props),
                              key=key)

    def presence(self, key: str, label: str, timeout_s: float = 5.0, *,
                 ts_ns: int | None = None, properties: dict | None = None) -> None:
        self.hub.presence(self.name, key, label, timeout_s, ts_ns, properties)

    def log(self, *parts) -> None:
        log(f"[{self.name}]", *parts)


# ── sources ──────────────────────────────────────────────────────────────────
class Source:
    """Base class for everything that reads data.

    Subclass, decorate with @register_source("my_type"), and implement `run`.
    `run` should loop forever for a live feed (the runner restarts it with backoff if
    it raises) or return when a finite input such as a replay file is exhausted.

    DEFAULTS are merged under the user's config; REQUIRED keys are checked at startup.
    """
    type_name = "?"
    DEFAULTS: dict[str, Any] = {}
    REQUIRED: tuple[str, ...] = ()

    def __init__(self, name: str, cfg: dict, ctx: Context):
        self.name, self.ctx = name, ctx
        self.cfg = {**self.merged_defaults(), **cfg}

    @classmethod
    def merged_defaults(cls) -> dict:
        out: dict = {}
        for klass in reversed(cls.__mro__):
            out.update(getattr(klass, "DEFAULTS", {}))
        return out

    @classmethod
    def validate(cls, cfg: dict) -> list[str]:
        """Return a list of human-readable problems with this source's config."""
        return [f"missing required key '{k}'" for k in cls.REQUIRED if cfg.get(k) in (None, "")]

    async def run(self) -> None:
        raise NotImplementedError
