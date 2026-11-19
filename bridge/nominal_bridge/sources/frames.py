"""Raw video frames (IR / EO) over UDP, one frame per datagram by default.

Derived channels are computed on the fly; the frames themselves are recorded to disk
so they can be rendered to MP4 and uploaded as a Nominal video channel afterwards.

To read frames from a different transport, subclass FrameSource and override
`frames()`; everything else (statistics, recording, snapshots) is inherited.
"""
from __future__ import annotations

import asyncio
import json
import socket
import time
from collections import deque
from pathlib import Path
from typing import AsyncIterator

import numpy as np

from ..core import Source, register_source
from ..util import now_ns, prune
from .squall import Tap

# name -> (numpy dtype, channels, full-scale value)
PIXEL_FORMATS = {
    "mono8": ("u1", 1, 255), "mono16le": ("<u2", 1, 65535), "mono16be": (">u2", 1, 65535),
    "rgb8": ("u1", 3, 255), "bgr8": ("u1", 3, 255),
}


class FrameSource(Source):
    DEFAULTS = {
        "stream": "sensor",
        "width": None,                   # null = ask Couloir (when `couloir:` is set)
        "height": None,
        "pixel_format": "mono8",
        "channel_prefix": "ir",
        "stats_interval_s": 0.5,
        "bright_level": None,            # counts; null = half of full scale
        "snapshot_png_s": 30,            # 0 = off; writes <capture_dir>/<prefix>_latest.png
        "record": {"enabled": True, "every_n": 2, "keep": 7200, "dir": None},
        "couloir": None,
    }

    @classmethod
    def validate(cls, cfg):
        problems = super().validate(cfg)
        if cfg.get("pixel_format", "mono8") not in PIXEL_FORMATS:
            problems.append(f"pixel_format must be one of {sorted(PIXEL_FORMATS)}")
        if not cfg.get("couloir") and not (cfg.get("width") and cfg.get("height")):
            problems.append("set width and height (or a `couloir:` block to read them "
                            "from the sensor)")
        if cfg.get("couloir") and cfg["couloir"].get("kind") not in ("ir", "rf"):
            problems.append("couloir.kind must be 'ir' or 'rf'")
        return problems

    def __init__(self, name, cfg, ctx):
        super().__init__(name, cfg, ctx)
        self.record = {**FrameSource.DEFAULTS["record"], **(self.cfg.get("record") or {})}
        self.width, self.height = self.cfg["width"], self.cfg["height"]
        self.dtype, self.planes, self.full_scale = PIXEL_FORMATS[self.cfg["pixel_format"]]
        self.n = self.mismatch = 0
        self._arrivals: deque[float] = deque(maxlen=64)
        self._last_stats = self._last_png = 0.0
        self._rec_dir: Path | None = None
        self.tap: Tap | None = None
        self._last_rx = 0.0

    @property
    def frame_bytes(self) -> int | None:
        if not (self.width and self.height):
            return None
        return int(self.width) * int(self.height) * self.planes * np.dtype(self.dtype).itemsize

    async def frames(self) -> AsyncIterator[tuple[int, bytes]]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def setup(self) -> None:
        """Register with Couloir (if configured) and learn the frame geometry."""
        if self.cfg["couloir"]:
            self.tap = Tap(self.ctx, self.cfg["couloir"], int(self.cfg.get("port") or 0))
            await self.tap.register()
            if not self.frame_bytes:
                st = await self.tap.status()
                if st is not None and st.ir_status.width and st.ir_status.height:
                    self.width, self.height = st.ir_status.width, st.ir_status.height
                    self.ctx.log(f"frame size {self.width}x{self.height} from Couloir")
        if not self.frame_bytes:
            raise RuntimeError("frame size unknown: set width/height in the config")

    async def run(self) -> None:
        watch = None
        try:
            await self.setup()
            if self.tap:
                watch = asyncio.create_task(self.tap.watch(lambda: self._last_rx))
            async for ts_ns, data in self.frames():
                self._last_rx = time.monotonic()
                self.process(ts_ns, data)
        finally:
            if watch:
                watch.cancel()
            if self.tap:
                await asyncio.to_thread(self.tap.release)

    # ── per-frame work ───────────────────────────────────────────────────────
    def process(self, ts_ns: int, data: bytes, mono: float | None = None) -> None:
        mono = time.monotonic() if mono is None else mono
        p = self.cfg["channel_prefix"]
        if len(data) != self.frame_bytes:
            self.mismatch += 1
            if self.mismatch in (1, 100, 10000):
                self.ctx.log(f"frame of {len(data)} bytes does not match {self.width}x"
                             f"{self.height} {self.cfg['pixel_format']} = {self.frame_bytes} "
                             f"bytes ({self.mismatch} so far); check width/height/pixel_format")
            self.ctx.emit(f"{p}.frame.size_mismatch_total", float(self.mismatch), ts_ns=ts_ns)
            return
        self.n += 1
        self._arrivals.append(mono)
        if mono - self._last_stats >= float(self.cfg["stats_interval_s"]):
            self._last_stats = mono
            self._stats(ts_ns, data)
        snap = float(self.cfg["snapshot_png_s"] or 0)
        if snap and mono - self._last_png >= snap:
            self._last_png = mono
            self._png(data)
        if self.record["enabled"] and self.n % max(int(self.record["every_n"]), 1) == 0:
            self._record(ts_ns, data)

    def _image(self, data: bytes) -> np.ndarray:
        a = np.frombuffer(data, dtype=self.dtype)
        if self.planes == 3:                       # luma for statistics
            a = a.reshape(self.height, self.width, 3).astype(np.float32)
            w = (0.299, 0.587, 0.114) if self.cfg["pixel_format"] == "rgb8" else (0.114, 0.587, 0.299)
            return a @ np.array(w, dtype=np.float32)
        return a.reshape(self.height, self.width)

    def _stats(self, ts_ns: int, data: bytes) -> None:
        p = self.cfg["channel_prefix"]
        img = self._image(data)
        e = lambda ch, v: self.ctx.emit(f"{p}.{ch}", float(v), ts_ns=ts_ns)  # noqa: E731
        rate = 0.0
        if len(self._arrivals) >= 2 and self._arrivals[-1] > self._arrivals[0]:
            rate = (len(self._arrivals) - 1) / (self._arrivals[-1] - self._arrivals[0])
        level = self.cfg["bright_level"]
        level = (self.full_scale + 1) // 2 if level is None else level
        p999 = float(np.percentile(img, 99.9))
        bright = int((img > level).sum())
        e("frame.rate_hz", rate)
        e("frame.mean_intensity", img.mean())
        e("frame.max_intensity", img.max())
        e("frame.p999_intensity", p999)
        e("frame.saturated_px", int((img >= self.full_scale).sum()))
        e("frame.bright_px", bright)
        e("frame.count", self.n)
        if bright:
            ys, xs = np.where(img > max(level, p999 * 0.9))
            if len(xs):
                e("target.centroid_x_px", xs.mean())
                e("target.centroid_y_px", ys.mean())
                e("target.pixel_count", len(xs))

    def _png(self, data: bytes) -> None:
        try:
            from PIL import Image
            img = self._image(data).astype(np.float32)
            img = (img * (255.0 / self.full_scale)).clip(0, 255).astype(np.uint8)
            self.ctx.capture_dir.mkdir(parents=True, exist_ok=True)
            Image.fromarray(img, "L").save(
                self.ctx.capture_dir / f"{self.cfg['channel_prefix']}_latest.png")
        except Exception as e:
            self.ctx.log(f"snapshot failed: {e!r}")

    def _record(self, ts_ns: int, data: bytes) -> None:
        if self._rec_dir is None:
            sub = self.record["dir"] or f"{self.cfg['channel_prefix']}_frames"
            self._rec_dir = self.ctx.capture_dir / sub
            self._rec_dir.mkdir(parents=True, exist_ok=True)
            (self._rec_dir / "meta.json").write_text(json.dumps({
                "width": self.width, "height": self.height,
                "pixel_format": self.cfg["pixel_format"], "source": self.name,
                "naming": "<utc_ns>.raw, one frame per file"}, indent=1))
        (self._rec_dir / f"{ts_ns}.raw").write_bytes(data)
        if self.n % 200 == 0:
            prune(self._rec_dir, "*.raw", int(self.record["keep"]))


@register_source("udp_frames")
class UdpFrameSource(FrameSource):
    DEFAULTS = {
        "bind": "0.0.0.0",
        "port": None,
        "reassemble": False,     # true = frames span several datagrams (concatenate until full)
        "frame_gap_ms": 20,      # with reassemble: a pause this long starts a new frame
        "rcvbuf_bytes": 4 << 20,
        "replay": None,          # directory of <utc_ns>.raw files
    }

    @classmethod
    def validate(cls, cfg):
        problems = super().validate(cfg)
        if not cfg.get("port") and not cfg.get("replay"):
            problems.append("missing required key 'port'")
        if cfg.get("replay") and not Path(cfg["replay"]).is_dir():
            problems.append(f"replay directory not found: {cfg['replay']}")
        return problems

    async def setup(self) -> None:
        if not self.cfg["replay"]:
            # bind before registering so no frames are lost
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF,
                                     int(self.cfg["rcvbuf_bytes"]))
            except OSError:
                pass
            self.sock.bind((self.cfg["bind"], int(self.cfg["port"])))
            self.sock.setblocking(False)
            self.ctx.log(f"listening on udp/{self.cfg['port']}")
            await super().setup()
        elif not self.frame_bytes:
            meta = Path(self.cfg["replay"]) / "meta.json"
            if meta.is_file():
                m = json.loads(meta.read_text())
                self.width, self.height = m["width"], m["height"]

    async def run(self) -> None:
        if self.cfg["replay"]:
            await self.setup()
            files = sorted(Path(self.cfg["replay"]).glob("*.raw"))
            for i, f in enumerate(files):
                ts = int(f.stem) if f.stem.isdigit() else now_ns()
                self.process(ts, f.read_bytes(), mono=ts / 1e9)
                if i % 50 == 0:
                    await asyncio.sleep(0)
            self.ctx.log(f"replayed {len(files)} frames")
            return
        try:
            await super().run()
        finally:
            if getattr(self, "sock", None):
                self.sock.close()

    async def frames(self):
        loop = asyncio.get_running_loop()
        if not self.cfg["reassemble"]:
            while True:
                data = await loop.sock_recv(self.sock, 1 << 20)
                yield now_ns(), data
        gap = float(self.cfg["frame_gap_ms"]) / 1000.0
        buf, last, ts = bytearray(), 0.0, 0
        while True:
            data = await loop.sock_recv(self.sock, 1 << 20)
            t = time.monotonic()
            if buf and t - last > gap:
                self.mismatch += 1          # incomplete frame, drop it
                buf.clear()
            if not buf:
                ts = now_ns()
            buf += data
            last = t
            if len(buf) >= self.frame_bytes:
                yield ts, bytes(buf[:self.frame_bytes])
                buf.clear()
