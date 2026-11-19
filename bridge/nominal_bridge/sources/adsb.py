"""ADS-B from a BaseStation (SBS-1) TCP feed, the CSV format dump1090 / readsb serve
on port 30003. One set of channels per aircraft, told apart by the `icao` tag."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from ..core import Source, register_source
from ..util import now_ns

FT, KT, FPM = 0.3048, 0.514444, 0.00508

# SBS field index -> (channel, scale)
_NUMERIC = {
    11: (("altitude_ft", 1.0), ("altitude_m", FT)),
    12: (("ground_speed_kt", 1.0), ("ground_speed_mps", KT)),
    13: (("track_deg", 1.0),),
    14: (("latitude_deg", 1.0),),
    15: (("longitude_deg", 1.0),),
    16: (("vertical_rate_fpm", 1.0), ("vertical_rate_mps", FPM)),
}


@register_source("adsb_sbs")
class AdsbSbsSource(Source):
    DEFAULTS = {
        "stream": "adsb",
        "host": "127.0.0.1",
        "port": 30003,
        "channel_prefix": "adsb",
        "aircraft_tag": "icao",
        "max_aircraft": 300,
        "stale_after_s": 60.0,
        "aircraft_events": False,        # "<callsign> acquired / lost" per aircraft
        "replay": None,                  # text file of SBS lines
    }

    def __init__(self, name, cfg, ctx):
        super().__init__(name, cfg, ctx)
        self.seen: dict[str, float] = {}
        self.callsign: dict[str, str] = {}
        self.n = self.ignored = 0
        self._last_counts = 0.0

    def handle(self, line: str, ts_ns: int) -> None:
        f = line.strip().split(",")
        if len(f) < 11 or f[0] != "MSG":
            return
        icao = f[4].strip().upper()
        if not icao:
            return
        mono = time.monotonic()
        if icao not in self.seen and len(self.seen) >= int(self.cfg["max_aircraft"]):
            self.ignored += 1
            return
        self.seen[icao] = mono
        self.n += 1
        p = self.cfg["channel_prefix"]
        tags = {self.cfg["aircraft_tag"]: icao}
        cs = f[10].strip() if len(f) > 10 else ""
        if cs and self.callsign.get(icao) != cs:
            self.callsign[icao] = cs
            self.ctx.emit(f"{p}.callsign", cs, ts_ns=ts_ns, tags=tags)
        for idx, outs in _NUMERIC.items():
            if len(f) > idx and f[idx].strip():
                try:
                    v = float(f[idx])
                except ValueError:
                    continue
                for ch, k in outs:
                    self.ctx.emit(f"{p}.{ch}", v * k, ts_ns=ts_ns, tags=tags)
        if len(f) > 17 and f[17].strip():
            self.ctx.emit(f"{p}.squawk", f[17].strip(), ts_ns=ts_ns, tags=tags)
        if len(f) > 21 and f[21].strip() in ("0", "-1", "1"):
            self.ctx.emit(f"{p}.on_ground", f[21].strip() != "0", ts_ns=ts_ns, tags=tags)
        if self.cfg["aircraft_events"]:
            self.ctx.presence(icao, f"ADS-B {self.callsign.get(icao, icao)}",
                              float(self.cfg["stale_after_s"]), ts_ns=ts_ns,
                              properties={"icao": icao})
        if mono - self._last_counts >= 2.0:
            self._counts(ts_ns, mono)

    def _counts(self, ts_ns: int, mono: float) -> None:
        self._last_counts = mono
        p = self.cfg["channel_prefix"]
        stale = float(self.cfg["stale_after_s"])
        self.ctx.emit(f"{p}.aircraft_active",
                      float(sum(1 for t in self.seen.values() if mono - t < stale)), ts_ns=ts_ns)
        self.ctx.emit(f"{p}.msg_total", float(self.n), ts_ns=ts_ns)
        self.ctx.emit(f"{p}.aircraft_ignored_total", float(self.ignored), ts_ns=ts_ns)

    async def run(self) -> None:
        if self.cfg["replay"]:
            ts = now_ns()
            for line in Path(self.cfg["replay"]).read_text().splitlines():
                ts += 1_000_000
                self.handle(line, ts)
            self._counts(ts, time.monotonic())
            return
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.cfg["host"], int(self.cfg["port"])), 5)
        self.ctx.log(f"connected to {self.cfg['host']}:{self.cfg['port']}")
        try:
            while True:
                line = await reader.readline()
                if not line:
                    raise ConnectionError("SBS feed closed by peer")
                self.handle(line.decode("ascii", "ignore"), now_ns())
        finally:
            writer.close()
