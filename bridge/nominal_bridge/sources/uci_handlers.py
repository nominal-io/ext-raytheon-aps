"""UCI message handlers.

A handler turns one decoded UCI message into channels and events:

    from nominal_bridge import uci_handler

    @uci_handler("ESM_Activity")
    def esm_activity(msg, ctx):
        for act in msg.data.get("Activity", []):
            ctx.emit("uci.esm.activity_state", act["ActivityState"], ts_ns=msg.ts_ns,
                     tags={**msg.tags, "activity": act["ActivityID"]["UUID"][:8]})

Messages with no registered handler go through the generic flattener, so every topic
you subscribe to produces channels without writing any code. Register a handler only
when you want specific channel names, units, or events.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..core import Context
from ..flatten import flatten
from ..util import RAD2DEG, iso_duration_s

UCI_HANDLERS: dict[str, Callable[["UciMessage", Context], None]] = {}


@dataclass(slots=True)
class UciMessage:
    name: str                       # e.g. "PositionReport"
    body: dict                      # the whole message object
    ts_ns: int                      # MessageHeader.Timestamp (receive time if absent)
    tags: dict[str, str]            # system / service labels from the header
    raw: str                        # original JSON text
    state: dict[str, Any]           # scratch space that persists per source
    options: dict[str, Any] = field(default_factory=dict)   # the source's config

    @property
    def header(self) -> dict:
        return self.body.get("MessageHeader") or {}

    @property
    def data(self) -> dict:
        return self.body.get("MessageData") or {}


def uci_handler(*message_names: str):
    """Decorator: register a function as the handler for one or more UCI messages."""
    def deco(fn):
        for n in message_names:
            UCI_HANDLERS[n] = fn
        return fn
    return deco


def header_tags(body: dict) -> dict[str, str]:
    h = body.get("MessageHeader") or {}
    tags = {}
    for key, tag in (("SystemID", "system"), ("ServiceID", "service")):
        label = (h.get(key) or {}).get("DescriptiveLabel")
        if label:
            tags[tag] = label
    return tags


def generic(msg: UciMessage, ctx: Context) -> None:
    """Fallback: flatten MessageData into `uci.<message>.<path>` channels."""
    opts = msg.options.get("generic") or {}
    rows = flatten(msg.data, f"uci.{msg.name.lower()}",
                   max_fields=int(opts.get("max_fields", 80)),
                   max_list_items=int(opts.get("max_list_items", 16)),
                   keep_ids=bool(opts.get("keep_ids", False)))
    for channel, value, tags in rows:
        ctx.emit(channel, value, ts_ns=msg.ts_ns, tags={**msg.tags, **tags})


def _transition(msg: UciMessage, ctx: Context, kind: str, who: str, state: str,
                normal: str | None, props: dict) -> None:
    seen = msg.state.setdefault(f"{kind}_state", {})
    prev = seen.get(who)
    if prev is not None and prev != state:
        label = f"{kind} {who}" if who else kind
        ctx.event(f"{label}: {prev} -> {state}",
                  type="FLAG" if normal and state != normal else "INFO",
                  properties={**props, "from": prev, "to": state}, ts_ns=msg.ts_ns,
                  key=f"{kind}:{who}:{state}")
    seen[who] = state


# ── typed handlers for the messages the Hello World SK publishes ─────────────
@uci_handler("PositionReport")
def position_report(msg: UciMessage, ctx: Context) -> None:
    d = msg.data
    t = {**msg.tags, "entity": d.get("DisplayName") or "unknown"}
    ist = d.get("InertialState") or {}
    pos = ist.get("Position") or {}
    if "Latitude" in pos and "Longitude" in pos:     # UCI angles are radians
        ctx.emit("uci.position.latitude_deg", pos["Latitude"] * RAD2DEG, ts_ns=msg.ts_ns, tags=t)
        ctx.emit("uci.position.longitude_deg", pos["Longitude"] * RAD2DEG, ts_ns=msg.ts_ns, tags=t)
        ctx.emit("uci.position.altitude_m", float(pos.get("Altitude", 0)), ts_ns=msg.ts_ns, tags=t)
    for k, v in (ist.get("DomainVelocity") or {}).items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            ctx.emit(f"uci.velocity.{k.lower()}", float(v), ts_ns=msg.ts_ns, tags=t)


@uci_handler("ObservationMeasurementReport")
def observation_measurement_report(msg: UciMessage, ctx: Context) -> None:
    n = 0
    for om in msg.data.get("ObservationMeasurement") or []:
        for m in om.get("Measurements") or []:
            los = ((((m.get("MeasurementKinematics") or {}).get("LOS_AzEl") or {})
                    .get("Measurement") or {}).get("LineOfSight") or {})
            tags = {**msg.tags, "measurement": str(n)} if n else msg.tags
            n += 1
            if "Azimuth" in los and "Elevation" in los:
                ctx.emit("uci.ir_observation.azimuth_deg", los["Azimuth"] * RAD2DEG,
                         ts_ns=msg.ts_ns, tags=tags)
                ctx.emit("uci.ir_observation.elevation_deg", los["Elevation"] * RAD2DEG,
                         ts_ns=msg.ts_ns, tags=tags)
    ctx.emit("uci.ir_observation.measurements_per_report", float(n),
             ts_ns=msg.ts_ns, tags=msg.tags)
    if n:
        _presence(msg, ctx)


@uci_handler("SignalReport")
def signal_report(msg: UciMessage, ctx: Context) -> None:
    signals = msg.data.get("Signal") or []
    ctx.emit("uci.signal.count", float(len(signals)), ts_ns=msg.ts_ns, tags=msg.tags)
    for i, sig in enumerate(signals):
        # first signal keeps the untagged series; extra signals are told apart by tag
        t = {**msg.tags, "signal": str(i)} if i else msg.tags
        freq = ((sig.get("Parametrics") or {}).get("Frequency") or {}).get("FrequencyAverage")
        if freq is not None:
            ctx.emit("uci.signal.frequency_hz", float(freq), ts_ns=msg.ts_ns, tags=t)
        if sig.get("SignalState"):
            ctx.emit("uci.signal.state", str(sig["SignalState"]), ts_ns=msg.ts_ns, tags=t)
        ck = sig.get("CollectorKinematics") or []
        if ck:
            fp = (((ck[0].get("PlatformKinematics") or {}).get("Position") or {})
                  .get("FixedPoint") or {})
            if "Latitude" in fp and "Longitude" in fp:
                ctx.emit("uci.signal.collector_latitude_deg", fp["Latitude"] * RAD2DEG,
                         ts_ns=msg.ts_ns, tags=t)
                ctx.emit("uci.signal.collector_longitude_deg", fp["Longitude"] * RAD2DEG,
                         ts_ns=msg.ts_ns, tags=t)
    if signals:
        _presence(msg, ctx)


@uci_handler("ServiceStatus")
def service_status(msg: UciMessage, ctx: Context) -> None:
    d = msg.data
    svc = (d.get("ServiceID") or {}).get("DescriptiveLabel") or "unknown"
    state = d.get("ServiceState") or "UNKNOWN"
    t = {**msg.tags, "service": svc}
    ctx.emit("uci.service_status.state", state, ts_ns=msg.ts_ns, tags=t)
    up = iso_duration_s(d.get("TimeUp") or "")
    if up is not None:
        ctx.emit("uci.service_status.time_up_s", up, ts_ns=msg.ts_ns, tags=t)
    _transition(msg, ctx, "Service", svc, state, "NORMAL", {"service": svc})


@uci_handler("SubsystemStatus")
def subsystem_status(msg: UciMessage, ctx: Context) -> None:
    d = msg.data
    sub = (d.get("SubsystemID") or {}).get("DescriptiveLabel") or "unknown"
    state = d.get("SubsystemState") or "UNKNOWN"
    ctx.emit("uci.subsystem_status.state", state, ts_ns=msg.ts_ns,
             tags={**msg.tags, "subsystem": sub})
    _transition(msg, ctx, "Subsystem", sub, state, None, {"subsystem": sub})


@uci_handler("SystemStatus")
def system_status(msg: UciMessage, ctx: Context) -> None:
    state = msg.data.get("SystemState") or "UNKNOWN"
    ctx.emit("uci.system_status.state", state, ts_ns=msg.ts_ns, tags=msg.tags)
    _transition(msg, ctx, "System", msg.tags.get("system", ""), state, "OPERATIONAL", {})


@uci_handler("NavigationReport")
def navigation_report(msg: UciMessage, ctx: Context) -> None:
    d = msg.data
    for key in ("Source", "ContingencyLevel"):
        if key in d:
            ctx.emit(f"uci.navigation.{key.lower()}", str(d[key]), ts_ns=msg.ts_ns, tags=msg.tags)
    src = (d.get("Navigation") or {}).get("NavigationSource")
    if src:
        ctx.emit("uci.navigation.navigation_source", str(src), ts_ns=msg.ts_ns, tags=msg.tags)


def _presence(msg: UciMessage, ctx: Context) -> None:
    """Config-driven "<label> acquired / lost" events, e.g.
        presence: {SignalReport: {label: "FM signal", timeout_s: 10}}"""
    spec = (msg.options.get("presence") or {}).get(msg.name)
    if spec:
        ctx.presence(msg.name, spec.get("label", msg.name), float(spec.get("timeout_s", 5)),
                     ts_ns=msg.ts_ns, properties=spec.get("properties") or {})
