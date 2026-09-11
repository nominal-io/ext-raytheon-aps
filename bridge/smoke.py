#!/usr/bin/env python3
"""Smoke-test every local tap without touching Nominal."""
import asyncio, math, socket, struct, time
import numpy as np
import grpc
import squall_ir_pb2, squall_ir_pb2_grpc
import squall_rf_pb2, squall_rf_pb2_grpc

def ecef_to_lla(x, y, z):
    a, f = 6378137.0, 1 / 298.257223563
    b = a * (1 - f); e2 = f * (2 - f); ep2 = (a*a - b*b) / (b*b)
    p = math.hypot(x, y)
    theta = math.atan2(z * a, p * b)
    lat = math.atan2(z + ep2 * b * math.sin(theta)**3, p - e2 * a * math.cos(theta)**3)
    lon = math.atan2(y, x)
    n = a / math.sqrt(1 - e2 * math.sin(lat)**2)
    return math.degrees(lat), math.degrees(lon), p / math.cos(lat) - n

async def dis_test():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try: sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except OSError: pass
    sock.bind(("0.0.0.0", 21100))
    for iface in ("127.0.0.1", "0.0.0.0"):
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            socket.inet_aton("239.1.2.3") + socket.inet_aton(iface))
        except OSError: pass
    sock.setblocking(False)
    loop = asyncio.get_event_loop()
    got = {}
    end = time.time() + 6
    while time.time() < end and len(got) < 4:
        try:
            data = await asyncio.wait_for(loop.sock_recv(sock, 2048), timeout=2)
        except asyncio.TimeoutError:
            continue
        if len(data) < 144 or data[2] != 1: continue
        marking = data[129:140].split(b"\x00")[0].decode("ascii","ignore").strip()
        x, y, z = struct.unpack_from(">ddd", data, 48)
        got[marking] = ecef_to_lla(x, y, z)
    print("DIS entities:", {k: tuple(round(v,4) for v in vv) for k, vv in got.items()})
    return bool(got)

async def tap_test(kind, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))
    sock.setblocking(False)
    chan = grpc.insecure_channel("127.0.0.1:21203")
    if kind == "ir":
        stub = squall_ir_pb2_grpc.SquallIrControlStub(chan)
        st = stub.GetStatus(squall_ir_pb2.GetStatusRequest(), timeout=5)
        print("IR status: running=", st.running, "mode=", squall_ir_pb2.IrMode.Name(st.ir_status.current_mode),
              "res=", st.ir_status.width, "x", st.ir_status.height, "fps=", st.ir_status.fps)
        resp = stub.AddDataDestination(squall_ir_pb2.AddDataDestinationRequest(
            udp_destination=f"127.0.0.1:{port}", client_id="nominal-smoke-ir"), timeout=5)
    else:
        stub = squall_rf_pb2_grpc.SquallRfControlStub(chan)
        st = stub.GetStatus(squall_rf_pb2.GetStatusRequest(), timeout=5)
        print("RF status: running=", st.running, "mode=", squall_rf_pb2.RfMode.Name(st.rf_status.current_mode),
              "fc=", st.rf_status.center_frequency_hz, "fs=", st.rf_status.sample_rate)
        resp = stub.AddDataDestination(squall_rf_pb2.AddDataDestinationRequest(
            udp_destination=f"127.0.0.1:{port}", client_id="nominal-smoke-rf"), timeout=5)
    print(f"{kind} AddDataDestination: success={resp.success} {resp.error_message}")
    loop = asyncio.get_event_loop()
    sizes, t0 = [], time.time()
    while time.time() - t0 < 5 and len(sizes) < 40:
        try:
            data = await asyncio.wait_for(loop.sock_recv(sock, 1 << 20), timeout=2)
            sizes.append(len(data))
        except asyncio.TimeoutError:
            break
    print(f"{kind} tap: {len(sizes)} datagrams, sizes {sorted(set(sizes))[:5]}")
    if kind == "rf" and sizes:
        pass
    # cleanup: remove our smoke destination so the real bridge can claim the slot
    if kind == "ir":
        stub.RemoveDataDestination(squall_ir_pb2.RemoveDataDestinationRequest(
            client_id="nominal-smoke-ir"), timeout=5)
    else:
        stub.RemoveDataDestination(squall_rf_pb2.RemoveDataDestinationRequest(
            client_id="nominal-smoke-rf"), timeout=5)
    return bool(sizes)

async def audio_test():
    try:
        reader, _ = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", 21602), timeout=5)
        data = await asyncio.wait_for(reader.read(65536), timeout=5)
        a = np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2").astype(np.float32) / 32768
        rms = float(np.sqrt(np.mean(a**2))) if len(a) else 0.0
        print(f"AUDIO: {len(data)} bytes, rms={rms:.4f} ({20*math.log10(rms+1e-9):.1f} dBFS)")
        return True
    except Exception as e:
        print("AUDIO failed:", repr(e))
        return False

async def main():
    r1 = await dis_test()
    r2 = await tap_test("ir", 21620)
    r3 = await tap_test("rf", 21621)
    r4 = await audio_test()
    print("RESULT dis=%s ir=%s rf=%s audio=%s" % (r1, r2, r3, r4))

asyncio.run(main())
