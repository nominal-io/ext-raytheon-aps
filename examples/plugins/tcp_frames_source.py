"""Example plugin: a new source type built on an existing one.

`tcp_frames` reads video frames from a TCP server that sends each frame as a 4-byte
big-endian length followed by the pixels. It inherits statistics, recording and
snapshots from FrameSource, so the only code here is the transport.

Use it from a config like any built-in type:

    - name: tower-eo
      type: tcp_frames
      stream: sensor
      host: 10.0.0.20
      port: 5600
      width: 640
      height: 512
      pixel_format: mono16le
      channel_prefix: eo
      tags: {sensor: tower-eo}
"""
import asyncio
import struct

from nominal_bridge import register_source
from nominal_bridge.sources.frames import FrameSource
from nominal_bridge.util import now_ns


@register_source("tcp_frames")
class TcpFrameSource(FrameSource):
    DEFAULTS = {"host": "127.0.0.1", "port": None, "max_frame_bytes": 64 << 20}
    REQUIRED = ("port",)

    async def frames(self):
        reader, writer = await asyncio.open_connection(self.cfg["host"], int(self.cfg["port"]))
        self.ctx.log(f"connected to {self.cfg['host']}:{self.cfg['port']}")
        try:
            while True:
                (length,) = struct.unpack(">I", await reader.readexactly(4))
                if length > int(self.cfg["max_frame_bytes"]):
                    raise ValueError(f"frame length {length} is not plausible; out of sync")
                yield now_ns(), await reader.readexactly(length)
        finally:
            writer.close()
