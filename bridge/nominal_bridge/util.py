"""Small shared helpers: time, geodesy, flattening, rotating capture logs."""
from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path

RAD2DEG = 180.0 / math.pi
_FRAC = re.compile(r"\.(\d+)")


def now_ns() -> int:
    return time.time_ns()


def log(*parts) -> None:
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *parts, flush=True)


def iso_to_ns(text: str) -> int | None:
    """ISO-8601 (any fractional precision, 'Z' or offset) -> epoch ns, or None."""
    if not text or not isinstance(text, str):
        return None
    try:
        frac_ns = 0
        m = _FRAC.search(text)
        if m:
            digits = m.group(1)
            frac_ns = int(digits[:9].ljust(9, "0"))
            text = text[:m.start()] + text[m.end():]
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp()) * 1_000_000_000 + frac_ns
    except (ValueError, OverflowError):
        return None


def iso_duration_s(text: str) -> float | None:
    """ISO-8601 time duration such as PT1H2M3.5S -> seconds."""
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", text or "")
    if not m or not any(m.groups()):
        return None
    h, mn, s = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mn * 60 + s


def ecef_to_lla(x: float, y: float, z: float) -> tuple[float, float, float]:
    """WGS84 ECEF metres -> (lat deg, lon deg, height above ellipsoid m)."""
    a, f = 6378137.0, 1 / 298.257223563
    b = a * (1 - f)
    e2 = f * (2 - f)
    ep2 = (a * a - b * b) / (b * b)
    p = math.hypot(x, y)
    if p < 1e-9:
        return math.copysign(90.0, z), 0.0, abs(z) - b
    theta = math.atan2(z * a, p * b)
    lat = math.atan2(z + ep2 * b * math.sin(theta) ** 3,
                     p - e2 * a * math.cos(theta) ** 3)
    lon = math.atan2(y, x)
    n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    return math.degrees(lat), math.degrees(lon), p / math.cos(lat) - n


def dbfs(x: float, floor: float = 1e-9, power: bool = False) -> float:
    return (10.0 if power else 20.0) * math.log10(max(x, 0.0) + floor)


class RotatingLog:
    """Append-only capture file with size-capped rotation (keeps the newest N)."""

    def __init__(self, directory: Path, prefix: str, max_bytes: int = 25 << 20,
                 keep: int = 2, suffix: str = ".bin"):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.prefix, self.max_bytes, self.keep, self.suffix = prefix, max_bytes, keep, suffix
        self._fh = None
        self._written = 0

    def write(self, payload: bytes) -> None:
        if self._fh is None or self._written >= self.max_bytes:
            if self._fh:
                self._fh.close()
            for old in sorted(self.dir.glob(f"{self.prefix}_*{self.suffix}"))[: -self.keep]:
                old.unlink(missing_ok=True)
            self._fh = open(self.dir / f"{self.prefix}_{time.time_ns()}{self.suffix}", "ab")
            self._written = 0
        self._fh.write(payload)
        self._written += len(payload)

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


def prune(directory: Path, pattern: str, keep: int) -> None:
    for old in sorted(Path(directory).glob(pattern))[:-keep]:
        old.unlink(missing_ok=True)
