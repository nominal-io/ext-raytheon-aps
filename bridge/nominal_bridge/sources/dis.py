"""DIS (IEEE 1278.1) EntityState PDUs from UDP multicast/broadcast/unicast, or a
recorded capture. One set of channels per entity, told apart by a tag."""
from __future__ import annotations

import asyncio
import math
import socket
import struct
import time
from pathlib import Path

from ..core import Source, register_source
from ..util import RotatingLog, ecef_to_lla, now_ns

ENTITY_STATE = 1
_REC = struct.Struct(">QH")          # capture record header: rx time (ns), PDU length


def parse_entity_state(pdu: bytes) -> dict | None:
    """Decode the fixed part of an EntityState PDU (DIS 6 and 7 share this layout)."""
    if len(pdu) < 144 or pdu[2] != ENTITY_STATE:
        return None
    site, app, eid = struct.unpack_from(">HHH", pdu, 12)
    kind, domain, country, category, subcat, specific, extra = struct.unpack_from(">BBHBBBB", pdu, 20)
    vx, vy, vz = struct.unpack_from(">fff", pdu, 36)
    x, y, z = struct.unpack_from(">ddd", pdu, 48)
    psi, theta, phi = struct.unpack_from(">fff", pdu, 72)
    marking = pdu[129:140].split(b"\x00")[0].decode("ascii", "ignore").strip()
    return {
        "exercise": pdu[1], "site": site, "application": app, "entity_id": eid,
        "force_id": pdu[18],
        "entity_type": f"{kind}.{domain}.{country}.{category}.{subcat}.{specific}.{extra}",
        "velocity": (vx, vy, vz), "ecef": (x, y, z), "orientation": (psi, theta, phi),
        "marking": marking,
    }


def open_udp(port: int, group: str | None, interfaces: list[str], rcvbuf: int = 4 << 20):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    except OSError:
        pass
    sock.bind(("0.0.0.0", port))
    joined = []
    if group:
        for iface in interfaces:
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                socket.inet_aton(group) + socket.inet_aton(iface))
                joined.append(iface)
            except OSError:
                pass
    sock.setblocking(False)
    return sock, joined


@register_source("dis_entity_state")
class DisEntityStateSource(Source):
    DEFAULTS = {
        "stream": "dis",
        "group": "239.1.2.3",            # multicast group; null for broadcast / unicast
        "port": 21100,
        "interfaces": ["127.0.0.1", "0.0.0.0"],   # local addresses to join the group on
        "exercise_id": None,             # only accept this DIS exercise (null = any)
        "channel_prefix": "dis",
        "entity_tag": "entity",          # tag key that separates entities
        "entity_name": "marking",        # marking | id   (id = site.app.entity)
        "max_entities": 200,             # ignore new entities beyond this (protects cardinality)
        "min_interval_s": 0.0,           # per-entity decimation; 0 = every PDU
        "stale_after_s": 10.0,           # entity counts as gone after this long
        "entity_events": True,           # "DIS entity appeared" events
        "entity_event_limit": 25,        # after this many, stop raising appeared events
        "record": True,                  # raw PDUs to <capture_dir>/dis_raw
        "replay": None,
        "replay_speed": 0,
        "replay_timestamps": "original",
    }

    @classmethod
    def validate(cls, cfg):
        problems = super().validate(cfg)
        if cfg.get("entity_name", "marking") not in ("marking", "id"):
            problems.append("entity_name must be 'marking' or 'id'")
        if cfg.get("replay") and not Path(cfg["replay"]).is_file():
            problems.append(f"replay file not found: {cfg['replay']}")
        return problems

    def __init__(self, name, cfg, ctx):
        super().__init__(name, cfg, ctx)
        self.last_seen: dict[str, float] = {}
        self.last_emit: dict[str, float] = {}
        self.n_pdus = self.n_other = self.n_ignored = 0
        self._events = 0
        self._last_counts = 0.0
        self._log = None

    def handle(self, pdu: bytes, ts_ns: int, mono: float | None = None) -> None:
        mono = time.monotonic() if mono is None else mono
        es = parse_entity_state(pdu)
        if es is None:
            self.n_other += 1
            return
        if self.cfg["exercise_id"] is not None and es["exercise"] != self.cfg["exercise_id"]:
            return
        self.n_pdus += 1
        ident = f"{es['site']}.{es['application']}.{es['entity_id']}"
        ent = ident if self.cfg["entity_name"] == "id" else (es["marking"] or f"entity-{ident}")
        if ent not in self.last_seen:
            if len(self.last_seen) >= int(self.cfg["max_entities"]):
                self.n_ignored += 1
                if self.n_ignored == 1:
                    self.ctx.log(f"more than max_entities={self.cfg['max_entities']} entities; "
                                 "ignoring new ones (raise max_entities to keep them)")
                return
            if self.cfg["entity_events"] and self._events < int(self.cfg["entity_event_limit"]):
                self._events += 1
                self.ctx.event(f"DIS entity appeared: {ent}", ts_ns=ts_ns, key=f"dis:{ent}",
                               properties={"entity": ent, "entity_id": ident,
                                           "force_id": es["force_id"],
                                           "entity_type": es["entity_type"]})
        self.last_seen[ent] = mono
        self._counts(ts_ns, mono)
        gap = float(self.cfg["min_interval_s"])
        if gap > 0 and mono - self.last_emit.get(ent, -1e9) < gap:
            return
        self.last_emit[ent] = mono

        p = self.cfg["channel_prefix"]
        tags = {self.cfg["entity_tag"]: ent}
        lat, lon, alt = ecef_to_lla(*es["ecef"])
        vx, vy, vz = es["velocity"]
        psi, theta, phi = es["orientation"]
        e = self.ctx.emit
        e(f"{p}.position.latitude_deg", lat, ts_ns=ts_ns, tags=tags)
        e(f"{p}.position.longitude_deg", lon, ts_ns=ts_ns, tags=tags)
        e(f"{p}.position.altitude_m", alt, ts_ns=ts_ns, tags=tags)
        e(f"{p}.velocity.speed_mps", math.sqrt(vx * vx + vy * vy + vz * vz), ts_ns=ts_ns, tags=tags)
        e(f"{p}.velocity.ecef_x_mps", vx, ts_ns=ts_ns, tags=tags)
        e(f"{p}.velocity.ecef_y_mps", vy, ts_ns=ts_ns, tags=tags)
        e(f"{p}.velocity.ecef_z_mps", vz, ts_ns=ts_ns, tags=tags)
        e(f"{p}.orientation.psi_rad", psi, ts_ns=ts_ns, tags=tags)
        e(f"{p}.orientation.theta_rad", theta, ts_ns=ts_ns, tags=tags)
        e(f"{p}.orientation.phi_rad", phi, ts_ns=ts_ns, tags=tags)
        e(f"{p}.force_id", float(es["force_id"]), ts_ns=ts_ns, tags=tags)

    def _counts(self, ts_ns: int, mono: float, force: bool = False) -> None:
        if not force and mono - self._last_counts < 2.0:
            return
        self._last_counts = mono
        p = self.cfg["channel_prefix"]
        stale = float(self.cfg["stale_after_s"])
        active = sum(1 for t in self.last_seen.values() if mono - t < stale)
        self.ctx.emit(f"{p}.entities_active", float(active), ts_ns=ts_ns)
        self.ctx.emit(f"{p}.pdu_total", float(self.n_pdus), ts_ns=ts_ns)
        self.ctx.emit(f"{p}.pdu_other_total", float(self.n_other), ts_ns=ts_ns)
        self.ctx.emit(f"{p}.entities_ignored_total", float(self.n_ignored), ts_ns=ts_ns)

    async def run(self) -> None:
        if self.cfg["replay"]:
            await self._replay(Path(self.cfg["replay"]))
            return
        sock, joined = open_udp(int(self.cfg["port"]), self.cfg["group"],
                                list(self.cfg["interfaces"] or []))
        where = f"{self.cfg['group']}:{self.cfg['port']} via {joined}" if self.cfg["group"] \
            else f"udp/{self.cfg['port']}"
        self.ctx.log(f"listening on {where}")
        if self.cfg["record"]:
            self._log = RotatingLog(self.ctx.capture_dir / "dis_raw", "dis",
                                    max_bytes=50 << 20, keep=4)
        loop = asyncio.get_running_loop()
        try:
            while True:
                data = await loop.sock_recv(sock, 4096)
                ts = now_ns()
                if self._log and len(data) >= 12:
                    self._log.write(_REC.pack(ts, len(data)) + data)
                self.handle(data, ts)
        finally:
            sock.close()
            if self._log:
                self._log.close()

    async def _replay(self, path: Path) -> None:
        speed = float(self.cfg["replay_speed"] or 0)
        blob = path.read_bytes()
        off = n = 0
        first = wall0 = None
        shift = ts = 0
        while off + _REC.size <= len(blob):
            rx, length = _REC.unpack_from(blob, off)
            off += _REC.size
            pdu = blob[off:off + length]
            off += length
            if first is None:
                first, wall0 = rx, time.monotonic()
                if self.cfg["replay_timestamps"] == "now":
                    shift = now_ns() - rx
            if speed > 0:
                delay = (rx - first) / 1e9 / speed - (time.monotonic() - wall0)
                if delay > 0:
                    await asyncio.sleep(delay)
            ts = rx + shift
            self.handle(pdu, ts, mono=(rx - first) / 1e9)   # recording time drives staleness
            n += 1
            if n % 2000 == 0:
                await asyncio.sleep(0)
        if n:
            self._counts(ts, (ts - shift - first) / 1e9, force=True)
        self.ctx.log(f"replayed {n} PDUs from {path.name}; {len(self.last_seen)} entities")
