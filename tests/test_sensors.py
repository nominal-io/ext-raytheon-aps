import json
import math
import struct

import numpy as np
import pytest

from nominal_bridge.sources.iq import to_complex


def test_ir_frames_from_the_kit(run_sources, fixtures):
    """Real 320x200 mono8 frames recorded from squall-optical."""
    sink, _ = run_sources([{"name": "ir", "type": "udp_frames", "width": 320, "height": 200,
                            "replay": str(fixtures / "ir_frames"), "stats_interval_s": 0,
                            "record": {"enabled": False}, "snapshot_png_s": 0,
                            "tags": {"sensor": "squall-ir"}}])
    assert sink.values("ir.frame.count", sensor="squall-ir") == [1.0, 2.0, 3.0, 4.0]
    assert max(sink.values("ir.frame.bright_px")) == 88           # the bandit in view
    assert all(0 <= x < 320 for x in sink.values("ir.target.centroid_x_px"))
    assert all(0 <= y < 200 for y in sink.values("ir.target.centroid_y_px"))


@pytest.mark.parametrize("fmt,dtype,planes,hot", [
    ("mono16le", "<u2", 1, 60000), ("mono16be", ">u2", 1, 60000), ("rgb8", "u1", 3, 250)])
def test_other_frame_formats(run_sources, tmp_path, fmt, dtype, planes, hot):
    """A different camera: 64x48, 16-bit or colour, with one bright 4x4 target."""
    d = tmp_path / "frames"
    d.mkdir()
    img = np.zeros((48, 64, planes), dtype=dtype)
    img[20:24, 40:44] = hot
    (d / "1800000000000000000.raw").write_bytes(img.tobytes())
    (d / "1800000000100000000.raw").write_bytes(b"\x00" * 100)          # wrong size
    sink, _ = run_sources([{"name": "eo", "type": "udp_frames", "width": 64, "height": 48,
                            "pixel_format": fmt, "replay": str(d), "channel_prefix": "eo",
                            "stats_interval_s": 0, "snapshot_png_s": 0,
                            "record": {"enabled": True, "every_n": 1, "dir": "eo_rec"}}])
    assert sink.values("eo.target.pixel_count") == [16.0]
    assert sink.values("eo.target.centroid_x_px") == [pytest.approx(41.5)]
    assert sink.values("eo.target.centroid_y_px") == [pytest.approx(21.5)]
    assert sink.values("eo.frame.size_mismatch_total") == [1.0]         # counted, not fatal
    meta = json.loads((tmp_path / "eo_rec" / "meta.json").read_text())
    assert (meta["width"], meta["height"], meta["pixel_format"]) == (64, 48, fmt)
    assert len(list((tmp_path / "eo_rec").glob("*.raw"))) == 1


def test_iq_silence_and_waterfall_log(run_sources, tmp_path):
    """Silence must not produce NaN/inf, and the waterfall log has the documented layout."""
    raw = tmp_path / "quiet.sc16"
    rng = np.random.default_rng(0)
    raw.write_bytes(bytes(16384 * 4) + rng.integers(-300, 300, 16384 * 2, dtype="<i2").tobytes())
    sink, _ = run_sources([{"name": "rf", "type": "udp_iq", "sample_rate_hz": 2_048_000,
                            "center_frequency_hz": 915_000_000, "stats_interval_s": 0.003,
                            "spectrum_interval_s": 0.003, "snapshot": {"enabled": False},
                            "waterfall": {"interval_s": 0.002}, "replay": str(raw)}])
    rms = sink.values("rf.iq.rms_dbfs")
    assert rms[0] == pytest.approx(-120.0) and -60 < rms[-1] < -30
    assert all(math.isfinite(p.value) for p in sink.points)
    assert all(abs(v - 1.0) < 0.5 for v in sink.values("rf.iq.delivery_ratio"))
    wf = next((tmp_path / "waterfall").glob("wf_*.bin")).read_bytes()
    assert len(wf) % (8 + 512 * 4) == 0 and len(wf) > 0
    assert json.loads((tmp_path / "waterfall" / "meta.json").read_text())["sample_rate_hz"] == 2_048_000


@pytest.mark.parametrize("fmt,dtype,scale,offset", [
    ("sc16le", "<i2", 32767, 0), ("sc8", "i1", 127, 0), ("cu8", "u1", 127, 127.5),
    ("cf32le", "<f4", 1.0, 0)])
def test_iq_formats_find_a_tone(run_sources, tmp_path, fmt, dtype, scale, offset):
    """A different radio: 1 Msps with a tone at +100 kHz, in each supported sample format."""
    fs, f0, n = 1_000_000, 100_000, 65536
    t = np.arange(n) / fs
    tone = 0.5 * np.exp(2j * np.pi * f0 * t)
    inter = np.empty(2 * n, dtype=np.float64)
    inter[0::2], inter[1::2] = tone.real, tone.imag
    raw = (inter * scale + offset).astype(dtype).tobytes()
    assert abs(np.abs(to_complex(raw, fmt)).mean() - 0.5) < 0.01
    path = tmp_path / f"tone.{fmt}"
    path.write_bytes(raw)
    sink, _ = run_sources([{"name": "sdr", "type": "udp_iq", "sample_format": fmt,
                            "sample_rate_hz": fs, "center_frequency_hz": 1_090_000_000,
                            "channel_prefix": "sdr", "replay": str(path),
                            "replay_block_bytes": 4096, "stats_interval_s": 0.02,
                            "spectrum_interval_s": 0.02, "waterfall": {"enabled": False},
                            "snapshot": {"enabled": False}}])
    assert sink.values("sdr.spectrum.peak_offset_hz")[0] == pytest.approx(f0, abs=fs / 8192)
    assert sink.values("sdr.spectrum.peak_frequency_hz")[0] == pytest.approx(1_090_100_000, abs=200)
    assert sink.values("sdr.iq.rms_dbfs")[0] == pytest.approx(20 * math.log10(0.5), abs=0.3)
    assert sink.values("sdr.iq.clip_fraction")[0] == 0.0


def test_adsb_sbs(run_sources, tmp_path):
    feed = tmp_path / "sbs.txt"
    feed.write_text(
        "MSG,1,1,1,A1B2C3,1,2026/11/18,12:00:00.000,2026/11/18,12:00:00.000,UAL123  ,,,,,,,,,,,\n"
        "MSG,3,1,1,A1B2C3,1,2026/11/18,12:00:01.000,2026/11/18,12:00:01.000,,35000,,,33.19,-96.59,,,0,0,0,0\n"
        "MSG,4,1,1,A1B2C3,1,2026/11/18,12:00:02.000,2026/11/18,12:00:02.000,,,450,270,,,-640,,,,,\n"
        "MSG,3,1,1,ABCDEF,1,2026/11/18,12:00:03.000,2026/11/18,12:00:03.000,,1200,,,33.0,-96.0,,,0,0,0,-1\n"
        "STA,,,,,,,,,,,\nnot,a,valid,line\n")
    sink, _ = run_sources([{"name": "adsb", "type": "adsb_sbs", "replay": str(feed)}])
    assert sink.values("adsb.callsign", icao="A1B2C3") == ["UAL123"]
    assert sink.values("adsb.latitude_deg", icao="A1B2C3") == [33.19]
    assert sink.values("adsb.altitude_m", icao="A1B2C3") == [pytest.approx(10668.0)]
    assert sink.values("adsb.ground_speed_kt", icao="A1B2C3") == [450.0]
    assert sink.values("adsb.on_ground", icao="ABCDEF") == [1.0]
    assert sink.values("adsb.aircraft_active")[-1] == 2.0


def test_json_lines_custom_sensor(run_sources, tmp_path):
    feed = tmp_path / "custom.jsonl"
    feed.write_text(
        '{"t": 1800000000.5, "unit": "tower-1", "az_deg": 12.5, "status": "TRACK", "temps": {"fpa_k": 77.2}}\n'
        'not json\n'
        '{"t": 1800000001500, "unit": "tower-1", "az_deg": 13.0, "locked": true}\n')
    sink, _ = run_sources([{"name": "tower", "type": "json_lines", "transport": "file",
                            "path": str(feed), "channel_prefix": "tower",
                            "timestamp_field": "t", "tag_fields": {"unit": "unit"}}])
    assert sink.values("tower.az_deg", unit="tower-1") == [12.5, 13.0]
    assert sink.values("tower.temps.fpa_k") == [77.2]
    assert sink.values("tower.status") == ["TRACK"] and sink.values("tower.locked") == [1.0]
    assert "tower.unit" not in sink.channels() and "tower.t" not in sink.channels()
    ts = sorted({p.ts_ns for p in sink.points})
    assert ts == [1_800_000_000_500_000_000, 1_800_000_001_500_000_000]   # s and ms both accepted
