#!/usr/bin/env python3
"""nominal-bridge: stream every capturable AMS GRA Hello World SK output to Nominal.

Taps (all on the shared host network namespace of the sim):
  1. DIS EntityState multicast (239.1.2.3:21100, UDP)     -> DIS dataset
  2. Sleet LA-CAL OWP WebSocket (ws://127.0.0.1:21402/owp) -> UCI dataset + events
  3. Squall IR MEL raw mono8 frames (UDP tap, self-registered via Couloir gRPC)
  4. Squall RF MEL raw SC16 IQ (UDP tap, self-registered via Couloir gRPC)
  5. rf-fm-demod PCM audio (TCP 127.0.0.1:21602, S16LE @ 32 kHz)
  6. Couloir GetStatus polling (RF + IR control plane)
  7. Prometheus metrics from all service admin endpoints    -> observability dataset
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import socket
import struct
import time
import wave
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

import grpc
import squall_ir_pb2, squall_ir_pb2_grpc
import squall_rf_pb2, squall_rf_pb2_grpc

from nominal.core.client import NominalClient

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL = os.environ["NOMINAL_BASE_URL"]
TOKEN = os.environ["NOMINAL_TOKEN"]
WORKSPACE = os.environ.get("NOMINAL_WORKSPACE_RID") or None
DS_DIS = os.environ["NOMINAL_DS_DIS"]
DS_UCI = os.environ["NOMINAL_DS_UCI"]
DS_SENSOR = os.environ["NOMINAL_DS_SENSOR"]
DS_OBS = os.environ["NOMINAL_DS_OBS"]
ASSET_RID = os.environ["NOMINAL_ASSET_RID"]

IR_TAP_PORT = int(os.environ.get("BRIDGE_IR_TAP_PORT", "21610"))
RF_TAP_PORT = int(os.environ.get("BRIDGE_RF_TAP_PORT", "21611"))
COULOIR = "127.0.0.1:21203"
OWP_URL = "ws://127.0.0.1:21402/owp"
CAPTURE = Path("/capture")

METRIC_ENDPOINTS = {
    "supercell": "http://127.0.0.1:21300/metrics",
    "sleet": "http://127.0.0.1:21312/metrics",
    "squall-rf": "http://127.0.0.1:21314/metrics",
    "squall-optical": "http://127.0.0.1:21316/metrics",
    "couloir": "http://127.0.0.1:21318/metrics",
    "graupel": "http://127.0.0.1:21401/metrics",
}
METRIC_PREFIXES = ("sleet_", "pipeline_", "squall_", "graupel_", "supercell_",
                   "czml_", "dis_", "owp_", "frames_", "blocks_")

TOPICS = [
    ("ESM_Activity", "mission.esm-activity"),
    ("NavigationReport", "mission.navigation-report"),
    ("ObservationMeasurementReport", "mission.observation-measurement-report"),
    ("OperatorNotification", "mission.operator-notification"),
    ("PO_Activity", "mission.po-activity"),
    ("PositionReport", "mission.position-report"),
    ("PositionReportDetailed", "mission.position-report-detailed"),
    ("RoutePlan", "mission.route-plan"),
    ("SignalReport", "mission.signal-report"),
    ("SubsystemStatus", "mission.subsystem-status"),
    ("SystemStatus", "mission.system-status"),
    ("ServiceStatus", "mission.service-status"),
]

RAD2DEG = 180.0 / math.pi

def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *a, flush=True)

# ── Nominal client / streams / events ────────────────────────────────────────
client = NominalClient.from_token(TOKEN, base_url=BASE_URL, workspace_rid=WORKSPACE) \
    if hasattr(NominalClient, "from_token") else None
if client is None:
    raise SystemExit("NominalClient.from_token missing")

_streams = {}

def stream_for(rid):
    if rid not in _streams:
        ds = client.get_dataset(rid)
        _streams[rid] = ds.get_write_stream(batch_size=20000, max_wait=timedelta(seconds=2))
    return _streams[rid]

def emit(rid, channel, ts, value, tags=None):
    try:
        stream_for(rid).enqueue(channel, ts, value, tags=tags or {})
    except Exception as e:
        log("emit error", channel, repr(e))

_event_lock = asyncio.Lock()

async def emit_event(name, etype="INFO", description=None, properties=None, start=None):
    from nominal.core.event import EventType
    et = getattr(EventType, etype, None) or EventType.INFO
    def _do():
        try:
            client.create_event(
                name, et, start or datetime.now(timezone.utc),
                description=description, assets=[ASSET_RID],
                properties=properties or {}, labels=["ams-gra"],
            )
        except Exception as e:
            log("event error", name, repr(e))
    async with _event_lock:
        await asyncio.get_event_loop().run_in_executor(None, _do)
    log("EVENT:", name)

# ── Helpers ───────────────────────────────────────────────────────────────────
_TS_FRAC = re.compile(r"(\.\d{1,9})")

def parse_uci_ts(s):
    try:
        m = _TS_FRAC.search(s)
        if m and len(m.group(1)) > 7:
            s = s.replace(m.group(1), m.group(1)[:7])
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)

def ecef_to_lla(x, y, z):
    """WGS84 ECEF -> lat(deg), lon(deg), alt HAE (m). Bowring's method."""
    a, f = 6378137.0, 1 / 298.257223563
    b = a * (1 - f)
    e2 = f * (2 - f)
    ep2 = (a * a - b * b) / (b * b)
    p = math.hypot(x, y)
    if p < 1e-9:
        lat = math.copysign(math.pi / 2, z)
        return math.degrees(lat), 0.0, abs(z) - b
    theta = math.atan2(z * a, p * b)
    lat = math.atan2(z + ep2 * b * math.sin(theta) ** 3,
                     p - e2 * a * math.cos(theta) ** 3)
    lon = math.atan2(y, x)
    n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    alt = p / math.cos(lat) - n
    return math.degrees(lat), math.degrees(lon), alt

def iso_dur_to_s(s):
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", s or "")
    if not m:
        return None
    h, mn, sec = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mn * 60 + sec

SKIP_KEYS = {"UUID", "SchemaVersion", "Classification", "OwnerProducer",
             "GovernmentIdentifier", "DescriptiveLabel", "MeasurementID",
             "ObservationMeasurementReportID", "MissionID"}

def flatten(obj, prefix, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in SKIP_KEYS:
                continue
            flatten(v, f"{prefix}.{k}" if prefix else k, out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            flatten(v, f"{prefix}.{i}" if len(obj) > 1 else prefix, out)
    elif isinstance(obj, bool):
        out[prefix] = 1.0 if obj else 0.0
    elif isinstance(obj, (int, float)):
        out[prefix] = float(obj)
    elif isinstance(obj, str):
        if not re.fullmatch(r"[0-9a-fA-F-]{36}|.*\d{4}-\d\d-\d\dT.*", obj):
            out[prefix] = obj

# ── Raw capture helpers ───────────────────────────────────────────────────────
class RotatingBin:
    """Append-only binary log with size-capped rotation (keep newest N files)."""

    def __init__(self, directory, prefix, max_bytes=50 << 20, keep=4):
        self.dir = CAPTURE / directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.prefix, self.max_bytes, self.keep = prefix, max_bytes, keep
        self.fh = None
        self.written = 0

    def write(self, payload: bytes):
        if self.fh is None or self.written >= self.max_bytes:
            if self.fh:
                self.fh.close()
            for old in sorted(self.dir.glob(f"{self.prefix}_*"))[: -self.keep]:
                old.unlink(missing_ok=True)
            self.fh = open(self.dir / f"{self.prefix}_{time.time_ns()}.bin", "ab")
            self.written = 0
        self.fh.write(payload)
        self.written += len(payload)

DIS_LOG = None      # ts_ns(u64) + len(u16) + raw PDU, big-endian
UCI_LOG = None      # JSONL, one message per line
WF_LOG = None       # ts_ns(u64) + 512 float32 log-power bins

# ── 1. DIS EntityState listener ───────────────────────────────────────────────
async def dis_task():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    sock.bind(("0.0.0.0", 21100))
    mreq = socket.inet_aton("239.1.2.3") + socket.inet_aton("127.0.0.1")
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    mreq2 = socket.inet_aton("239.1.2.3") + socket.inet_aton("0.0.0.0")
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq2)
    except OSError:
        pass
    sock.setblocking(False)
    log("DIS: listening on 239.1.2.3:21100")
    loop = asyncio.get_event_loop()
    entities_seen = {}
    last_count_emit = 0.0
    n_pdus = 0
    while True:
        data = await loop.sock_recv(sock, 2048)
        now = datetime.now(timezone.utc)
        if len(data) < 144 or data[2] != 1:
            continue
        n_pdus += 1
        DIS_LOG.write(struct.pack(">QH", time.time_ns(), len(data)) + data)
        try:
            site, app, eid = struct.unpack_from(">HHH", data, 12)
            force = data[18]
            vx, vy, vz = struct.unpack_from(">fff", data, 36)
            x, y, z = struct.unpack_from(">ddd", data, 48)
            psi, theta, phi = struct.unpack_from(">fff", data, 72)
            marking = data[129:140].split(b"\x00")[0].decode("ascii", "ignore").strip()
        except Exception:
            continue
        if not marking:
            marking = f"entity-{eid}"
        lat, lon, alt = ecef_to_lla(x, y, z)
        speed = math.sqrt(vx * vx + vy * vy + vz * vz)
        tags = {"entity": marking}
        emit(DS_DIS, "dis.position.latitude_deg", now, lat, tags)
        emit(DS_DIS, "dis.position.longitude_deg", now, lon, tags)
        emit(DS_DIS, "dis.position.altitude_m", now, alt, tags)
        emit(DS_DIS, "dis.velocity.speed_mps", now, speed, tags)
        emit(DS_DIS, "dis.velocity.ecef_x_mps", now, vx, tags)
        emit(DS_DIS, "dis.velocity.ecef_y_mps", now, vy, tags)
        emit(DS_DIS, "dis.velocity.ecef_z_mps", now, vz, tags)
        emit(DS_DIS, "dis.orientation.psi_rad", now, psi, tags)
        emit(DS_DIS, "dis.orientation.theta_rad", now, theta, tags)
        emit(DS_DIS, "dis.orientation.phi_rad", now, phi, tags)
        emit(DS_DIS, "dis.force_id", now, float(force), tags)
        first_seen = marking not in entities_seen
        entities_seen[marking] = time.time()
        if first_seen:
            await emit_event(f"DIS entity appeared: {marking}",
                             properties={"entity": marking, "entity_id": str(eid),
                                         "force_id": str(force)})
        if time.time() - last_count_emit > 2.0:
            last_count_emit = time.time()
            active = sum(1 for t in entities_seen.values() if time.time() - t < 10)
            emit(DS_DIS, "dis.entities_active", now, float(active))
            emit(DS_DIS, "dis.pdu_total", now, float(n_pdus))

# ── 2. OWP / UCI listener ─────────────────────────────────────────────────────
def md(msg):
    return msg.get("MessageData", {})

def header_tags(msg):
    h = msg.get("MessageHeader", {})
    tags = {}
    sys_id = h.get("SystemID", {})
    if sys_id.get("DescriptiveLabel"):
        tags["system"] = sys_id["DescriptiveLabel"]
    svc = h.get("ServiceID", {})
    if svc.get("DescriptiveLabel"):
        tags["service"] = svc["DescriptiveLabel"]
    return tags

class UciState:
    def __init__(self):
        self.svc_state = {}
        self.subsys_state = {}
        self.sys_state = {}
        self.last_obs_with_meas = 0.0
        self.ir_tracking = False
        self.last_signal = 0.0
        self.fm_locked = False
        self.msg_counts = Counter()
        self.samples_saved = set()

S = UciState()

async def handle_uci(name, msg, raw):
    now = datetime.now(timezone.utc)
    h = msg.get("MessageHeader", {})
    ts = parse_uci_ts(h.get("Timestamp", "")) if h.get("Timestamp") else now
    tags = header_tags(msg)
    S.msg_counts[name] += 1
    UCI_LOG.write((json.dumps({"t_rx": now.isoformat(), "message": name,
                               "payload": msg}, separators=(",", ":")) + "\n").encode())
    if name not in S.samples_saved:
        S.samples_saved.add(name)
        (CAPTURE / "samples").mkdir(parents=True, exist_ok=True)
        (CAPTURE / "samples" / f"{name}.json").write_text(raw)

    data = md(msg)
    if name == "PositionReport":
        ent = data.get("DisplayName", "unknown")
        t = {**tags, "entity": ent}
        ist = data.get("InertialState", {})
        pos = ist.get("Position", {})
        if "Latitude" in pos:
            emit(DS_UCI, "uci.position.latitude_deg", ts, pos["Latitude"] * RAD2DEG, t)
            emit(DS_UCI, "uci.position.longitude_deg", ts, pos["Longitude"] * RAD2DEG, t)
            emit(DS_UCI, "uci.position.altitude_m", ts, float(pos.get("Altitude", 0)), t)
        vel = ist.get("DomainVelocity", {})
        for k, v in vel.items():
            if isinstance(v, (int, float)):
                emit(DS_UCI, f"uci.velocity.{k.lower()}", ts, float(v), t)
    elif name == "ObservationMeasurementReport":
        oms = data.get("ObservationMeasurement", [])
        n_meas = 0
        for om in oms:
            for m in om.get("Measurements", []):
                n_meas += 1
                los = (m.get("MeasurementKinematics", {}).get("LOS_AzEl", {})
                       .get("Measurement", {}).get("LineOfSight", {}))
                if "Azimuth" in los:
                    emit(DS_UCI, "uci.ir_observation.azimuth_deg", ts,
                         los["Azimuth"] * RAD2DEG, tags)
                    emit(DS_UCI, "uci.ir_observation.elevation_deg", ts,
                         los["Elevation"] * RAD2DEG, tags)
        emit(DS_UCI, "uci.ir_observation.measurements_per_report", ts, float(n_meas), tags)
        if n_meas > 0:
            S.last_obs_with_meas = time.time()
            if not S.ir_tracking:
                S.ir_tracking = True
                await emit_event("IR track acquired",
                                 description="ir-search-and-track began reporting LOS measurements",
                                 properties={"skill": "ir-search-and-track"})
    elif name == "SignalReport":
        S.last_signal = time.time()
        signals = data.get("Signal", [])
        emit(DS_UCI, "uci.signal.count", ts, float(len(signals)), tags)
        for sig in signals:
            freq = (sig.get("Parametrics", {}).get("Frequency", {})
                    .get("FrequencyAverage"))
            if freq is not None:
                emit(DS_UCI, "uci.signal.frequency_hz", ts, float(freq), tags)
            if sig.get("SignalState"):
                emit(DS_UCI, "uci.signal.state", ts, str(sig["SignalState"]), tags)
            ck = sig.get("CollectorKinematics", [])
            if ck:
                fp = (ck[0].get("PlatformKinematics", {}).get("Position", {})
                      .get("FixedPoint", {}))
                if "Latitude" in fp:
                    emit(DS_UCI, "uci.signal.collector_latitude_deg", ts,
                         fp["Latitude"] * RAD2DEG, tags)
                    emit(DS_UCI, "uci.signal.collector_longitude_deg", ts,
                         fp["Longitude"] * RAD2DEG, tags)
        if not S.fm_locked:
            S.fm_locked = True
            await emit_event("FM signal acquired (WOBR-953)",
                             description="rf-fm-demod published a SignalReport",
                             properties={"skill": "rf-fm-demod", "station": "WOBR-953"})
    elif name == "ServiceStatus":
        svc = data.get("ServiceID", {}).get("DescriptiveLabel", "unknown")
        state = data.get("ServiceState", "UNKNOWN")
        t = {**tags, "service": svc}
        emit(DS_UCI, "uci.service_status.state", ts, state, t)
        up = iso_dur_to_s(data.get("TimeUp", ""))
        if up is not None:
            emit(DS_UCI, "uci.service_status.time_up_s", ts, up, t)
        prev = S.svc_state.get(svc)
        if prev is not None and prev != state:
            await emit_event(f"Service {svc}: {prev} -> {state}",
                             etype="FLAG" if state != "NORMAL" else "INFO",
                             properties={"service": svc, "from": prev, "to": state})
        S.svc_state[svc] = state
    elif name == "SubsystemStatus":
        sub = data.get("SubsystemID", {}).get("DescriptiveLabel", "unknown")
        state = data.get("SubsystemState", "UNKNOWN")
        t = {**tags, "subsystem": sub}
        emit(DS_UCI, "uci.subsystem_status.state", ts, state, t)
        prev = S.subsys_state.get(sub)
        if prev is not None and prev != state:
            await emit_event(f"Subsystem {sub}: {prev} -> {state}",
                             properties={"subsystem": sub, "from": prev, "to": state})
        S.subsys_state[sub] = state
    elif name == "SystemStatus":
        state = data.get("SystemState", "UNKNOWN")
        emit(DS_UCI, "uci.system_status.state", ts, state, tags)
        prev = S.sys_state.get("system")
        if prev is not None and prev != state:
            await emit_event(f"System state: {prev} -> {state}",
                             etype="FLAG" if state != "OPERATIONAL" else "INFO",
                             properties={"from": prev, "to": state})
        S.sys_state["system"] = state
    elif name == "NavigationReport":
        nav = data.get("Navigation", {})
        for key in ("Source", "ContingencyLevel"):
            if key in data:
                emit(DS_UCI, f"uci.navigation.{key.lower()}", ts, str(data[key]), tags)
        if nav.get("NavigationSource"):
            emit(DS_UCI, "uci.navigation.navigation_source", ts,
                 str(nav["NavigationSource"]), tags)
    else:
        flat = {}
        flatten(data, f"uci.{name.lower()}", flat)
        for ch, v in list(flat.items())[:40]:
            emit(DS_UCI, ch, ts, v, tags)
        if name in ("OperatorNotification", "ESM_Activity", "PO_Activity"):
            await emit_event(f"UCI {name}", description=raw[:900],
                             properties={"message": name})

async def uci_gap_watch():
    while True:
        await asyncio.sleep(1.0)
        now = time.time()
        if S.ir_tracking and now - S.last_obs_with_meas > 5.0:
            S.ir_tracking = False
            await emit_event("IR track lost",
                             description="No LOS measurements for 5 s (bandits out of FOV)",
                             properties={"skill": "ir-search-and-track"})
        if S.fm_locked and now - S.last_signal > 10.0:
            S.fm_locked = False
            await emit_event("FM signal lost (WOBR-953)",
                             description="No SignalReport for 10 s (out of range)",
                             properties={"skill": "rf-fm-demod", "station": "WOBR-953"})

async def owp_task():
    import websockets
    while True:
        try:
            async with websockets.connect(OWP_URL, subprotocols=["owp"],
                                          max_size=16_000_000) as ws:
                await ws.send('INIT {"versions":["1.0"],"schema":"002.5.0",'
                              '"service_id":"nominal-bridge","verbose":false}')
                info = await ws.recv()
                log("OWP: connected,", str(info)[:80])
                for i, (m, t) in enumerate(TOPICS):
                    await ws.send(f"SUB s{i} {m} {t}")
                last_rate = time.time()
                while True:
                    frame = await ws.recv()
                    if not isinstance(frame, str) or not frame.startswith("MSG "):
                        continue
                    _, sid, payload = frame.split(" ", 2)
                    try:
                        d = json.loads(payload)
                        name = next(iter(d))
                        await handle_uci(name, d[name], payload)
                    except Exception as e:
                        log("uci parse error", repr(e))
                    if time.time() - last_rate > 5.0:
                        last_rate = time.time()
                        now = datetime.now(timezone.utc)
                        for mname, cnt in S.msg_counts.items():
                            emit(DS_UCI, f"uci.msg_total.{mname}", now, float(cnt))
        except Exception as e:
            log("OWP reconnect after error:", repr(e))
            await asyncio.sleep(3)

# ── 3+4. Raw MEL taps ─────────────────────────────────────────────────────────
async def register_tap(kind):
    """Register our UDP port as an extra data destination. Retry forever."""
    while True:
        try:
            chan = grpc.insecure_channel(COULOIR)
            if kind == "ir":
                stub = squall_ir_pb2_grpc.SquallIrControlStub(chan)
                req = squall_ir_pb2.AddDataDestinationRequest(
                    udp_destination=f"127.0.0.1:{IR_TAP_PORT}",
                    client_id="nominal-bridge-ir")
            else:
                stub = squall_rf_pb2_grpc.SquallRfControlStub(chan)
                req = squall_rf_pb2.AddDataDestinationRequest(
                    udp_destination=f"127.0.0.1:{RF_TAP_PORT}",
                    client_id="nominal-bridge-rf")
            resp = await asyncio.get_event_loop().run_in_executor(
                None, lambda: stub.AddDataDestination(req, timeout=5))
            log(f"{kind.upper()} tap registered: success={resp.success} {resp.error_message}")
            if resp.success:
                return
        except Exception as e:
            log(f"{kind} tap register failed: {e!r}; retrying in 5s")
        await asyncio.sleep(5)

async def ir_tap_task():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    sock.bind(("0.0.0.0", IR_TAP_PORT))
    sock.setblocking(False)
    await register_tap("ir")
    loop = asyncio.get_event_loop()
    log("IR tap: listening on", IR_TAP_PORT)
    width, height = 320, 200
    n = 0
    window = deque(maxlen=64)
    last_emit = 0.0
    last_png = 0.0
    while True:
        data = await loop.sock_recv(sock, 1 << 20)
        now = datetime.now(timezone.utc)
        n += 1
        window.append(time.time())
        if len(data) != width * height:
            if len(data) in (64000, 76800, 307200):
                pass
            else:
                continue
        frame = np.frombuffer(data, dtype=np.uint8)
        t = time.time()
        if t - last_emit >= 0.5:
            last_emit = t
            rate = 0.0
            if len(window) >= 2:
                span = window[-1] - window[0]
                rate = (len(window) - 1) / span if span > 0 else 0.0
            fmean = float(frame.mean()); fmax = float(frame.max())
            p99 = float(np.percentile(frame, 99.9))
            sat = int((frame == 255).sum()); bright = int((frame > 128).sum())
            tags = {"sensor": "squall-ir"}
            emit(DS_SENSOR, "ir.frame.rate_hz", now, rate, tags)
            emit(DS_SENSOR, "ir.frame.mean_intensity", now, fmean, tags)
            emit(DS_SENSOR, "ir.frame.max_intensity", now, fmax, tags)
            emit(DS_SENSOR, "ir.frame.p999_intensity", now, p99, tags)
            emit(DS_SENSOR, "ir.frame.saturated_px", now, float(sat), tags)
            emit(DS_SENSOR, "ir.frame.bright_px", now, float(bright), tags)
            emit(DS_SENSOR, "ir.frame.count", now, float(n), tags)
            if bright > 0 and len(data) == width * height:
                img = frame.reshape(height, width)
                ys, xs = np.where(img > max(128, p99 * 0.9))
                if len(xs):
                    emit(DS_SENSOR, "ir.target.centroid_x_px", now, float(xs.mean()), tags)
                    emit(DS_SENSOR, "ir.target.centroid_y_px", now, float(ys.mean()), tags)
                    emit(DS_SENSOR, "ir.target.pixel_count", now, float(len(xs)), tags)
        if t - last_png >= 30.0 and len(data) == width * height:
            last_png = t
            try:
                from PIL import Image
                Image.fromarray(frame.reshape(height, width), "L").save(
                    CAPTURE / "ir_latest.png")
            except Exception:
                pass
        # Record every 2nd frame (4 fps) for post-run MP4 -> Nominal video
        if n % 2 == 0 and len(data) == width * height:
            rec = CAPTURE / "ir_frames"
            rec.mkdir(exist_ok=True)
            ns = time.time_ns()
            (rec / f"{ns}.raw").write_bytes(data)
            if n % 200 == 0:
                frames = sorted(rec.glob("*.raw"))
                for old in frames[:-7200]:
                    old.unlink(missing_ok=True)

async def rf_tap_task():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    sock.bind(("0.0.0.0", RF_TAP_PORT))
    sock.setblocking(False)
    await register_tap("rf")
    loop = asyncio.get_event_loop()
    log("RF tap: listening on", RF_TAP_PORT)
    acc = []
    acc_samples = 0
    n_pkts = 0
    window = deque(maxlen=256)
    last_emit = 0.0
    last_fft = 0.0
    last_wf = 0.0
    fft_buf = np.zeros(0, dtype=np.complex64)
    FS = 2_048_000
    # Raw IQ snapshots: 2 s of SC16 every 5 min, JSON sidecar, keep last 10
    snap_dir = CAPTURE / "iq_snapshots"
    snap_dir.mkdir(exist_ok=True)
    snap_fh, snap_until, snap_meta, last_snap = None, 0.0, None, time.time() - 240
    while True:
        data = await loop.sock_recv(sock, 1 << 20)
        now = datetime.now(timezone.utc)
        n_pkts += 1
        window.append(time.time())
        t_ns = time.time_ns()
        if snap_fh is None and time.time() - last_snap >= 300:
            last_snap = time.time()
            snap_until = last_snap + 2.0
            snap_meta = {"start_utc_ns": t_ns, "format": "sc16le_interleaved_iq",
                         "sample_rate_sps": FS, "center_frequency_hz": 915_000_000,
                         "source": "squall-rf MEL tap (nominal-bridge)", "bytes": 0}
            snap_fh = open(snap_dir / f"iq_{t_ns}.sc16", "wb")
        if snap_fh is not None:
            snap_fh.write(data)
            snap_meta["bytes"] += len(data)
            if time.time() >= snap_until:
                snap_fh.close()
                snap_fh = None
                snap_meta["samples"] = snap_meta["bytes"] // 4
                (snap_dir / f"iq_{snap_meta['start_utc_ns']}.json").write_text(
                    json.dumps(snap_meta, indent=1))
                for old in sorted(snap_dir.glob("iq_*.sc16"))[:-10]:
                    old.unlink(missing_ok=True)
                    sc = old.with_suffix(".json")
                    sc.unlink(missing_ok=True)
                log(f"IQ snapshot saved ({snap_meta['bytes']} bytes)")
        iq = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        if len(iq) % 2:
            iq = iq[:-1]
        acc.append(iq)
        acc_samples += len(iq) // 2
        c_new = iq[0::2] + 1j * iq[1::2]
        fft_buf = np.concatenate([fft_buf, c_new.astype(np.complex64)])[-8192:]
        t = time.time()
        if t - last_wf >= 0.25 and len(fft_buf) >= 4096:
            last_wf = t
            seg = fft_buf[-4096:] * np.hanning(4096).astype(np.float32)
            spec = np.fft.fftshift(np.abs(np.fft.fft(seg)) ** 2)
            row = 10.0 * np.log10(spec.reshape(512, 8).mean(axis=1) + 1e-12)
            WF_LOG.write(struct.pack(">Q", time.time_ns())
                         + row.astype(">f4").tobytes())
        if t - last_emit >= 0.5 and acc_samples > 0:
            last_emit = t
            buf = np.concatenate(acc)
            acc, total = [], acc_samples
            acc_samples = 0
            c = buf[0::2] + 1j * buf[1::2]
            power = np.mean(np.abs(c) ** 2)
            rms_dbfs = 10 * math.log10(power + 1e-12)
            peak = float(np.max(np.abs(c))) if len(c) else 0.0
            peak_dbfs = 20 * math.log10(peak + 1e-9)
            clip = float(np.mean(np.abs(buf) > 0.999))
            rate = 0.0
            if len(window) >= 2:
                span = window[-1] - window[0]
                rate = (len(window) - 1) / span if span > 0 else 0.0
            tags = {"sensor": "squall-rf"}
            emit(DS_SENSOR, "rf.iq.rms_dbfs", now, rms_dbfs, tags)
            emit(DS_SENSOR, "rf.iq.peak_dbfs", now, peak_dbfs, tags)
            emit(DS_SENSOR, "rf.iq.clip_fraction", now, clip, tags)
            emit(DS_SENSOR, "rf.iq.packet_rate_hz", now, rate, tags)
            emit(DS_SENSOR, "rf.iq.samples_per_window", now, float(total), tags)
            if t - last_fft >= 2.0 and len(c) >= 4096:
                last_fft = t
                seg = c[:8192] * np.hanning(min(8192, len(c[:8192])))
                spec = np.fft.fftshift(np.abs(np.fft.fft(seg, 8192)) ** 2)
                freqs = np.fft.fftshift(np.fft.fftfreq(8192, 1 / FS))
                pk = int(np.argmax(spec))
                emit(DS_SENSOR, "rf.spectrum.peak_offset_hz", now, float(freqs[pk]), tags)
                emit(DS_SENSOR, "rf.spectrum.peak_power_db", now,
                     float(10 * np.log10(spec[pk] + 1e-12)), tags)

# ── 5. FM audio tap ───────────────────────────────────────────────────────────
async def audio_task():
    AUDIO_FS = 32000
    ring = deque(maxlen=AUDIO_FS * 2 * 120)  # 120 s of s16le bytes
    last_wav = time.time()
    while True:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", 21602)
            log("AUDIO: connected to rf-fm-demod:21602")
            last_emit = 0.0
            while True:
                chunk = await reader.read(16384)
                if not chunk:
                    raise ConnectionError("audio stream closed")
                now = datetime.now(timezone.utc)
                ring.extend(chunk)
                t = time.time()
                if t - last_emit >= 0.5:
                    last_emit = t
                    a = np.frombuffer(bytes(chunk), dtype="<i2").astype(np.float32) / 32768.0
                    if len(a):
                        rms = float(np.sqrt(np.mean(a ** 2)))
                        emit(DS_SENSOR, "audio.rms_dbfs", now,
                             20 * math.log10(rms + 1e-9), {"source": "rf-fm-demod"})
                        emit(DS_SENSOR, "audio.peak_dbfs", now,
                             20 * math.log10(float(np.max(np.abs(a))) + 1e-9),
                             {"source": "rf-fm-demod"})
                        emit(DS_SENSOR, "audio.active", now,
                             1.0 if rms > 0.01 else 0.0, {"source": "rf-fm-demod"})
                if t - last_wav > 60 and len(ring) > AUDIO_FS:
                    last_wav = t
                    try:
                        with wave.open(str(CAPTURE / "fm_audio_last.wav"), "wb") as w:
                            w.setnchannels(1); w.setsampwidth(2); w.setframerate(AUDIO_FS)
                            w.writeframes(bytes(ring))
                        seg_dir = CAPTURE / "audio_segments"
                        seg_dir.mkdir(exist_ok=True)
                        seg = seg_dir / f"audio_{time.time_ns()}.wav"
                        with wave.open(str(seg), "wb") as w:
                            w.setnchannels(1); w.setsampwidth(2); w.setframerate(AUDIO_FS)
                            w.writeframes(bytes(ring))
                        for old in sorted(seg_dir.glob("audio_*.wav"))[:-12]:
                            old.unlink(missing_ok=True)
                    except Exception as e:
                        log("wav write error", repr(e))
        except Exception as e:
            log("AUDIO reconnect after:", repr(e))
            await asyncio.sleep(5)

# ── 6. Couloir status polling ────────────────────────────────────────────────
async def status_task():
    loop = asyncio.get_event_loop()
    ir_mode_prev = None
    rf_mode_prev = None
    while True:
        await asyncio.sleep(2.0)
        now = datetime.now(timezone.utc)
        try:
            chan = grpc.insecure_channel(COULOIR)
            ir_stub = squall_ir_pb2_grpc.SquallIrControlStub(chan)
            st = await loop.run_in_executor(
                None, lambda: ir_stub.GetStatus(squall_ir_pb2.GetStatusRequest(), timeout=3))
            tags = {"sensor": "squall-ir"}
            emit(DS_SENSOR, "squall.ir.running", now, 1.0 if st.running else 0.0, tags)
            ir = st.ir_status
            mode = squall_ir_pb2.IrMode.Name(ir.current_mode)
            emit(DS_SENSOR, "squall.ir.mode", now, mode, tags)
            emit(DS_SENSOR, "squall.ir.fps_configured", now, float(ir.fps), tags)
            los = ir.los_status
            if los.los_valid:
                emit(DS_SENSOR, "squall.ir.los.azimuth_deg", now,
                     los.pointing_azimuth_rad * RAD2DEG, tags)
                emit(DS_SENSOR, "squall.ir.los.elevation_deg", now,
                     los.pointing_elevation_rad * RAD2DEG, tags)
            if los.platform_attitude_valid:
                emit(DS_SENSOR, "squall.ir.platform.yaw_deg", now,
                     los.platform_yaw_rad * RAD2DEG, tags)
                emit(DS_SENSOR, "squall.ir.platform.pitch_deg", now,
                     los.platform_pitch_rad * RAD2DEG, tags)
                emit(DS_SENSOR, "squall.ir.platform.roll_deg", now,
                     los.platform_roll_rad * RAD2DEG, tags)
            if ir_mode_prev and ir_mode_prev != mode:
                await emit_event(f"Squall IR mode: {ir_mode_prev} -> {mode}",
                                 properties={"sensor": "squall-ir"})
            ir_mode_prev = mode
        except Exception:
            pass
        try:
            chan = grpc.insecure_channel(COULOIR)
            rf_stub = squall_rf_pb2_grpc.SquallRfControlStub(chan)
            st = await loop.run_in_executor(
                None, lambda: rf_stub.GetStatus(squall_rf_pb2.GetStatusRequest(), timeout=3))
            tags = {"sensor": "squall-rf"}
            emit(DS_SENSOR, "squall.rf.running", now, 1.0 if st.running else 0.0, tags)
            rf = st.rf_status
            mode = squall_rf_pb2.RfMode.Name(rf.current_mode)
            emit(DS_SENSOR, "squall.rf.mode", now, mode, tags)
            emit(DS_SENSOR, "squall.rf.center_frequency_hz", now,
                 float(rf.center_frequency_hz), tags)
            emit(DS_SENSOR, "squall.rf.sample_rate_sps", now, float(rf.sample_rate), tags)
            if rf_mode_prev and rf_mode_prev != mode:
                await emit_event(f"Squall RF mode: {rf_mode_prev} -> {mode}",
                                 properties={"sensor": "squall-rf"})
            rf_mode_prev = mode
        except Exception:
            pass

# ── 7. Prometheus scraping ───────────────────────────────────────────────────
PROM_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([0-9eE+.\-naif]+)")
LABEL = re.compile(r'(\w+)="([^"]*)"')

def scrape(service, url):
    out = []
    try:
        r = requests.get(url, timeout=3)
        if r.status_code != 200:
            return out
        for line in r.text.splitlines():
            if line.startswith("#"):
                continue
            m = PROM_LINE.match(line)
            if not m:
                continue
            name, labels_s, val = m.groups()
            if not name.startswith(METRIC_PREFIXES) or name.endswith("_bucket"):
                continue
            try:
                v = float(val)
            except ValueError:
                continue
            if math.isnan(v) or math.isinf(v):
                continue
            tags = {"service": service}
            if labels_s:
                for lk, lv in LABEL.findall(labels_s)[:3]:
                    tags[lk] = lv
            out.append((f"obs.{name}", v, tags))
    except Exception:
        pass
    return out

async def metrics_task():
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(5.0)
        now = datetime.now(timezone.utc)
        for service, url in METRIC_ENDPOINTS.items():
            rows = await loop.run_in_executor(None, scrape, service, url)
            emit(DS_OBS, "obs.scrape_up", now, 1.0 if rows else 0.0, {"service": service})
            for ch, v, tags in rows[:300]:
                emit(DS_OBS, ch, now, v, tags)

# ── main ──────────────────────────────────────────────────────────────────────
async def main():
    global DIS_LOG, UCI_LOG, WF_LOG
    CAPTURE.mkdir(exist_ok=True)
    DIS_LOG = RotatingBin("dis_raw", "dis", max_bytes=50 << 20, keep=4)
    UCI_LOG = RotatingBin("uci_raw", "uci", max_bytes=100 << 20, keep=4)
    WF_LOG = RotatingBin("waterfall", "wf", max_bytes=100 << 20, keep=2)
    log("nominal-bridge starting; asset", ASSET_RID)
    await emit_event("Nominal bridge capture started",
                     description="nominal-bridge connected to AMS GRA Hello World SK "
                                 "(DIS, OWP/UCI, IR MEL tap, RF MEL tap, FM audio, metrics)")
    tasks = [
        dis_task(), owp_task(), uci_gap_watch(), ir_tap_task(), rf_tap_task(),
        audio_task(), status_task(), metrics_task(),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    log("bridge exiting:", results)

if __name__ == "__main__":
    asyncio.run(main())
