"""Raw complex baseband (IQ) samples over UDP.

Emits level / clipping / spectrum-peak channels, writes waterfall rows for
render_waterfall.py, and saves short raw IQ snapshots with a JSON sidecar.
Subclass IqSource and override `blocks()` for another transport.
"""
from __future__ import annotations

import asyncio
import json
import math
import socket
import struct
import time
from collections import deque
from pathlib import Path
from typing import AsyncIterator

import numpy as np

from ..core import Source, register_source
from ..util import RotatingLog, now_ns
from .squall import Tap

# name -> (numpy dtype, scale to +-1.0, offset removed first, bytes per complex sample)
SAMPLE_FORMATS = {
    "sc16le": ("<i2", 32768.0, 0.0, 4), "sc16be": (">i2", 32768.0, 0.0, 4),
    "sc8": ("i1", 128.0, 0.0, 2), "cu8": ("u1", 127.5, 127.5, 2),      # cu8 = RTL-SDR native
    "cf32le": ("<f4", 1.0, 0.0, 8),
}


def to_complex(data: bytes, fmt: str) -> np.ndarray:
    dtype, scale, offset, bps = SAMPLE_FORMATS[fmt]
    raw = np.frombuffer(data[:len(data) - len(data) % bps], dtype=dtype).astype(np.float32)
    if offset:
        raw = raw - offset
    if scale != 1.0:
        raw = raw / scale
    return (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)


class IqSource(Source):
    DEFAULTS = {
        "stream": "sensor",
        "sample_format": "sc16le",
        "sample_rate_hz": None,          # null = ask Couloir (when `couloir:` is set)
        "center_frequency_hz": None,
        "channel_prefix": "rf",
        "stats_interval_s": 0.5,
        "spectrum_interval_s": 2.0,
        "fft_size": 8192,
        "waterfall": {"enabled": True, "interval_s": 0.25, "fft_size": 4096, "bins": 512,
                      "dir": None},
        "snapshot": {"enabled": True, "every_s": 300, "duration_s": 2.0, "keep": 10,
                     "dir": None},
        "couloir": None,
        "couloir_refresh_s": 10,         # re-read sample rate / centre frequency (retunes)
    }

    @classmethod
    def validate(cls, cfg):
        problems = super().validate(cfg)
        if cfg.get("sample_format", "sc16le") not in SAMPLE_FORMATS:
            problems.append(f"sample_format must be one of {sorted(SAMPLE_FORMATS)}")
        if not cfg.get("couloir") and not cfg.get("sample_rate_hz"):
            problems.append("set sample_rate_hz (or a `couloir:` block to read it from "
                            "the sensor)")
        wf = {**IqSource.DEFAULTS["waterfall"], **(cfg.get("waterfall") or {})}
        if int(wf["fft_size"]) % int(wf["bins"]):
            problems.append("waterfall.fft_size must be a multiple of waterfall.bins")
        return problems

    def __init__(self, name, cfg, ctx):
        super().__init__(name, cfg, ctx)
        self.wf = {**IqSource.DEFAULTS["waterfall"], **(self.cfg.get("waterfall") or {})}
        self.snap = {**IqSource.DEFAULTS["snapshot"], **(self.cfg.get("snapshot") or {})}
        self.fs = self.cfg["sample_rate_hz"]
        self.fc = self.cfg["center_frequency_hz"]
        self.tap: Tap | None = None
        self._last_rx = 0.0
        self._acc: list[np.ndarray] = []
        self._tail = np.zeros(0, dtype=np.complex64)
        self._arrivals: deque[float] = deque(maxlen=256)
        self._t_stats = self._t_spec = self._t_wf = 0.0
        self._t_stats_start: float | None = None
        self._wf_log = None
        self._snap_fh = None
        self._snap_meta: dict = {}
        self._snap_until = 0.0
        self._last_snap: float | None = None
        self.n_blocks = 0
        self._arrived = 0

    @property
    def _ext(self) -> str:           # file extension for raw snapshots
        fmt = self.cfg["sample_format"]
        return "sc16" if fmt == "sc16le" else fmt

    async def blocks(self) -> AsyncIterator[tuple[int, bytes]]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def _refresh(self) -> None:
        st = await self.tap.status() if self.tap else None
        if st is None:
            return
        rf = st.rf_status
        if rf.sample_rate and rf.sample_rate != self.fs:
            self.ctx.log(f"sample rate {rf.sample_rate} sps from Couloir")
            self.fs = rf.sample_rate
        if rf.center_frequency_hz and rf.center_frequency_hz != self.fc:
            self.ctx.log(f"centre frequency {rf.center_frequency_hz} Hz from Couloir")
            self.fc = rf.center_frequency_hz

    async def setup(self) -> None:
        if self.cfg["couloir"]:
            self.tap = Tap(self.ctx, self.cfg["couloir"], int(self.cfg.get("port") or 0))
            await self.tap.register()
            if not self.cfg["sample_rate_hz"]:
                await self._refresh()
        if not self.fs:
            raise RuntimeError("sample rate unknown: set sample_rate_hz in the config")

    async def _refresher(self) -> None:
        while True:
            await asyncio.sleep(float(self.cfg["couloir_refresh_s"]))
            await self._refresh()

    async def run(self) -> None:
        refresher = watch = None
        try:
            await self.setup()
            if self.tap:
                watch = asyncio.create_task(self.tap.watch(lambda: self._last_rx))
            if self.tap and not self.cfg["sample_rate_hz"]:
                refresher = asyncio.create_task(self._refresher())
            async for ts_ns, data in self.blocks():
                self._last_rx = time.monotonic()
                self.process(ts_ns, data)
        finally:
            for task in (refresher, watch):
                if task:
                    task.cancel()
            self.close()
            if self.tap:
                await asyncio.to_thread(self.tap.release)

    def close(self) -> None:
        if self._wf_log:
            self._wf_log.close()
        if self._snap_fh:
            self._finish_snapshot()

    # ── per-block work ───────────────────────────────────────────────────────
    def process(self, ts_ns: int, data: bytes, mono: float | None = None) -> None:
        mono = time.monotonic() if mono is None else mono
        self.n_blocks += 1
        self._arrivals.append(mono)
        self._snapshot(ts_ns, data, mono)
        c = to_complex(data, self.cfg["sample_format"])
        if not len(c):
            return
        self._acc.append(c)
        if self._t_stats_start is None:      # the first block only starts the clock
            self._t_stats_start = self._t_stats = self._t_spec = mono
            self._arrived = 0
        else:
            self._arrived += len(c)
        n_wf = int(self.wf["fft_size"])
        self._tail = np.concatenate([self._tail, c])[-max(n_wf, 8192):]
        if self.wf["enabled"] and mono - self._t_wf >= float(self.wf["interval_s"]) \
                and len(self._tail) >= n_wf:
            self._t_wf = mono
            self._waterfall(ts_ns, n_wf)
        if mono - self._t_stats >= float(self.cfg["stats_interval_s"]):
            self._stats(ts_ns, mono)

    def _waterfall(self, ts_ns: int, n: int) -> None:
        if self._wf_log is None:
            d = self.ctx.capture_dir / (self.wf["dir"] or "waterfall")
            self._wf_log = RotatingLog(d, "wf", max_bytes=100 << 20, keep=2)
            (d / "meta.json").write_text(json.dumps({
                "record": "utc_ns (u64 BE) + <bins> float32 BE of dB power, fftshifted",
                "bins": int(self.wf["bins"]), "fft_size": n, "sample_rate_hz": self.fs,
                "center_frequency_hz": self.fc, "source": self.name}, indent=1))
        seg = self._tail[-n:] * np.hanning(n).astype(np.float32)
        spec = np.fft.fftshift(np.abs(np.fft.fft(seg)) ** 2)
        bins = int(self.wf["bins"])
        row = 10.0 * np.log10(spec.reshape(bins, n // bins).mean(axis=1) + 1e-12)
        self._wf_log.write(struct.pack(">Q", ts_ns) + row.astype(">f4").tobytes())

    def _stats(self, ts_ns: int, mono: float) -> None:
        p = self.cfg["channel_prefix"]
        e = lambda ch, v: self.ctx.emit(f"{p}.{ch}", float(v), ts_ns=ts_ns)  # noqa: E731
        c = np.concatenate(self._acc)
        self._acc = []
        elapsed = mono - self._t_stats
        self._t_stats = mono
        mag = np.abs(c)
        e("iq.rms_dbfs", 10 * math.log10(float(np.mean(mag ** 2)) + 1e-12))
        e("iq.peak_dbfs", 20 * math.log10(float(mag.max()) + 1e-9))
        e("iq.clip_fraction", float(np.mean((np.abs(c.real) > 0.999) | (np.abs(c.imag) > 0.999))))
        rate = 0.0
        if len(self._arrivals) >= 2 and self._arrivals[-1] > self._arrivals[0]:
            rate = (len(self._arrivals) - 1) / (self._arrivals[-1] - self._arrivals[0])
        e("iq.packet_rate_hz", rate)
        e("iq.samples_per_window", len(c))
        if elapsed > 0 and self.fs:
            # 1.0 = every sample arrived; lower means drops between sensor and bridge
            e("iq.delivery_ratio", self._arrived / elapsed / float(self.fs))
        self._arrived = 0
        n = int(self.cfg["fft_size"])
        if mono - self._t_spec >= float(self.cfg["spectrum_interval_s"]) and len(c) >= n // 2:
            self._t_spec = mono
            seg = c[:n]
            spec = np.fft.fftshift(np.abs(np.fft.fft(seg * np.hanning(len(seg)), n)) ** 2)
            freqs = np.fft.fftshift(np.fft.fftfreq(n, 1 / float(self.fs)))
            pk = int(np.argmax(spec))
            e("spectrum.peak_offset_hz", freqs[pk])
            e("spectrum.peak_power_db", 10 * np.log10(spec[pk] + 1e-12))
            if self.fc:
                e("spectrum.peak_frequency_hz", float(self.fc) + float(freqs[pk]))

    def _snapshot(self, ts_ns: int, data: bytes, mono: float) -> None:
        if not self.snap["enabled"]:
            return
        if self._last_snap is None:                 # first snapshot one minute in
            self._last_snap = mono - float(self.snap["every_s"]) + 60.0
        if self._snap_fh is None and mono - self._last_snap >= float(self.snap["every_s"]):
            self._last_snap = mono
            self._snap_until = mono + float(self.snap["duration_s"])
            d = self.ctx.capture_dir / (self.snap["dir"] or "iq_snapshots")
            d.mkdir(parents=True, exist_ok=True)
            self._snap_meta = {
                "start_utc_ns": ts_ns, "format": self.cfg["sample_format"] + "_interleaved_iq",
                "sample_rate_sps": self.fs, "center_frequency_hz": self.fc,
                "source": self.name, "bytes": 0, "_dir": str(d)}
            self._snap_fh = open(d / f"iq_{ts_ns}.{self._ext}", "wb")
        if self._snap_fh is not None:
            self._snap_fh.write(data)
            self._snap_meta["bytes"] += len(data)
            if mono >= self._snap_until:
                self._finish_snapshot()

    def _finish_snapshot(self) -> None:
        self._snap_fh.close()
        self._snap_fh = None
        m = self._snap_meta
        d = Path(m.pop("_dir"))
        m["samples"] = m["bytes"] // SAMPLE_FORMATS[self.cfg["sample_format"]][3]
        (d / f"iq_{m['start_utc_ns']}.json").write_text(json.dumps(m, indent=1))
        for old in sorted(d.glob(f"iq_*.{self._ext}"))[:-int(self.snap["keep"])]:
            old.unlink(missing_ok=True)
            old.with_suffix(".json").unlink(missing_ok=True)
        self.ctx.log(f"IQ snapshot saved ({m['bytes']} bytes)")


@register_source("udp_iq")
class UdpIqSource(IqSource):
    DEFAULTS = {
        "bind": "0.0.0.0",
        "port": None,
        "rcvbuf_bytes": 8 << 20,
        "replay": None,                  # raw IQ file
        "replay_block_bytes": 16384,
    }

    @classmethod
    def validate(cls, cfg):
        problems = super().validate(cfg)
        if not cfg.get("port") and not cfg.get("replay"):
            problems.append("missing required key 'port'")
        if cfg.get("replay") and not Path(cfg["replay"]).is_file():
            problems.append(f"replay file not found: {cfg['replay']}")
        return problems

    async def setup(self) -> None:
        if self.cfg["replay"]:
            if not self.fs:
                raise RuntimeError("sample_rate_hz is required for replay")
            return
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, int(self.cfg["rcvbuf_bytes"]))
        except OSError:
            pass
        self.sock.bind((self.cfg["bind"], int(self.cfg["port"])))
        self.sock.setblocking(False)
        self.ctx.log(f"listening on udp/{self.cfg['port']}")
        await super().setup()

    async def run(self) -> None:
        if not self.cfg["replay"]:
            try:
                await super().run()
            finally:
                if getattr(self, "sock", None):
                    self.sock.close()
            return
        await self.setup()
        blob = Path(self.cfg["replay"]).read_bytes()
        size = int(self.cfg["replay_block_bytes"])
        bps = SAMPLE_FORMATS[self.cfg["sample_format"]][3]
        t0 = now_ns()
        for i in range(0, len(blob) - size + 1, size):
            t = (i // bps) / float(self.fs)             # sample clock
            self.process(t0 + int(t * 1e9), blob[i:i + size], mono=t)
            if (i // size) % 64 == 0:
                await asyncio.sleep(0)
        self.close()
        self.ctx.log(f"replayed {len(blob) // size} IQ blocks")

    async def blocks(self):
        loop = asyncio.get_running_loop()
        while True:
            data = await loop.sock_recv(self.sock, 1 << 20)
            yield now_ns(), data
