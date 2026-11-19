import asyncio
import json
import textwrap

import pytest

from conftest import ROOT
from nominal_bridge import Source, config, register_source, runner
from nominal_bridge.__main__ import main
from nominal_bridge.core import EventGate, MemorySink, load_plugins
from nominal_bridge.sources.prometheus import parse

PLUGINS = ROOT / "examples" / "plugins"


def write(tmp_path, text, name="bridge.yaml"):
    p = tmp_path / name
    p.write_text(textwrap.dedent(text))
    return p


def test_shipped_configs_are_valid(monkeypatch):
    from nominal_bridge import sinks, sources  # noqa: F401
    for k in ("NOMINAL_BASE_URL", "NOMINAL_TOKEN", "NOMINAL_DS_DIS", "NOMINAL_DS_UCI",
              "NOMINAL_DS_SENSOR", "NOMINAL_DS_OBS", "NOMINAL_DS_ADSB"):
        monkeypatch.setenv(k, "x")
    monkeypatch.chdir(ROOT)
    for name in ("hello-world-sk.yaml", "lab-template.yaml", "replay.yaml"):
        cfg = config.load(ROOT / "configs" / name)
        load_plugins(cfg["plugins"])
        assert config.validate(cfg) == [], name


def test_env_interpolation_and_helpful_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_PORT", "5000")
    monkeypatch.delenv("NOT_SET", raising=False)
    cfg = config.load(write(tmp_path, """
        sinks:
          - type: nominal
            targets: [{name: t, token: "${NOT_SET}", datasets: {sensor: rid}}]
        sources:
          - {name: a, type: udp_frames, port: "${MY_PORT}", width: 4, height: 4}
          - {name: a, type: udp_iq, port: "${OTHER:-6000}"}
          - {name: c, type: nope}
          - {name: d, type: dis_entity_state, enabled: false, entity_name: wrong}
    """))
    assert cfg["sources"][0]["port"] == "5000" and cfg["sources"][1]["port"] == "6000"
    errs = "\n".join(config.validate(cfg))
    assert "a: duplicate source name" in errs
    assert "set sample_rate_hz" in errs
    assert "unknown type 'nope'. Available: adsb_sbs" in errs
    assert "set 'token' or 'profile'" in errs
    assert "unset environment variables referenced in config: NOT_SET" in errs
    assert "entity_name" not in errs                    # disabled sources are not checked
    # a stream with no dataset is reported
    cfg2 = config.load(write(tmp_path, """
        sinks: [{type: nominal, targets: [{name: t, token: x, datasets: {dis: rid}}]}]
        sources: [{name: a, type: udp_frames, port: 1, width: 4, height: 4}]
    """, "b.yaml"))
    assert config.validate(cfg2) == ["nominal target 't': no dataset RID for stream 'sensor'"]
    assert config.validate(cfg2, dry_run=True) == []


def test_event_gate_throttles():
    g = EventGate(max_per_minute=3, min_interval_s=5)
    assert g.allow("a", now=0) and not g.allow("a", now=1) and g.allow("a", now=6)
    assert g.allow("b", now=7) and not g.allow("c", now=8)       # ceiling of 3 per minute
    assert g.allow("c", now=70) and g.dropped == 2


def test_plugins_add_a_uci_handler_and_a_source_type(run_sources, fixtures, tmp_path):
    doc = json.loads((fixtures / "uci" / "ESM_Activity.json").read_text())
    msgs = tmp_path / "esm.jsonl"
    changed = json.loads(json.dumps(doc))
    changed["ESM_Activity"]["MessageData"]["Activity"][0]["ActivityState"] = "ACTIVE_FULLY_CONSTRAINED"
    msgs.write_text(json.dumps(doc) + "\n" + json.dumps(changed) + "\n")
    sink, _ = run_sources([{"name": "uci", "type": "uci_owp", "record": False,
                            "save_samples": False, "replay": str(msgs)}],
                          plugins=[str(PLUGINS)])
    assert sink.values("uci.esm.activity_state", activity="550e8400") == \
        ["DISABLED", "ACTIVE_FULLY_CONSTRAINED"]
    assert [e.name for e in sink.events] == \
        ["ESM activity 550e8400: DISABLED -> ACTIVE_FULLY_CONSTRAINED"]
    assert "uci.esm_activity.Activity.ActivityState" not in sink.channels()   # typed wins

    from nominal_bridge.core import SOURCE_TYPES
    from nominal_bridge.sources.uci_handlers import UCI_HANDLERS
    UCI_HANDLERS.pop("ESM_Activity")                 # leave the registry as we found it
    assert "tcp_frames" in SOURCE_TYPES


def test_plugin_tcp_frame_source_end_to_end(run_sources, tmp_path):
    """The example tcp_frames plugin against a real local TCP server."""
    import socket, struct, threading
    frame = bytes([10]) * (8 * 8 - 1) + bytes([255])
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        for _ in range(5):
            conn.sendall(struct.pack(">I", len(frame)) + frame)
        threading.Event().wait(0.5)
        conn.close()
    threading.Thread(target=serve, daemon=True).start()
    sink, hub = run_sources([{"name": "tower", "type": "tcp_frames", "port": port, "width": 8,
                              "height": 8, "channel_prefix": "eo", "stats_interval_s": 0,
                              "snapshot_png_s": 0, "record": {"enabled": False}}],
                            plugins=[str(PLUGINS)], duration_s=0.4)
    srv.close()
    assert sink.values("eo.frame.count") == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert sink.values("eo.frame.saturated_px")[0] == 1.0
    assert hub.points_by_source["tower"] > 0


def test_a_crashing_source_is_restarted_and_others_keep_running(tmp_path):
    calls = {"n": 0}

    @register_source("flaky_test_source")
    class Flaky(Source):
        DEFAULTS = {"stream": "sensor"}

        async def run(self):
            calls["n"] += 1
            self.ctx.emit("flaky.attempt", calls["n"])
            if calls["n"] == 1:
                raise RuntimeError("boom")
            await asyncio.sleep(3600)

    @register_source("steady_test_source")
    class Steady(Source):
        DEFAULTS = {"stream": "sensor"}

        async def run(self):
            while True:
                self.ctx.emit("steady.tick", 1)
                await asyncio.sleep(0.05)

    sink = MemorySink()
    cfg = {"sources": [{"name": "flaky", "type": "flaky_test_source"},
                       {"name": "steady", "type": "steady_test_source"}],
           "plugins": [], "capture_dir": str(tmp_path),
           "health": {"stream": "obs", "interval_s": 1}}
    asyncio.run(runner.run(cfg, sinks=[sink], duration_s=3.2))
    assert sink.values("flaky.attempt") == [1.0, 2.0]             # restarted after 2 s backoff
    assert len(sink.values("steady.tick")) > 30                   # never interrupted
    assert 0.0 in sink.values("bridge.source_up", source="flaky")
    assert sink.values("bridge.source_up", source="flaky")[-1] == 1.0
    assert sink.values("bridge.points_total", source="steady")[-1] > 10


def test_presence_lost_event(tmp_path):
    @register_source("presence_test_source")
    class P(Source):
        DEFAULTS = {"stream": "sensor"}

        async def run(self):
            self.ctx.presence("trk", "IR track", timeout_s=0.5)
            await asyncio.sleep(3600)

    sink = MemorySink()
    cfg = {"sources": [{"name": "p", "type": "presence_test_source"}], "plugins": [],
           "capture_dir": str(tmp_path), "events": {"min_interval_s": 0}}
    asyncio.run(runner.run(cfg, sinks=[sink], duration_s=2.3))
    assert [e.name for e in sink.events] == ["IR track acquired", "IR track lost"]


def test_prometheus_parser():
    text = textwrap.dedent('''
        # HELP sleet_messages_total x
        sleet_messages_total{topic="mission.position-report",dir="in"} 42
        sleet_latency_bucket{le="0.1"} 5
        process_cpu_seconds_total 1.5
        sleet_bad NaN
    ''')
    assert list(parse(text, ("sleet_",), 3)) == [
        ("sleet_messages_total", 42.0, {"topic": "mission.position-report", "dir": "in"})]
    assert len(list(parse(text, (), 3))) == 2


def test_jsonl_sink_and_cli_dry_run(tmp_path, capsys, monkeypatch, fixtures):
    monkeypatch.chdir(ROOT)
    cfg = write(tmp_path, f"""
        capture_dir: {tmp_path}
        plugins: []
        sinks: [{{type: jsonl, dir: {tmp_path}/out}}]
        sources:
          - {{name: dis, type: dis_entity_state, record: false, replay: {fixtures}/dis_sample.bin}}
    """)
    main(["run", "-c", str(cfg)])
    lines = next((tmp_path / "out").glob("points_*.jsonl")).read_text().splitlines()
    recs = [json.loads(line) for line in lines]
    assert recs[0]["s"] == "dis" and recs[0]["g"] == {"entity": "WOBR-953"}
    assert [r["event"] for r in recs if "event" in r] == ["DIS entity appeared: WOBR-953"]
    main(["run", "-c", str(cfg), "--dry-run"])
    out = capsys.readouterr().out
    assert "dis.position.latitude_deg {entity=WOBR-953}" in out and "totals:" in out
    with pytest.raises(SystemExit):
        main(["validate", "-c", str(write(tmp_path, "sources: []", "empty.yaml"))])
    assert "no sources defined" in capsys.readouterr().err


def test_env_file_writer(tmp_path):
    from nominal_bridge.provision import write_env
    env = tmp_path / ".env"
    env.write_text("# comment\nKEEP=1\nNOMINAL_DS_DIS=old\n")
    write_env(env, {"NOMINAL_DS_DIS": "new", "NOMINAL_RUN_RID": "r"})
    assert env.read_text() == "# comment\nKEEP=1\nNOMINAL_DS_DIS=new\nNOMINAL_RUN_RID=r\n"


def test_tap_reregisters_after_sensor_restart_but_does_not_steal_a_slot(tmp_path):
    """Sensor restart -> register again. Data stops while the sensor keeps running ->
    warn only (taking the slot back would disconnect another client)."""
    import time
    from types import SimpleNamespace
    from nominal_bridge.core import Context, Hub
    from nominal_bridge.sources.squall import Tap

    logs = []
    ctx = Context(Hub([], capture_dir=tmp_path), "ir", "sensor")
    ctx.log = lambda *parts: logs.append(" ".join(map(str, parts)))
    tap = Tap(ctx, {"kind": "ir", "stall_s": 0.05}, 21610)
    registrations = []

    async def fake_register():
        registrations.append(time.monotonic())
        tap.registered_at = time.monotonic()
    state = {"uptime": 1000, "running": True}

    async def fake_status():
        return SimpleNamespace(uptime_seconds=state["uptime"], running=state["running"])
    tap.register, tap.status = fake_register, fake_status

    async def scenario():
        tap.registered_at = time.monotonic() - 500        # we registered 500 s ago
        last = {"t": time.monotonic()}
        task = asyncio.create_task(tap.watch(lambda: last["t"], interval_s=0.02))
        await asyncio.sleep(0.15)                         # healthy sensor, data stalled
        assert registrations == [] and sum("Another" in m for m in logs) == 1
        state["uptime"] = 3                               # sensor restarted
        await asyncio.sleep(0.1)
        task.cancel()
    asyncio.run(scenario())
    assert len(registrations) == 1
    assert any("sensor restarted" in m for m in logs)
