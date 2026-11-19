"""nominal-bridge: stream mission-system and sensor data into Nominal.

Everything is assembled from a YAML config: *sources* read a wire format (DIS,
UCI over OWP, raw frames, IQ, PCM, Prometheus, ...) and emit points and events;
*sinks* deliver them (Nominal, console, JSONL). New source types and UCI message
handlers are plain Python files dropped into a plugin directory.
"""
from .core import (Context, Event, Point, Sink, Source, register_sink,
                   register_source, SOURCE_TYPES, SINK_TYPES)
from .sources.uci_handlers import uci_handler, UCI_HANDLERS

__all__ = ["Context", "Event", "Point", "Sink", "Source", "register_sink",
           "register_source", "uci_handler", "SOURCE_TYPES", "SINK_TYPES",
           "UCI_HANDLERS"]
__version__ = "2.0.0"
