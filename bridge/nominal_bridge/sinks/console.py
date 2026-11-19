"""Console sink: no network. Prints the first few points of every channel and a
periodic summary. This is what `--dry-run` uses."""
from __future__ import annotations

import time
from collections import Counter

from ..core import Event, Point, Sink, register_sink
from ..util import log


@register_sink("console")
class ConsoleSink(Sink):
    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg or {})
        self.first_n = int(self.cfg.get("first_n", 1))
        self.summary_s = float(self.cfg.get("summary_s", 10))
        self.counts: Counter[tuple[str, str]] = Counter()
        self.series: set[tuple] = set()
        self._last = time.monotonic()
        self._since = 0

    def write(self, p: Point) -> None:
        key = (p.stream, p.channel)
        self.counts[key] += 1
        self.series.add((p.stream, p.channel, tuple(sorted(p.tags.items()))))
        self._since += 1
        if self.counts[key] <= self.first_n:
            tags = ",".join(f"{k}={v}" for k, v in sorted(p.tags.items()))
            val = f"{p.value:.6g}" if isinstance(p.value, float) else repr(p.value)
            log(f"  {p.stream:>8} | {p.channel} {{{tags}}} = {val}")
        now = time.monotonic()
        if now - self._last >= self.summary_s:
            log(f"-- {self._since / (now - self._last):.0f} points/s, "
                f"{len(self.counts)} channels, {len(self.series)} series so far")
            self._last, self._since = now, 0

    async def event(self, ev: Event) -> None:
        pass  # the hub already logs every event it lets through

    async def close(self) -> None:
        by_stream: Counter[str] = Counter()
        for (stream, _), n in self.counts.items():
            by_stream[stream] += n
        log(f"-- totals: {sum(self.counts.values())} points, {len(self.counts)} channels, "
            f"{len(self.series)} series; by stream {dict(by_stream)}")
