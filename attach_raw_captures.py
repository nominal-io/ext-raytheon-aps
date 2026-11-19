#!/usr/bin/env python3
"""Attach the bridge's raw stream captures to a Nominal run.

Pulls the newest of each raw capture family out of the bridge container and
uploads them as run attachments: SC16 IQ snapshot + provenance sidecar, DIS
PDU binary log, UCI JSONL log, FM audio WAV segment, and a sample raw IR frame.

Usage: attach_raw_captures.py <profile> <run_rid> [container=nominal-bridge]
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from nominal.core.client import NominalClient


def newest(container: str, pattern: str, workdir: Path) -> Path | None:
    ls = subprocess.run(
        ["docker", "exec", container, "sh", "-c", f"ls -t {pattern} 2>/dev/null | head -1"],
        capture_output=True, text=True)
    src = ls.stdout.strip()
    if not src:
        return None
    dst = workdir / Path(src).name
    subprocess.run(["docker", "cp", f"{container}:{src}", str(dst)],
                   check=True, capture_output=True)
    return dst


def main() -> None:
    profile, run_rid = sys.argv[1], sys.argv[2]
    container = sys.argv[3] if len(sys.argv) > 3 else "nominal-bridge"
    workdir = Path(tempfile.mkdtemp(prefix="raw_captures_"))

    targets = [
        ("/capture/iq_snapshots/*.sc16",
         "Raw SC16 IQ snapshot tapped from the Squall RF MEL data plane. Interleaved "
         "little-endian int16 I/Q. Sample rate and centre frequency are in the matching "
         ".json sidecar."),
        ("/capture/iq_snapshots/*.json",
         "Provenance sidecar for the SC16 IQ snapshot: start timestamp (ns), sample "
         "rate, center frequency, byte/sample counts."),
        ("/capture/dis_raw/dis_*.bin",
         "Raw DIS EntityState PDU log from multicast 239.1.2.3:21100. Record format: "
         "big-endian u64 receive-time ns + u16 length + verbatim PDU bytes."),
        ("/capture/uci_raw/uci_*.bin",
         "Raw UCI message log (JSONL): every OWP message verbatim with receive "
         "timestamp, as routed by the Sleet LA-CAL."),
        ("/capture/audio_segments/audio_*.wav",
         "FM demod audio segment (S16LE mono 32 kHz WAV) from the rf-fm-demod skill's "
         "PCM stream - the FM broadcast as received."),
        ("/capture/ir_frames/*.raw",
         "Sample raw IR frame (headerless, filename = capture ns timestamp; geometry in "
         "meta.json beside it) tapped from the Squall IR MEL data plane."),
    ]

    client = NominalClient.from_profile(profile)
    run = client.get_run(run_rid)
    attachments = []
    for pattern, desc in targets:
        path = newest(container, pattern, workdir)
        if path is None:
            print("skip (no file):", pattern)
            continue
        att = client.create_attachment(path, description=desc,
                                       labels=["ams-gra", "raw-capture"])
        attachments.append(att)
        print("attachment:", path.name, att.rid)
    if attachments:
        run.add_attachments(attachments)
        print(f"attached {len(attachments)} raw captures to run {run_rid}")


if __name__ == "__main__":
    main()
