"""Couloir (Squall control-plane gRPC gateway): status polling and helpers to
register the bridge as an extra UDP data destination on a sensor pipeline.

Each Squall pipeline has a fixed number of destination slots: one per
`[[sinks]] type = "udp", destinations = []` entry in the Squall config (two in the
stock kit). The bridge takes one per sensor it taps and gives it back on shutdown.
When every slot is taken, Squall hands a newcomer any slot older than 60 s, which
disconnects whoever held it. So provision one slot per consumer, bridge included.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Callable

from ..core import Context, Source, register_source
from ..util import RAD2DEG

_PROTO_DIR = Path(__file__).resolve().parent.parent / "proto"
_mods: dict = {}


def load_proto() -> dict:
    """Import the generated gRPC stubs, generating them on first use if needed
    (the container image generates them at build time)."""
    if _mods:
        return _mods
    if str(_PROTO_DIR) not in sys.path:
        sys.path.insert(0, str(_PROTO_DIR))
    if not (_PROTO_DIR / "squall_rf_pb2.py").exists():
        from grpc_tools import protoc
        rc = protoc.main(["protoc", f"-I{_PROTO_DIR}", f"--python_out={_PROTO_DIR}",
                          f"--grpc_python_out={_PROTO_DIR}", "squall_rf.proto", "squall_ir.proto"])
        if rc != 0:
            raise RuntimeError("could not generate gRPC stubs from proto/")
    import squall_ir_pb2, squall_ir_pb2_grpc, squall_rf_pb2, squall_rf_pb2_grpc
    _mods.update(ir=(squall_ir_pb2, squall_ir_pb2_grpc.SquallIrControlStub),
                 rf=(squall_rf_pb2, squall_rf_pb2_grpc.SquallRfControlStub))
    return _mods


class Couloir:
    """Blocking client; call through `asyncio.to_thread`."""

    def __init__(self, address: str, kind: str):
        import grpc
        if kind not in ("ir", "rf"):
            raise ValueError("couloir kind must be 'ir' or 'rf'")
        self.kind = kind
        self.pb, stub = load_proto()[kind]
        self.stub = stub(grpc.insecure_channel(address))

    def status(self, timeout: float = 3.0):
        return self.stub.GetStatus(self.pb.GetStatusRequest(), timeout=timeout)

    def add_destination(self, udp_destination: str, client_id: str, timeout: float = 5.0):
        return self.stub.AddDataDestination(self.pb.AddDataDestinationRequest(
            udp_destination=udp_destination, client_id=client_id), timeout=timeout)

    def remove_destination(self, client_id: str, timeout: float = 3.0):
        return self.stub.RemoveDataDestination(
            self.pb.RemoveDataDestinationRequest(client_id=client_id), timeout=timeout)


class Tap:
    """Registers `advertise` (host:port the sensor should send to) with Couloir.

    config:  couloir: {address: 127.0.0.1:21203, kind: ir|rf,
                       advertise: 127.0.0.1:21610, client_id: nominal-bridge-ir}
    """

    def __init__(self, ctx: Context, cfg: dict, default_port: int):
        self.ctx = ctx
        self.address = cfg.get("address", "127.0.0.1:21203")
        self.kind = cfg["kind"]
        self.advertise = cfg.get("advertise") or f"127.0.0.1:{default_port}"
        self.client_id = cfg.get("client_id") or f"nominal-bridge-{ctx.name}"
        self.retry_s = float(cfg.get("retry_s", 5))
        self.stall_s = float(cfg.get("stall_s", 30))
        self.client: Couloir | None = None
        self.registered_at = 0.0

    async def register(self) -> None:
        while True:
            try:
                self.client = self.client or Couloir(self.address, self.kind)
                resp = await asyncio.to_thread(self.client.add_destination,
                                               self.advertise, self.client_id)
                if resp.success:
                    self.registered_at = time.monotonic()
                    self.ctx.log(f"registered {self.advertise} as a {self.kind} data "
                                 f"destination (client_id={self.client_id})")
                    return
                self.ctx.log(f"couloir refused the destination: {resp.error_message or '?'} "
                             f"(all slots taken?); retrying in {self.retry_s:g}s")
            except Exception as e:
                self.ctx.log(f"couloir {self.address} not reachable ({type(e).__name__}); "
                             f"retrying in {self.retry_s:g}s")
            await asyncio.sleep(self.retry_s)

    async def status(self):
        try:
            self.client = self.client or Couloir(self.address, self.kind)
            return await asyncio.to_thread(self.client.status)
        except Exception:
            return None

    async def watch(self, last_data: Callable[[], float], interval_s: float = 10.0) -> None:
        """Keep the tap alive. `last_data()` returns the monotonic time of the last
        datagram.

        * The sensor restarted (its uptime is shorter than our registration is old):
          its slots are empty again, so register again.
        * Data stopped but the sensor did not restart: most likely another client was
          given this slot. Say so, and do not take it back, because that would
          disconnect someone else in turn.
        """
        warned = False
        while True:
            await asyncio.sleep(interval_s)
            st = await self.status()
            if st is None:
                continue
            held = time.monotonic() - self.registered_at
            if st.uptime_seconds + interval_s < held:
                self.ctx.log("sensor restarted; registering the data destination again")
                await self.register()
                warned = False
                continue
            quiet = time.monotonic() - max(last_data(), self.registered_at)
            if quiet > self.stall_s and st.running:
                if not warned:
                    warned = True
                    self.ctx.log(
                        f"no data for {quiet:.0f}s although the sensor is running. Another "
                        "client may have been given this destination slot. Squall has one "
                        "slot per `[[sinks]]` entry in its config; add one for the bridge, "
                        "then restart the bridge.")
            else:
                warned = False

    def release(self) -> None:
        if self.client:
            try:
                self.client.remove_destination(self.client_id, timeout=2.0)
                self.ctx.log("released couloir data destination")
            except Exception:
                pass


@register_source("couloir_status")
class CouloirStatusSource(Source):
    DEFAULTS = {
        "stream": "sensor",
        "address": "127.0.0.1:21203",
        "sensors": [{"kind": "ir", "tag": "squall-ir"}, {"kind": "rf", "tag": "squall-rf"}],
        "interval_s": 2.0,
        "channel_prefix": "squall",
        "mode_events": True,
    }

    @classmethod
    def validate(cls, cfg):
        problems = super().validate(cfg)
        for s in cfg.get("sensors") or []:
            if s.get("kind") not in ("ir", "rf"):
                problems.append(f"sensor kind must be 'ir' or 'rf', got {s.get('kind')!r}")
        return problems

    async def run(self) -> None:
        sensors = [(s, Couloir(s.get("address") or self.cfg["address"], s["kind"]))
                   for s in self.cfg["sensors"]]
        prev_mode: dict[str, str] = {}
        down: set[str] = set()
        p = self.cfg["channel_prefix"]
        while True:
            for spec, client in sensors:
                kind = spec["kind"]
                label = spec.get("tag") or f"squall-{kind}"
                tags = {"sensor": label}
                pre = f"{p}.{spec.get('channel', kind)}"
                e = lambda ch, v: self.ctx.emit(f"{pre}.{ch}", v, tags=tags)  # noqa: E731
                try:
                    st = await asyncio.to_thread(client.status)
                except Exception as ex:
                    e("reachable", 0.0)
                    if label not in down:
                        down.add(label)
                        self.ctx.log(f"{label}: status unavailable ({type(ex).__name__})")
                    continue
                if label in down:
                    down.discard(label)
                    self.ctx.log(f"{label}: status available again")
                e("reachable", 1.0)
                e("running", st.running)
                e("ready", st.ready)
                e("uptime_s", float(st.uptime_seconds))
                if kind == "ir":
                    ir = st.ir_status
                    mode = client.pb.IrMode.Name(ir.current_mode)
                    e("mode", mode)
                    e("fps_configured", float(ir.fps))
                    e("width_px", float(ir.width))
                    e("height_px", float(ir.height))
                    los = ir.los_status
                    if los.los_valid:
                        e("los.azimuth_deg", los.pointing_azimuth_rad * RAD2DEG)
                        e("los.elevation_deg", los.pointing_elevation_rad * RAD2DEG)
                    if los.platform_attitude_valid:
                        e("platform.yaw_deg", los.platform_yaw_rad * RAD2DEG)
                        e("platform.pitch_deg", los.platform_pitch_rad * RAD2DEG)
                        e("platform.roll_deg", los.platform_roll_rad * RAD2DEG)
                else:
                    rf = st.rf_status
                    mode = client.pb.RfMode.Name(rf.current_mode)
                    e("mode", mode)
                    e("center_frequency_hz", float(rf.center_frequency_hz))
                    e("sample_rate_sps", float(rf.sample_rate))
                    e("block_size", float(rf.block_size))
                    if rf.source_type:
                        e("source_type", rf.source_type)
                    if rf.rtlsdr_tuner:
                        e("rtlsdr_tuner", rf.rtlsdr_tuner)
                if self.cfg["mode_events"] and prev_mode.get(label) not in (None, mode):
                    self.ctx.event(f"{label} mode: {prev_mode[label]} -> {mode}",
                                   properties={"sensor": label}, key=f"mode:{label}:{mode}")
                prev_mode[label] = mode
            await asyncio.sleep(float(self.cfg["interval_s"]))
