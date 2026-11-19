"""Builds sinks and sources from config and supervises them."""
from __future__ import annotations

import asyncio
import signal
import traceback

from . import sinks as _sinks  # noqa: F401  (register built-ins)
from . import sources as _sources  # noqa: F401
from .core import SINK_TYPES, SOURCE_TYPES, Context, Hub, Sink, Source, load_plugins
from .util import log, now_ns


def build(cfg: dict, *, dry_run: bool = False, only: set[str] | None = None,
          sinks: list[Sink] | None = None) -> tuple[Hub, list[Source]]:
    loaded = load_plugins(cfg.get("plugins") or [])
    if loaded:
        log("plugins loaded:", ", ".join(loaded))
    if sinks is None:
        specs = [{"type": "console"}] if dry_run else (cfg.get("sinks") or [])
        sinks = [SINK_TYPES[s["type"]](s) for s in specs]
    hub = Hub(sinks, cfg.get("events"), cfg.get("capture_dir", "/capture"))
    sources: list[Source] = []
    for s in cfg.get("sources") or []:
        if s.get("enabled", True) is False or (only and s["name"] not in only):
            continue
        cls = SOURCE_TYPES[s["type"]]
        merged = {**cls.merged_defaults(), **s}
        ctx = Context(hub, s["name"], merged["stream"], merged.get("tags"))
        sources.append(cls(s["name"], s, ctx))
    return hub, sources


async def _supervise(src: Source, state: dict) -> None:
    backoff = 2.0
    while True:
        state[src.name] = 1.0
        try:
            await src.run()
            src.ctx.log("finished")
            state[src.name] = 0.0
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            state[src.name] = 0.0
            where = traceback.extract_tb(e.__traceback__)[-1]
            src.ctx.log(f"error: {e!r} at {where.filename.split('/')[-1]}:{where.lineno}; "
                        f"restarting in {backoff:.0f}s")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


async def _housekeeping(hub: Hub, state: dict, health: dict) -> None:
    stream = health.get("stream")
    period = float(health.get("interval_s", 10))
    tick = 0
    while True:
        await asyncio.sleep(1.0)
        hub.check_presence()
        tick += 1
        if stream and tick % max(int(period), 1) == 0:
            ts = now_ns()
            from .core import Point
            for name, up in state.items():
                tags = {"source": name}
                hub.write("bridge", Point(stream, "bridge.source_up", ts, up, tags))
                hub.write("bridge", Point(stream, "bridge.points_total", ts,
                                          float(hub.points_by_source[name]), tags))
            hub.write("bridge", Point(stream, "bridge.events_dropped_total", ts,
                                      float(hub.gate.dropped), {}))


async def run(cfg: dict, *, dry_run: bool = False, only: set[str] | None = None,
              sinks: list[Sink] | None = None, duration_s: float | None = None) -> Hub:
    hub, sources = build(cfg, dry_run=dry_run, only=only, sinks=sinks)
    health = cfg.get("health") or {}
    if health.get("stream"):
        hub.streams.add(health["stream"])
    for s in hub.sinks:
        await s.start(hub.streams)
    log(f"bridge up: {len(sources)} source(s) [{', '.join(s.name for s in sources)}], "
        f"sinks [{', '.join(s.type_name for s in hub.sinks)}]")

    state: dict[str, float] = {}
    tasks = [asyncio.create_task(_supervise(s, state), name=s.name) for s in sources]
    house = asyncio.create_task(_housekeeping(hub, state, health))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    async def _all_done():
        await asyncio.gather(*tasks, return_exceptions=True)

    waiters = [asyncio.create_task(stop.wait()), asyncio.create_task(_all_done())]
    try:
        await asyncio.wait(waiters, timeout=duration_s, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks + [house] + waiters:
            t.cancel()
        await asyncio.gather(*tasks, house, *waiters, return_exceptions=True)
        await hub.drain()
        for s in hub.sinks:
            await s.close()
        log("bridge stopped")
    return hub
