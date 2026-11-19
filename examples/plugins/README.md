# Plugins

Any `*.py` file in the plugins directory is imported when the bridge starts. A plugin
registers itself with a decorator, so there is nothing else to wire up.

| File | Shows |
|---|---|
| `esm_activity_handler.py` | A typed handler for one UCI message (`@uci_handler`) |
| `tcp_frames_source.py` | A new source type that reuses the built-in frame processing (`@register_source`) |

Check that a plugin loaded:

    nominal-bridge types --plugins examples/plugins
