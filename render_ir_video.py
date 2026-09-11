#!/usr/bin/env python3
"""Render the bridge's recorded raw IR frames to H.264 and ingest as a Nominal
video channel with true per-frame timestamps.

Frame files (bridge/bridge.py): /capture/ir_frames/<ns>.raw, headerless mono8
320x200, recorded at 4 fps (every 2nd live frame).

Usage: render_ir_video.py <profile> <sensor_dataset_rid> [container=nominal-bridge]
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

WIDTH, HEIGHT = 320, 200


def main() -> None:
    profile, ds_rid = sys.argv[1], sys.argv[2]
    container = sys.argv[3] if len(sys.argv) > 3 else "nominal-bridge"

    workdir = Path(tempfile.mkdtemp(prefix="ir_frames_"))
    subprocess.run(["docker", "cp", f"{container}:/capture/ir_frames",
                    str(workdir / "ir_frames")], check=True, capture_output=True)
    frames = sorted((workdir / "ir_frames").glob("*.raw"))
    ts = []
    with open(workdir / "all.gray", "wb") as out:
        for f in frames:
            b = f.read_bytes()
            if len(b) == WIDTH * HEIGHT:
                out.write(b)
                ts.append(int(f.stem))
    print(f"frames: {len(ts)}")

    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo",
         "-pixel_format", "gray", "-video_size", f"{WIDTH}x{HEIGHT}",
         "-framerate", "4", "-i", str(workdir / "all.gray"),
         "-vf", "scale=640:400:flags=neighbor",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "fast",
         "ir_sensor.mp4"], check=True)

    from nominal.core.client import NominalClient
    client = NominalClient.from_profile(profile)
    ds = client.get_dataset(ds_rid)
    vf = ds.add_video("ir_sensor.mp4", channel="ir.video.eagle1_nose",
                      frame_timestamps=ts, tags={"sensor": "squall-ir"})
    vf.poll_until_ingestion_completed()
    print("ingested video channel ir.video.eagle1_nose")


if __name__ == "__main__":
    main()
