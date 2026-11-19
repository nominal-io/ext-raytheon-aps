import json
import math

import pytest

from conftest import ROOT
from nominal_bridge.flatten import flatten
from nominal_bridge.sources.owp_uci import HELLO_WORLD_TOPICS, default_topic, parse_topics
from nominal_bridge.util import iso_duration_s, iso_to_ns

SLEET = ROOT / "sleet-services.d" / "nominal-bridge.toml"


def envelope_file(tmp_path, *docs):
    """Write bare {"<Message>": {...}} lines, the shape OWP delivers."""
    p = tmp_path / "msgs.jsonl"
    p.write_text("\n".join(json.dumps(d) for d in docs) + "\n")
    return str(p)


def header(ts="2026-11-18T12:00:00.123456789Z", service=None):
    h = {"SystemID": {"UUID": "550e8400-e29b-41d4-a716-446655440000", "DescriptiveLabel": "lab"},
         "Timestamp": ts, "SchemaVersion": "002.5.0", "Mode": "LIVE"}
    if service:
        h["ServiceID"] = {"UUID": "550e8400-e29b-41d4-a716-446655440001", "DescriptiveLabel": service}
    return h


def test_time_parsing():
    assert iso_to_ns("2026-11-18T12:00:00.123456789Z") % 1_000_000_000 == 123_456_789
    assert iso_to_ns("2026-11-18T12:00:00Z") == iso_to_ns("2026-11-18T12:00:00+00:00")
    assert iso_to_ns("garbage") is None
    assert iso_duration_s("PT1H2M3.5S") == 3723.5
    assert iso_duration_s("") is None


def test_default_topics_match_the_sleet_authorisation_file():
    """Every topic the bridge subscribes to by default must be authorised in Sleet."""
    allowed = SLEET.read_text()
    for message, topic in parse_topics(HELLO_WORLD_TOPICS):
        assert f'"{topic}"' in allowed, topic
        assert f'allowed_messages = ["{message}"]' in allowed, message
    assert default_topic("ESM_Activity") == "mission.esm-activity"
    assert default_topic("PositionReportDetailed") == "mission.position-report-detailed"
    assert parse_topics([{"message": "X", "topic": "lab.x"}, ["Y", "lab.y"]]) == \
        [("X", "lab.x"), ("Y", "lab.y")]
    with pytest.raises(ValueError):
        parse_topics([{"topic": "no-message"}])


def test_replay_of_recorded_kit_capture(run_sources, fixtures):
    sink, _ = run_sources([{"name": "uci", "type": "uci_owp", "record": False,
                            "save_samples": False,
                            "replay": str(fixtures / "uci_sample.jsonl")}])
    lat = sink.values("uci.position.latitude_deg", entity="Eagle-1")
    assert len(lat) == 57 and lat[0] == pytest.approx(35.97, abs=0.01)
    assert set(sink.values("uci.system_status.state")) == {"OPERATIONAL"}
    assert "squall.ir.mfa" in {p.tags.get("subsystem") for p in sink.points}
    assert sink.values("uci.msg_total.ServiceStatus")[-1] == 171
    assert sink.values("uci.handler_errors_total")[-1] == 0
    # header timestamps (ns precision) are used, not the receive time
    p = next(p for p in sink.points if p.channel == "uci.position.latitude_deg")
    assert 1_791_000_000_000_000_000 < p.ts_ns < 1_792_000_000_000_000_000


def test_every_kit_message_fixture_is_handled(run_sources, fixtures, tmp_path):
    """All 11 message fixtures from the Sleet test-suite go through without a handler
    error, and the ones with no typed handler still produce channels."""
    docs = [json.loads(f.read_text()) for f in sorted((fixtures / "uci").glob("*.json"))]
    assert len(docs) == 11
    sink, _ = run_sources([{"name": "uci", "type": "uci_owp", "record": False,
                            "save_samples": False, "replay": envelope_file(tmp_path, *docs)}])
    assert sink.values("uci.handler_errors_total")[-1] == 0
    chans = sink.channels()
    for d in docs:
        assert f"uci.msg_total.{next(iter(d))}" in chans
    # generic flattener: list elements become a tag, not part of the channel name
    assert sink.values("uci.esm_activity.Activity.ActivityState", activity="0") == ["DISABLED"]
    assert sink.values("uci.esm_activity.Activity.Interactive", activity="0") == [1.0]
    assert "uci.operatornotification.Severity" in chans


def test_typed_handlers_units_and_events(run_sources, tmp_path):
    az, el = math.radians(30), math.radians(-5)
    omr = lambda n: {"ObservationMeasurementReport": {"MessageHeader": header(service="ir"), "MessageData": {  # noqa: E731
        "ObservationMeasurement": [{"Measurements": [
            {"MeasurementKinematics": {"LOS_AzEl": {"Measurement": {"LineOfSight": {
                "Azimuth": az, "Elevation": el}}}}}] * n}]}}}
    sig = {"SignalReport": {"MessageHeader": header(service="esm"), "MessageData": {"Signal": [
        {"SignalState": "NEW", "Parametrics": {"Frequency": {"FrequencyAverage": 915e6}}},
        {"SignalState": "UPDATE", "Parametrics": {"Frequency": {"FrequencyAverage": 1090e6}}}]}}}
    svc = lambda s: {"ServiceStatus": {"MessageHeader": header(), "MessageData": {  # noqa: E731
        "ServiceID": {"DescriptiveLabel": "esm-skill"}, "ServiceState": s, "TimeUp": "PT90S"}}}
    sink, _ = run_sources([{
        "name": "uci", "type": "uci_owp", "record": False, "save_samples": False,
        "replay": envelope_file(tmp_path, omr(2), omr(0), sig, svc("NORMAL"), svc("DEGRADED")),
        "presence": {"ObservationMeasurementReport": {"label": "IR track", "timeout_s": 5},
                     "SignalReport": {"label": "ESM signal", "timeout_s": 10}}}])
    assert sink.values("uci.ir_observation.azimuth_deg") == [pytest.approx(30.0)] * 2
    assert sink.values("uci.ir_observation.measurements_per_report") == [2.0, 0.0]
    # two signals in one report stay distinguishable
    assert sink.values("uci.signal.frequency_hz", service="esm") == [915e6, 1090e6]
    assert sink.values("uci.signal.frequency_hz", signal="1") == [1090e6]
    assert sink.values("uci.service_status.time_up_s", service="esm-skill") == [90.0, 90.0]
    names = [e.name for e in sink.events]
    assert names == ["IR track acquired", "ESM signal acquired",
                     "Service esm-skill: NORMAL -> DEGRADED"]
    assert sink.events[-1].type == "FLAG"


def test_handler_overrides_and_malformed_messages(run_sources, tmp_path):
    bad = {"PositionReport": {"MessageHeader": header(), "MessageData": {
        "DisplayName": "X", "InertialState": {"Position": {"Latitude": "oops", "Longitude": 1}}}}}
    good = {"PositionReport": {"MessageHeader": header(), "MessageData": {
        "DisplayName": "X", "InertialState": {"Position": {"Latitude": 0.5, "Longitude": 1.0,
                                                           "Altitude": 10}}}}}
    nav = {"NavigationReport": {"MessageHeader": header(), "MessageData": {"Source": "ACTUAL"}}}
    sink, _ = run_sources([{
        "name": "uci", "type": "uci_owp", "record": False, "save_samples": False,
        "replay": envelope_file(tmp_path, bad, good, nav),
        "handlers": {"NavigationReport": "off"}}])
    assert sink.values("uci.handler_errors_total")[-1] == 1          # counted, not fatal
    assert sink.values("uci.position.latitude_deg", entity="X") == [pytest.approx(math.degrees(0.5))]
    assert "uci.navigation.source" not in sink.channels()


def test_flatten_rules():
    rows = flatten({"A": {"B": 1, "Id": "550e8400-e29b-41d4-a716-446655440000", "On": True},
                    "L": [{"V": 1.5}, {"V": 2.5}], "S": ["x", "y"], "One": ["z"],
                    "When": "2026-01-01T00:00:00Z"}, "p")
    assert ("p.A.B", 1.0, {}) in rows and ("p.A.On", 1.0, {}) in rows
    assert ("p.L.V", 1.5, {"l": "0"}) in rows and ("p.L.V", 2.5, {"l": "1"}) in rows
    assert ("p.S.0", "x", {}) in rows and ("p.One", "z", {}) in rows
    assert not any("Id" in r[0] or "When" in r[0] for r in rows)
    assert len(flatten({f"k{i}": i for i in range(500)}, max_fields=10)) == 10
