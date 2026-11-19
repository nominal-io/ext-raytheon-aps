"""Nominal sink: streams points to datasets and creates events, via the public SDK.

One sink can fan out to several targets (tenants / workspaces). Each target maps the
bridge's logical stream names to dataset RIDs.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from ..core import Event, Point, Sink, register_sink
from ..util import log


@register_sink("nominal")
class NominalSink(Sink):
    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.targets = cfg.get("targets") or []
        self._clients: dict[str, object] = {}
        self._streams: dict[tuple[str, str], object] = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nominal-events")
        self._warned: set[tuple[str, str]] = set()
        self.errors = 0

    def _connect(self, streams: set[str]) -> None:
        from nominal.core.client import NominalClient
        for t in self.targets:
            name = t["name"]
            kwargs = {}
            if t.get("trust_store_path"):
                kwargs["trust_store_path"] = t["trust_store_path"]
            if t.get("profile"):
                client = NominalClient.from_profile(t["profile"], **kwargs)
            else:
                client = NominalClient.from_token(
                    t["token"], t.get("base_url") or "https://api.gov.nominal.io/api",
                    workspace_rid=t.get("workspace_rid") or None, **kwargs)
            self._clients[name] = client
            wait = timedelta(seconds=float(self.cfg.get("max_wait_s", 2.0)))
            for st in sorted(streams):
                rid = (t.get("datasets") or {}).get(st)
                if not rid:
                    continue
                ds = client.get_dataset(rid)
                self._streams[(name, st)] = ds.get_write_stream(
                    batch_size=int(self.cfg.get("batch_size", 20000)), max_wait=wait)
                log(f"nominal target '{name}': stream '{st}' -> {ds.name}")

    async def start(self, streams: set[str]) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self._connect, streams)

    def write(self, point: Point) -> None:
        for t in self.targets:
            key = (t["name"], point.stream)
            ws = self._streams.get(key)
            if ws is None:
                if key not in self._warned:
                    self._warned.add(key)
                    log(f"nominal target '{t['name']}': no dataset for stream "
                        f"'{point.stream}', dropping its points")
                continue
            try:
                ws.enqueue(point.channel, point.ts_ns, point.value, tags=point.tags)
            except Exception as e:  # never let one bad point stop a source
                self.errors += 1
                if self.errors <= 5:
                    log(f"nominal enqueue error on {point.channel}: {e!r}")

    def _create_event(self, ev: Event) -> None:
        from nominal.core.event import EventType
        et = getattr(EventType, ev.type, EventType.INFO)
        for t in self.targets:
            if not t.get("asset_rid"):
                continue
            try:
                self._clients[t["name"]].create_event(
                    ev.name, et, ev.ts_ns, description=ev.description,
                    assets=[t["asset_rid"]], properties=ev.properties,
                    labels=list(self.cfg.get("event_labels") or ["nominal-bridge"]))
            except Exception as e:
                log(f"nominal event error ({t['name']}) {ev.name!r}: {e!r}")

    async def event(self, ev: Event) -> None:
        await asyncio.get_running_loop().run_in_executor(self._pool, self._create_event, ev)

    async def close(self) -> None:
        def _close():
            for ws in self._streams.values():
                try:
                    ws.close()
                except Exception as e:
                    log(f"nominal stream close error: {e!r}")
        await asyncio.get_running_loop().run_in_executor(None, _close)
        self._pool.shutdown(wait=True)
