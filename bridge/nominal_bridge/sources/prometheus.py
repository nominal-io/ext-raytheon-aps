"""Scrape Prometheus text endpoints and forward the samples as channels."""
from __future__ import annotations

import asyncio
import math
import re

from ..core import Source, register_source

_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([0-9eE+.\-naifNI]+)")
_LABEL = re.compile(r'(\w+)="([^"]*)"')


def parse(text: str, prefixes: tuple[str, ...], max_labels: int, skip_buckets: bool = True):
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        m = _LINE.match(line)
        if not m:
            continue
        name, labels, val = m.groups()
        if (prefixes and not name.startswith(prefixes)) or (skip_buckets and name.endswith("_bucket")):
            continue
        try:
            v = float(val)
        except ValueError:
            continue
        if math.isnan(v) or math.isinf(v):
            continue
        yield name, v, dict(_LABEL.findall(labels or "")[:max_labels])


@register_source("prometheus")
class PrometheusSource(Source):
    DEFAULTS = {
        "stream": "obs",
        "endpoints": {},                 # name: url
        "interval_s": 5.0,
        "prefixes": [],                  # keep only metrics starting with these; [] = all
        "channel_prefix": "obs",
        "endpoint_tag": "service",
        "max_series_per_endpoint": 300,
        "max_labels": 3,
        "timeout_s": 3.0,
    }

    @classmethod
    def validate(cls, cfg):
        problems = super().validate(cfg)
        if not cfg.get("endpoints"):
            problems.append("`endpoints` is empty (expected name: url pairs)")
        return problems

    def _scrape(self, url: str) -> list:
        import requests
        r = requests.get(url, timeout=float(self.cfg["timeout_s"]))
        r.raise_for_status()
        return list(parse(r.text, tuple(self.cfg["prefixes"] or ()), int(self.cfg["max_labels"])))

    async def run(self) -> None:
        p, tag = self.cfg["channel_prefix"], self.cfg["endpoint_tag"]
        cap = int(self.cfg["max_series_per_endpoint"])
        down: set[str] = set()
        while True:
            for name, url in self.cfg["endpoints"].items():
                try:
                    rows = await asyncio.to_thread(self._scrape, url)
                except Exception as e:
                    rows = []
                    if name not in down:
                        down.add(name)
                        self.ctx.log(f"{name}: scrape failed ({type(e).__name__}) {url}")
                else:
                    down.discard(name)
                self.ctx.emit(f"{p}.scrape_up", 1.0 if rows else 0.0, tags={tag: name})
                for metric, v, labels in rows[:cap]:
                    self.ctx.emit(f"{p}.{metric}", v, tags={tag: name, **labels})
            await asyncio.sleep(float(self.cfg["interval_s"]))
