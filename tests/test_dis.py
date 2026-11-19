import math
import struct

import pytest

from conftest import entity_state_pdu
from nominal_bridge.sources.dis import parse_entity_state
from nominal_bridge.util import ecef_to_lla


def write_capture(path, pdus, t0=1_800_000_000_000_000_000, dt_ns=100_000_000):
    with open(path, "wb") as fh:
        for i, pdu in enumerate(pdus):
            fh.write(struct.pack(">QH", t0 + i * dt_ns, len(pdu)) + pdu)
    return path


def test_ecef_to_lla_known_points():
    lat, lon, alt = ecef_to_lla(6378137.0, 0.0, 0.0)
    assert (lat, lon) == (0.0, 0.0) and abs(alt) < 1e-6
    lat, lon, alt = ecef_to_lla(0.0, 0.0, 6356752.314245)
    assert lat == 90.0 and abs(alt) < 1e-3


def test_parse_entity_state_roundtrip():
    es = parse_entity_state(entity_state_pdu("Eagle-1", 7, (1.0, 2.0, 3.0), force=1,
                                             velocity=(3.0, 4.0, 0.0)))
    assert es["marking"] == "Eagle-1" and es["entity_id"] == 7 and es["force_id"] == 1
    assert es["ecef"] == (1.0, 2.0, 3.0) and es["velocity"] == (3.0, 4.0, 0.0)
    assert es["entity_type"] == "1.2.225.1.3.0.0"
    assert parse_entity_state(b"\x07\x01\x02" + bytes(141)) is None      # a Fire PDU
    assert parse_entity_state(b"short") is None


def test_replay_of_recorded_kit_capture(run_sources, fixtures):
    """Real PDUs recorded from Supercell: the fixed WOBR-953 tower at Kitty Hawk."""
    sink, _ = run_sources([{"name": "dis", "type": "dis_entity_state", "record": False,
                            "replay": str(fixtures / "dis_sample.bin")}])
    lats = sink.values("dis.position.latitude_deg", entity="WOBR-953")
    lons = sink.values("dis.position.longitude_deg", entity="WOBR-953")
    assert len(lats) == 300
    assert lats[0] == pytest.approx(35.865, abs=0.01)
    assert lons[0] == pytest.approx(-75.650, abs=0.01)
    assert sink.values("dis.pdu_total")[-1] == 300
    assert [e.name for e in sink.events] == ["DIS entity appeared: WOBR-953"]
    assert sink.events[0].properties["force_id"] == "3"
    # original timestamps from the recording are preserved
    ts = sorted(p.ts_ns for p in sink.points if p.channel == "dis.force_id")
    assert 1_791_000_000_000_000_000 < ts[0] < ts[-1] < 1_792_000_000_000_000_000


def test_many_entities_are_tagged_and_capped(run_sources, tmp_path):
    """A busy picture (e.g. ADS-B-derived truth): one series per entity, a hard cap on
    cardinality, and a bounded number of 'appeared' events."""
    pdus = [entity_state_pdu(f"AC{i:03d}", i, (6378137.0 + 10000, i * 100.0, 0.0),
                             velocity=(100.0, 0.0, 0.0)) for i in range(50)] * 2
    cap = write_capture(tmp_path / "many.bin", pdus)
    sink, _ = run_sources([{"name": "dis", "type": "dis_entity_state", "record": False,
                            "replay": str(cap), "max_entities": 20, "entity_event_limit": 5}])
    entities = {p.tags["entity"] for p in sink.points if "entity" in p.tags}
    assert len(entities) == 20
    assert len(sink.events) == 5
    assert sink.values("dis.entities_ignored_total")[-1] == 60
    assert sink.values("dis.velocity.speed_mps", entity="AC000") == [100.0, 100.0]


def test_decimation_and_custom_naming(run_sources, tmp_path):
    pdus = [entity_state_pdu("", 9, (6378137.0, 0.0, 0.0))] * 50          # 10 Hz, no marking
    cap = write_capture(tmp_path / "one.bin", pdus)
    sink, _ = run_sources([{"name": "dis", "type": "dis_entity_state", "record": False,
                            "replay": str(cap), "min_interval_s": 1.0, "entity_tag": "track",
                            "channel_prefix": "truth", "entity_events": False}])
    alts = sink.values("truth.position.altitude_m", track="entity-1.1.9")
    assert len(alts) == 5                                                  # 5 s at 1 Hz
    assert all(math.isclose(a, 0.0, abs_tol=1e-6) for a in alts)
    assert sink.events == []


def test_exercise_filter(run_sources, tmp_path):
    cap = write_capture(tmp_path / "ex.bin", [
        entity_state_pdu("A", 1, (6378137.0, 0, 0), exercise=1),
        entity_state_pdu("B", 2, (6378137.0, 0, 0), exercise=2)])
    sink, _ = run_sources([{"name": "dis", "type": "dis_entity_state", "record": False,
                            "replay": str(cap), "exercise_id": 2}])
    assert {p.tags.get("entity") for p in sink.points if p.tags} == {"B"}
