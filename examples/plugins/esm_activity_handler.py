"""Example plugin: a typed handler for a UCI message.

Drop a file like this into the plugins directory (mounted at /plugins in the
container). It is imported at start-up; no image rebuild is needed.

Without this file, ESM_Activity still reaches Nominal through the generic flattener
as uci.esm_activity.*. Write a handler when you want specific channel names, units,
tags, or events.
"""
from nominal_bridge import uci_handler


@uci_handler("ESM_Activity")
def esm_activity(msg, ctx):
    activities = msg.data.get("Activity") or []
    ctx.emit("uci.esm.activity_count", len(activities), ts_ns=msg.ts_ns, tags=msg.tags)

    last = msg.state.setdefault("esm_activity_state", {})     # persists between messages
    for act in activities:
        # short, stable identifier so each activity is its own series
        activity = (act.get("ActivityID") or {}).get("UUID", "unknown")[:8]
        state = act.get("ActivityState", "UNKNOWN")
        tags = {**msg.tags, "activity": activity}
        ctx.emit("uci.esm.activity_state", state, ts_ns=msg.ts_ns, tags=tags)
        ctx.emit("uci.esm.interactive", bool(act.get("Interactive")), ts_ns=msg.ts_ns, tags=tags)

        if last.get(activity) not in (None, state):           # event only on a change
            ctx.event(f"ESM activity {activity}: {last[activity]} -> {state}",
                      properties={"activity": activity}, ts_ns=msg.ts_ns)
        last[activity] = state
