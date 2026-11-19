#!/usr/bin/env python3
"""Render the bridge's spectrogram waterfall log to H.264 and ingest it as a
Nominal video channel on the Raw Sensor Streams dataset.

Waterfall log format (udp_iq source): big-endian u64 ns timestamp + <bins>
big-endian float32 log-power bins per row, ~4 rows/s. A meta.json beside the log
gives bins, sample rate and centre frequency (Hello World SK: 512 bins, 2.048 MHz
span @ 915 MHz).

Usage: render_waterfall.py <profile> <sensor_dataset_rid> [container=nominal-bridge]
           [waterfall_dir=waterfall] [channel=rf.video.spectrogram_915mhz] [sensor_tag=squall-rf]
"""
from __future__ import annotations

import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

WINDOW_ROWS = 240          # 60 s of history per video frame

# Compact "inferno-like" colormap from anchor points
ANCHORS = np.array([
    [0, 0, 4], [40, 11, 84], [101, 21, 110], [159, 42, 99],
    [212, 72, 66], [245, 125, 21], [250, 193, 39], [252, 255, 164],
], dtype=np.float32)

def colormap(norm: np.ndarray) -> np.ndarray:
    idx = norm * (len(ANCHORS) - 1)
    lo = np.clip(idx.astype(int), 0, len(ANCHORS) - 2)
    frac = (idx - lo)[..., None]
    return (ANCHORS[lo] * (1 - frac) + ANCHORS[lo + 1] * frac).astype(np.uint8)

def main() -> None:
    profile, ds_rid = sys.argv[1], sys.argv[2]
    container = sys.argv[3] if len(sys.argv) > 3 else "nominal-bridge"
    wf_dir = sys.argv[4] if len(sys.argv) > 4 else "waterfall"
    channel = sys.argv[5] if len(sys.argv) > 5 else "rf.video.spectrogram_915mhz"
    sensor = sys.argv[6] if len(sys.argv) > 6 else "squall-rf"

    workdir = Path(tempfile.mkdtemp(prefix="waterfall_"))
    subprocess.run(["docker", "cp", f"{container}:/capture/{wf_dir}",
                    str(workdir / "waterfall")], check=True, capture_output=True)
    meta_path = workdir / "waterfall" / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    BINS = int(meta.get("bins", 512))
    ROW_BYTES = 8 + BINS * 4
    FS_MHZ = (meta.get("sample_rate_hz") or 2_048_000) / 1e6
    FC_MHZ = (meta.get("center_frequency_hz") or 915_000_000) / 1e6
    raw = b"".join(f.read_bytes()
                   for f in sorted((workdir / "waterfall").glob("wf_*.bin")))
    n_rows = len(raw) // ROW_BYTES
    ts = np.empty(n_rows, dtype=np.int64)
    rows = np.empty((n_rows, BINS), dtype=np.float32)
    for i in range(n_rows):
        off = i * ROW_BYTES
        ts[i] = struct.unpack_from(">Q", raw, off)[0]
        rows[i] = np.frombuffer(raw, dtype=">f4", count=BINS, offset=off + 8)
    order = np.argsort(ts)
    ts, rows = ts[order], rows[order]
    print(f"waterfall rows: {n_rows} spanning "
          f"{(ts[-1] - ts[0]) / 1e9 / 60:.1f} min")

    lo, hi = np.percentile(rows, 1), np.percentile(rows, 99.9)
    norm = np.clip((rows - lo) / max(hi - lo, 1e-6), 0, 1)

    # Each video frame: scrolling 60 s window ending at that row's timestamp.
    # Step 2 rows -> 2 fps of wall time, frame timestamps are true row times.
    step = 2
    frame_idx = list(range(WINDOW_ROWS, n_rows, step))
    h, w = WINDOW_ROWS, BINS
    ffmpeg = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo",
         "-pixel_format", "rgb24", "-video_size", f"{w}x{h}", "-framerate", "4",
         "-i", "-", "-vf", "scale=1024:480:flags=neighbor",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "fast", "-g", "4",
         "rf_waterfall.mp4"], stdin=subprocess.PIPE)
    for i in frame_idx:
        img = colormap(norm[i - WINDOW_ROWS:i][::-1])  # newest row on top
        ffmpeg.stdin.write(img.tobytes())
    ffmpeg.stdin.close()
    ffmpeg.wait()
    frame_ts = [int(ts[i]) for i in frame_idx]
    print(f"encoded rf_waterfall.mp4: {len(frame_ts)} frames")

    from nominal.core.client import NominalClient
    client = NominalClient.from_profile(profile)
    ds = client.get_dataset(ds_rid)
    vf = ds.add_video("rf_waterfall.mp4", channel=channel,
                      frame_timestamps=frame_ts, tags={"sensor": sensor})
    vf.poll_until_ingestion_completed()
    print(f"ingested video channel {channel} "
          f"({FC_MHZ} MHz center, {FS_MHZ} MHz span, 60 s scroll window)")

if __name__ == "__main__":
    main()
