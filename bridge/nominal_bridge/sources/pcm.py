"""PCM audio from a TCP server (the rf-fm-demod skill serves S16LE mono @ 32 kHz).
Emits level channels and keeps rolling WAV segments for attachment/upload."""
from __future__ import annotations

import asyncio
import math
import time
import wave
from collections import deque

import numpy as np

from ..core import Source, register_source
from ..util import prune

_FORMATS = {"s16le": ("<i2", 32768.0, 2), "s16be": (">i2", 32768.0, 2), "f32le": ("<f4", 1.0, 4)}


@register_source("tcp_pcm")
class TcpPcmSource(Source):
    DEFAULTS = {
        "stream": "sensor",
        "host": "127.0.0.1",
        "port": None,
        "sample_rate_hz": 32000,
        "sample_format": "s16le",
        "channels": 1,
        "channel_prefix": "audio",
        "stats_interval_s": 0.5,
        "active_rms": 0.01,              # linear RMS above which audio counts as "active"
        "record": {"enabled": True, "segment_s": 60, "ring_s": 120, "keep": 12, "dir": None},
        "connect_timeout_s": 5,
    }
    REQUIRED = ("port",)

    @classmethod
    def validate(cls, cfg):
        problems = super().validate(cfg)
        if cfg.get("sample_format", "s16le") not in _FORMATS:
            problems.append(f"sample_format must be one of {sorted(_FORMATS)}")
        return problems

    async def run(self) -> None:
        dtype, scale, width = _FORMATS[self.cfg["sample_format"]]
        rec = {**TcpPcmSource.DEFAULTS["record"], **(self.cfg.get("record") or {})}
        fs, nch = int(self.cfg["sample_rate_hz"]), int(self.cfg["channels"])
        p = self.cfg["channel_prefix"]
        ring: deque[int] = deque(maxlen=fs * nch * width * int(rec["ring_s"]))
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.cfg["host"], int(self.cfg["port"])),
            float(self.cfg["connect_timeout_s"]))
        self.ctx.log(f"connected to {self.cfg['host']}:{self.cfg['port']}")
        last_emit, last_wav, pending = 0.0, time.monotonic(), b""
        try:
            while True:
                chunk = await reader.read(16384)
                if not chunk:
                    raise ConnectionError("audio stream closed by peer")
                ring.extend(chunk)
                pending += chunk
                t = time.monotonic()
                if t - last_emit >= float(self.cfg["stats_interval_s"]):
                    last_emit = t
                    usable = len(pending) - len(pending) % width
                    a = np.frombuffer(pending[:usable], dtype=dtype).astype(np.float32) / scale
                    pending = pending[usable:]
                    if len(a):
                        rms = float(np.sqrt(np.mean(a ** 2)))
                        self.ctx.emit(f"{p}.rms_dbfs", 20 * math.log10(rms + 1e-9))
                        self.ctx.emit(f"{p}.peak_dbfs",
                                      20 * math.log10(float(np.max(np.abs(a))) + 1e-9))
                        self.ctx.emit(f"{p}.active", rms > float(self.cfg["active_rms"]))
                if rec["enabled"] and width == 2 and t - last_wav > float(rec["segment_s"]) \
                        and len(ring) > fs:
                    last_wav = t
                    d = self.ctx.capture_dir / (rec["dir"] or f"{p}_segments")
                    d.mkdir(parents=True, exist_ok=True)
                    data = bytes(ring)
                    data = data[:len(data) - len(data) % (width * nch)]
                    with wave.open(str(d / f"audio_{time.time_ns()}.wav"), "wb") as w:
                        w.setnchannels(nch)
                        w.setsampwidth(width)
                        w.setframerate(fs)
                        w.writeframes(data)
                    prune(d, "audio_*.wav", int(rec["keep"]))
        finally:
            writer.close()
