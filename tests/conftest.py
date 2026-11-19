import asyncio
import struct
from pathlib import Path

import pytest

from nominal_bridge import runner
from nominal_bridge.core import MemorySink

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parent.parent


@pytest.fixture
def fixtures():
    return FIXTURES


@pytest.fixture
def run_sources(tmp_path):
    """Run a list of source configs to completion against an in-memory sink."""
    def _run(sources, *, duration_s=None, plugins=(), events=None):
        sink = MemorySink()
        cfg = {"sources": sources, "plugins": list(plugins), "capture_dir": str(tmp_path),
               "events": events or {"min_interval_s": 0, "max_per_minute": 10000}}
        hub = asyncio.run(runner.run(cfg, sinks=[sink], duration_s=duration_s))
        return sink, hub
    return _run


def entity_state_pdu(marking: str, entity_id: int, ecef, *, force=1, velocity=(0, 0, 0),
                     exercise=1) -> bytes:
    """Build a minimal 144-byte DIS EntityState PDU."""
    pdu = bytearray(144)
    pdu[0], pdu[1], pdu[2], pdu[3] = 7, exercise, 1, 1
    struct.pack_into(">H", pdu, 8, 144)
    struct.pack_into(">HHH", pdu, 12, 1, 1, entity_id)
    pdu[18] = force
    struct.pack_into(">BBHBBBB", pdu, 20, 1, 2, 225, 1, 3, 0, 0)
    struct.pack_into(">fff", pdu, 36, *velocity)
    struct.pack_into(">ddd", pdu, 48, *ecef)
    struct.pack_into(">fff", pdu, 72, 0.1, 0.2, 0.3)
    name = marking.encode()[:11]
    pdu[128] = 1
    pdu[129:129 + len(name)] = name
    return bytes(pdu)
