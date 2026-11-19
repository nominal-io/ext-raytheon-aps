#!/usr/bin/env python3
"""Render the bridge's recorded raw IR frames to H.264 and ingest as a Nominal
video channel with true per-frame timestamps.

Frame files (udp_frames source): /capture/<dir>/<utc_ns>.raw, headerless, with a
meta.json giving width / height / pixel_format. The Hello World SK sensor is mono8
320x200, recorded at 4 fps (every 2nd live frame).

Usage: render_ir_video.py <profile> <sensor_dataset_rid> [container=nominal-bridge]
           [frames_dir=ir_frames] [channel=ir.video.eagle1_nose] [sensor_tag=squall-ir]
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

# ffmpeg rawvideo names and bytes per pixel for the bridge's pixel formats
PIX = {"mono8": ("gray", 1), "mono16le": ("gray16le", 2), "mono16be": ("gray16be", 2),
       "rgb8": ("rgb24", 3), "bgr8": ("bgr24", 3)}


def main() -> None:
    profile, ds_rid = sys.argv[1], sys.argv[2]
    container = sys.argv[3] if len(sys.argv) > 3 else "nominal-bridge"
    frames_dir = sys.argv[4] if len(sys.argv) > 4 else "ir_frames"
    channel = sys.argv[5] if len(sys.argv) > 5 else "ir.video.eagle1_nose"
    sensor = sys.argv[6] if len(sys.argv) > 6 else "squall-ir"

    workdir = Path(tempfile.mkdtemp(prefix="ir_frames_"))
    subprocess.run(["docker", "cp", f"{container}:/capture/{frames_dir}",
                    str(workdir / "frames")], check=True, capture_output=True)
    meta_path = workdir / "frames" / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.is_file() else \
        {"width": 320, "height": 200, "pixel_format": "mono8"}
    WIDTH, HEIGHT = int(meta["width"]), int(meta["height"])
    pix_fmt, bpp = PIX[meta["pixel_format"]]
    frames = sorted((workdir / "frames").glob("*.raw"))
    ts = []
    with open(workdir / "all.gray", "wb") as out:
        for f in frames:
            b = f.read_bytes()
            if len(b) == WIDTH * HEIGHT * bpp:
                out.write(b)
                ts.append(int(f.stem))
    print(f"frames: {len(ts)} ({WIDTH}x{HEIGHT} {meta['pixel_format']})")
    if not ts:
        raise SystemExit("no frames recorded yet")
    # measured frame rate, so playback speed matches the recording
    fps = max(1.0, round((len(ts) - 1) / max((ts[-1] - ts[0]) / 1e9, 1e-3), 2)) if len(ts) > 1 else 4.0
    scale = 2 if WIDTH <= 640 else 1

    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo",
         "-pixel_format", pix_fmt, "-video_size", f"{WIDTH}x{HEIGHT}",
         "-framerate", str(fps), "-i", str(workdir / "all.gray"),
         "-vf", f"scale={WIDTH * scale}:{HEIGHT * scale}:flags=neighbor",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "fast",
         "-g", str(max(1, int(round(fps)))),          # 1 s GOP so scrubbing stays sharp
         "ir_sensor.mp4"], check=True)

    from nominal.core.client import NominalClient
    client = NominalClient.from_profile(profile)
    ds = client.get_dataset(ds_rid)
    vf = ds.add_video("ir_sensor.mp4", channel=channel,
                      frame_timestamps=ts, tags={"sensor": sensor})
    vf.poll_until_ingestion_completed()
    print("ingested video channel", channel)


if __name__ == "__main__":
    main()
