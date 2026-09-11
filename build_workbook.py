#!/usr/bin/env python3
"""Build the AMS GRA Hello World SK review workbook in Nominal.

Authors frontend-shaped JSON and decodes with ConjureDecoder (per skill recipe).
"""
from __future__ import annotations

import json
import sys
import uuid

from conjure_python_client import ConjureDecoder, ConjureEncoder
from nominal.core.client import NominalClient
from nominal_api import scout_notebook_api

import os
from pathlib import Path

def _load_env():
    p = Path(__file__).parent / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

_load_env()

PROFILE = os.environ.get("NOMINAL_PROFILE", "raytheon_aps_prod")

MISSION = os.environ["NOMINAL_ASSET_RID"]
EAGLE = os.environ["NOMINAL_ASSET_EAGLE"]
BANDIT1 = os.environ["NOMINAL_ASSET_BANDIT1"]
BANDIT2 = os.environ["NOMINAL_ASSET_BANDIT2"]
RUN_RID = os.environ["NOMINAL_RUN_RID"]
WORKSPACE = os.environ["NOMINAL_WORKSPACE_RID"]

BLUE, ORANGE, RED = "#4C79A8", "#F28E2B", "#E45756"
TEAL, GREEN, YELLOW, PURPLE = "#76B7B2", "#59A14F", "#EDC948", "#B07AA1"

U = lambda: str(uuid.uuid4())

# ── channel variables ────────────────────────────────────────────────────────
CHANNEL_VARS = {}

def _spec_v1(kind, channel, input_rid, ref):
    fn = "numericSeries" if kind == "numeric" else "enumSeries"
    typ = "numericSeriesType" if kind == "numeric" else "enumSeriesType"
    return json.dumps({"type": "fn", "fn": {
        "functionId": fn, "functionUuid": U(),
        "args": {"input": {"type": typ, "provider": {"type": "channel", "channel": {
            "kind": "CHANNEL", "channelName": channel,
            "inputId": input_rid, "dataSourceRefName": ref}}}}}})

def _spec_v2(kind, channel, input_rid, ref):
    select = "selectNumeric" if kind == "numeric" else "selectEnum"
    return {"computeNode": {"type": kind if kind == "numeric" else "enum",
        (kind if kind == "numeric" else "enum"): {
        "type": "selectTags", "selectTags": {
            "input": {"type": select, select: {
                "name": {"type": "literal", "literal": channel},
                "dataset": {"type": "filter", "filter": {
                    "input": {"type": "reference", "reference": {
                        "name": {"type": "literal", "literal": input_rid}}},
                    "predicate": {"type": "in", "in": {
                        "key": {"type": "literal", "literal": "DATA_SCOPE"},
                        "values": {"type": "literal", "literal": [
                            {"type": "literal", "literal": ref}]}}}}},
                "sampling": None, "storage": None}},
            "tagKeys": []}}},
        "context": {"variables": {}}, "computeExpression": None}

def var(name, channel, input_rid, ref, kind="numeric", display=None):
    CHANNEL_VARS[name] = {
        "displayName": display or name,
        "variableName": name,
        "computeSpec": {"type": "v1", "v1": _spec_v1(kind, channel, input_rid, ref)},
        "computeSpecV2": _spec_v2(kind, channel, input_rid, ref),
    }
    return name

# DIS truth per entity
for slug, rid in [("eagle", EAGLE), ("b1", BANDIT1), ("b2", BANDIT2)]:
    for ch, short in [("dis.position.latitude_deg", "lat"), ("dis.position.longitude_deg", "lon"),
                      ("dis.position.altitude_m", "alt"), ("dis.velocity.speed_mps", "speed")]:
        var(f"{slug}_{short}", ch, rid, "dis_truth")

# Mission-scope basics
var("entities_active", "dis.entities_active", MISSION, "dis_truth")
var("sys_state", "uci.system_status.state", MISSION, "uci_c2", kind="enum")
var("svc_ir_state", "uci.service_status.state", MISSION, "uci_svc_ir", kind="enum")
var("svc_rf_state", "uci.service_status.state", MISSION, "uci_svc_rf", kind="enum")
var("sub_ir_state", "uci.subsystem_status.state", MISSION, "uci_sub_ir", kind="enum")
var("sub_rf_state", "uci.subsystem_status.state", MISSION, "uci_sub_rf", kind="enum")
var("svc_ir_uptime", "uci.service_status.time_up_s", MISSION, "uci_svc_ir")
var("svc_rf_uptime", "uci.service_status.time_up_s", MISSION, "uci_svc_rf")

# Eagle perceived (UCI)
var("eagle_uci_lat", "uci.position.latitude_deg", EAGLE, "uci_c2")
var("eagle_uci_lon", "uci.position.longitude_deg", EAGLE, "uci_c2")
var("eagle_uci_alt", "uci.position.altitude_m", EAGLE, "uci_c2")
var("eagle_uci_vn", "uci.velocity.northspeed", EAGLE, "uci_c2")
var("eagle_uci_ve", "uci.velocity.eastspeed", EAGLE, "uci_c2")
var("eagle_uci_vd", "uci.velocity.downspeed", EAGLE, "uci_c2")

# IR observations + FM signal (mission uci scope)
var("obs_az", "uci.ir_observation.azimuth_deg", MISSION, "uci_c2")
var("obs_el", "uci.ir_observation.elevation_deg", MISSION, "uci_c2")
var("obs_n", "uci.ir_observation.measurements_per_report", MISSION, "uci_c2")
var("sig_count", "uci.signal.count", MISSION, "uci_c2")
var("sig_freq", "uci.signal.frequency_hz", MISSION, "uci_c2")
var("sig_state", "uci.signal.state", MISSION, "uci_c2", kind="enum")

# UCI message totals
for m in ["PositionReport", "ObservationMeasurementReport", "SignalReport",
          "NavigationReport", "ServiceStatus", "SubsystemStatus", "SystemStatus"]:
    var(f"msg_{m}", f"uci.msg_total.{m}", MISSION, "uci_c2")

# Raw sensor stream stats
for name, ch in [
    ("ir_rate", "ir.frame.rate_hz"), ("ir_mean", "ir.frame.mean_intensity"),
    ("ir_max", "ir.frame.max_intensity"), ("ir_p999", "ir.frame.p999_intensity"),
    ("ir_sat", "ir.frame.saturated_px"), ("ir_bright", "ir.frame.bright_px"),
    ("ir_cx", "ir.target.centroid_x_px"), ("ir_cy", "ir.target.centroid_y_px"),
    ("ir_npx", "ir.target.pixel_count"), ("ir_fps_cfg", "squall.ir.fps_configured"),
    ("ir_los_az", "squall.ir.los.azimuth_deg"), ("ir_los_el", "squall.ir.los.elevation_deg"),
    ("plat_yaw", "squall.ir.platform.yaw_deg"), ("plat_pitch", "squall.ir.platform.pitch_deg"),
    ("plat_roll", "squall.ir.platform.roll_deg"),
    ("rf_rms", "rf.iq.rms_dbfs"), ("rf_peak", "rf.iq.peak_dbfs"),
    ("rf_clip", "rf.iq.clip_fraction"), ("rf_pkt", "rf.iq.packet_rate_hz"),
    ("rf_fc", "squall.rf.center_frequency_hz"), ("rf_fs", "squall.rf.sample_rate_sps"),
    ("spec_off", "rf.spectrum.peak_offset_hz"), ("spec_pwr", "rf.spectrum.peak_power_db"),
    ("au_rms", "audio.rms_dbfs"), ("au_peak", "audio.peak_dbfs"), ("au_act", "audio.active"),
]:
    var(name, ch, MISSION, "raw_sensor")
var("ir_mode", "squall.ir.mode", MISSION, "raw_sensor", kind="enum")
var("rf_mode", "squall.rf.mode", MISSION, "raw_sensor", kind="enum")

# Observability
var("sleet_conn", "obs.sleet_connections_active", MISSION, "obs_sleet")
var("sleet_delivered", "obs.sleet_messages_delivered_total", MISSION, "obs_sleet")
var("sleet_rx_bytes", "obs.sleet_websocket_bytes_received_total", MISSION, "obs_sleet")
var("sc_ticks", "obs.supercell_ticks_total", MISSION, "obs_supercell")
var("sc_pdus", "obs.supercell_dis_pdus_published_total", MISSION, "obs_supercell")
var("sc_entities", "obs.supercell_entities_active", MISSION, "obs_supercell")
var("sc_waypoints", "obs.supercell_waypoints_reached_total", MISSION, "obs_supercell")
var("gr_tracked", "obs.graupel_entities_tracked_active", MISSION, "obs_graupel")
var("gr_czml", "obs.graupel_czml_broadcast_total", MISSION, "obs_graupel")
var("gr_uci_rx", "obs.graupel_uci_messages_received_total", MISSION, "obs_graupel")

# ── panel builders ───────────────────────────────────────────────────────────
CHARTS = {}

def add_chart(viz):
    cid = U()
    CHARTS[cid] = viz
    return cid

def ts_panel(title, rows, thresholds=None, axis_title=None):
    """rows: list of (row_title, [(var, color)])"""
    axis_id = U()
    out_rows = []
    for rt, plots in rows:
        out_rows.append({"title": rt, "plots": [], "rowFlexSize": 1.0, "enabled": True,
            "plotsV2": [{"variableName": v, "enabled": True, "yAxisId": axis_id,
                         "type": {"type": "numeric", "numeric": {"color": c,
                                  "lineStyle": {"type": "v1", "v1": "SOLID"}}},
                         "hideFromTooltip": None} for v, c in plots]})
    thr = []
    if thresholds:
        thr = [{"axisId": axis_id, "visibility": True, "thresholds": {
            "type": "lineThresholds", "lineThresholds": {
                "shadingConfig": "NONE", "defaultFill": None,
                "lines": [{"value": v, "label": lbl, "color": col, "lineStyle": "DOTTED"}
                          for v, lbl, col in thresholds]}}}]
    return add_chart({"type": "timeSeries", "timeSeries": {"type": "v1", "v1": {
        "rows": out_rows, "comparisonRunGroups": [], "events": None, "title": title,
        "valueAxes": [{"id": axis_id, "title": axis_title or "",
            "displayOptions": {"showTitle": bool(axis_title), "axisWidth": None, "scaleType": None},
            "range": {"start": None, "end": None}, "limit": {"start": None, "end": None},
            "position": "LEFT", "domainType": "NUMERIC", "tickNumberFormat": None,
            "tooltipNumberFormat": None, "reversed": None, "tickInterval": None}],
        "thresholds": thr, "disconnectedValues": None, "stalenessConfiguration": None,
        "plotColoringConfiguration": {"type": "rowIndependent", "rowIndependent": {}},
        "anchorPoint": None, "bucketStrategy": None, "floatingLegendsConfig": None,
        "bucketDisplayConfig": None}}})

def geo_panel(title, plots, points=()):
    return add_chart({"type": "geo", "geo": {"type": "v1", "v1": {
        "plots": [{"id": U(), "latitudeVariableName": la, "longitudeVariableName": lo,
                   "secondaryVariables": None, "enabled": True, "label": lbl,
                   "visualizationOptions": {"color": c, "lineStyle": "SOLID",
                       "arrowStyle": "NONE", "secondaryColorVisualization": None}}
                  for la, lo, lbl, c in plots],
        "backgroundOverlays": [], "title": title,
        "customFeatures": [{"type": "point", "point": {"label": lbl, "icon": icon,
                            "latitude": la, "longitude": lo, "variables": []}}
                           for lbl, icon, la, lo in points],
        "customMeasurements": [], "baseTileset": "SATELLITE",
        "additionalTileset": None, "unitSystem": "METRIC"}}})

def cartesian_panel(title, plots, x_title, y_title):
    xa, ya = U(), U()
    def axis(aid, t):
        return {"id": aid, "title": t,
                "displayOptions": {"showTitle": True, "axisWidth": None, "scaleType": None},
                "range": {"start": None, "end": None}, "limit": {"start": None, "end": None},
                "position": "LEFT", "domainType": "NUMERIC", "tickNumberFormat": None,
                "tooltipNumberFormat": None, "reversed": None, "tickInterval": None}
    return add_chart({"type": "cartesian", "cartesian": {"type": "v1", "v1": {
        "title": title, "comparisonRunGroups": [],
        "plots": [{"xVariableName": xv, "yVariableName": yv, "xAxisId": xa, "yAxisId": ya,
                   "color": c, "enabled": True, "colorBy": None, "secondaryVariables": None}
                  for xv, yv, c in plots],
        "valueAxes": [axis(xa, x_title), axis(ya, y_title)],
        "connectPoints": True, "decimation": None, "events": None,
        "floatingLegendsConfig": None}}})

def geo3d_panel(title, plots):
    """plots: (lat_var, lon_var, alt_var, label, color, model)"""
    return add_chart({"type": "geo3d", "geo3d": {"type": "v1", "v1": {
        "title": title, "spatialPlots": [], "crs": {"type": "ecef", "ecef": {}},
        "plots": [{"plotId": U(), "enabled": True, "label": lbl,
                   "visualizationOptions": {"color": c, "lineStyle": "SOLID",
                       "model": {"type": "default", "default": model}},
                   "position": {"type": "wgs84", "wgs84": {
                       "latitudeVariableName": la, "longitudeVariableName": lo,
                       "heightVariableName": al}},
                   "orientation": {"type": "principalAxes", "principalAxes": {
                       "headingVariableName": "", "pitchVariableName": "",
                       "rollVariableName": ""}},
                   "modelLocalOrientation": None, "plotSensors": [],
                   "displayVectors": []}
                  for la, lo, al, lbl, c, model in plots]}}})

def hist_panel(title, plots):
    return add_chart({"type": "histogram", "histogram": {"type": "v1", "v1": {
        "plots": [{"variableName": v, "enabled": True, "color": c} for v, c in plots],
        "title": title, "displaySettings": {"stacked": False, "sort": "VALUE_ASCENDING"},
        "numericBucketStrategy": None, "valueAxis": None}}})

def md_panel(text):
    return add_chart({"type": "markdown", "markdown": {"type": "v1", "v1": {
        "content": text}}})

STATE_COLORS = {
    "OPERATIONAL": "green", "NORMAL": "green", "OPERATE": "green", "NEW": "green",
    "IR_MODE_OPERATIONAL": "green", "RF_MODE_OPERATIONAL": "green", "UPDATED": "green",
    "DEGRADED": "yellow", "STANDBY": "yellow", "IR_MODE_IDLE": "yellow",
    "RF_MODE_IDLE": "yellow", "INITIALIZE": "yellow", "IR_MODE_INITIALIZATION": "yellow",
    "RF_MODE_INITIALIZATION": "yellow",
    "FAULT": "red", "ERROR": "red", "LOST": "red", "OFF": "red",
}

def vt_panel(title, cells, columns):
    """cells: list of rows; each row: list of (var, kind, config_extra) or None.
    kind: 'num' with thresholds list [(val,color,label)] or None; 'enum'."""
    grid_cells = []
    n_rows = len(cells)
    for r, row in enumerate(cells):
        for col, spec in enumerate(row):
            if spec is None:
                continue
            v, kind, extra = spec
            if kind == "enum":
                cfg = {"type": "enum", "enum": {"visualisation": {"type": "raw", "raw": {
                    "colorMap": STATE_COLORS}}, "groupBySort": None}}
            else:
                thr = None
                if extra:
                    thr = [{"value": tv, "color": tc, "label": tl, "latch": None}
                           for tv, tc, tl in extra]
                cfg = {"type": "numeric", "numeric": {
                    "visualisation": {"type": "raw", "raw": {"thresholds": thr}},
                    "numberFormat": {"sigFigs": 5, "displayOption": None,
                                     "fixedDecimalPlaces": None, "decimalPlaces": None},
                    "groupBySort": None}}
            grid_cells.append({"row": r, "column": col,
                "cell": {"variableName": v, "uuid": U(), "config": cfg}})
    default_cfg = {
        "range": {"visualisation": {"type": "raw", "raw": {"rangeColor": None,
                  "rangeLabel": None, "noRangeColor": None, "noRangeLabel": None}},
                  "groupBySort": None},
        "enum": {"visualisation": {"type": "raw", "raw": {"colorMap": None}},
                 "groupBySort": None},
        "numeric": {"visualisation": {"type": "raw", "raw": {"thresholds": None}},
                    "numberFormat": None, "groupBySort": None},
        "staleness": {"visualisation": {"type": "raw", "raw": {"thresholds": None}},
                      "groupBySort": None},
        "numericArray": None, "enumArray": None, "struct": None}
    return add_chart({"type": "valueTable", "valueTable": {"type": "v2", "v2": {
        "title": title, "showUnits": True, "showStalenessIndicator": True,
        "layout": {"type": "grid", "grid": {
            "showRowHeaders": False, "rowHeaderWidth": None, "showColumnHeaders": True,
            "showCellLabels": True,
            "gridDefaultCellConfigs": default_cfg,
            "columnConfigs": [{"position": i, "header": h, "cellConfig": None}
                              for i, h in enumerate(columns)],
            "rowConfigs": [],
            "cells": grid_cells,
            "rowCount": n_rows, "columnCount": len(columns)}}}}})

# ── Tab 1: Mission Overview ──────────────────────────────────────────────────
t1_md = md_panel(
    "## AMS GRA Hello World Starter Kit - Live Mission View\n"
    "Open Arsenal reference mission system over Kitty Hawk, NC. Blue ownship **Eagle-1** "
    "carries Squall RF + IR sensors; **Bandit-1/2** fly one leg ahead; **WOBR-953** "
    "broadcasts FM from the ground. Truth from DIS multicast, perception from UCI over "
    "the Sleet LA-CAL, raw sensor stats from MEL taps.")
t1_vt = vt_panel("Mission status", [[
        ("entities_active", "num", [(0, "red", "none"), (3.5, "yellow", "partial"), (3.9, "green", "all")]),
        ("sys_state", "enum", None),
        ("ir_mode", "enum", None),
        ("rf_mode", "enum", None),
        ("sig_state", "enum", None),
        ("sleet_conn", "num", [(0, "red", "none"), (5, "green", "ok")]),
    ]],
    ["DIS entities", "System state", "IR sensor", "RF sensor", "FM signal", "OWP connections"])
t1_geo = geo_panel("Ground truth tracks (DIS)",
    [("eagle_lat", "eagle_lon", "Eagle-1 (blue)", BLUE),
     ("b1_lat", "b1_lon", "Bandit-1 (red)", RED),
     ("b2_lat", "b2_lon", "Bandit-2 (red)", ORANGE)],
    points=[("WOBR-953 FM tower", "radio-tower", 35.865166, -75.649611)])
t1_geo3d = geo3d_panel("3D tracking (DIS truth)",
    [("eagle_lat", "eagle_lon", "eagle_alt", "Eagle-1", BLUE, "FICTIONAL_FIGHTER"),
     ("b1_lat", "b1_lon", "b1_alt", "Bandit-1", RED, "FICTIONAL_FIGHTER"),
     ("b2_lat", "b2_lon", "b2_alt", "Bandit-2", ORANGE, "FICTIONAL_FIGHTER")])
t1_alt = ts_panel("Altitude - DIS truth [m HAE]",
    [("", [("eagle_alt", BLUE), ("b1_alt", RED), ("b2_alt", ORANGE)])],
    thresholds=[(1219.2, "Eagle-1 plan", BLUE), (1127.76, "Bandit-1 plan", RED),
                (1310.64, "Bandit-2 plan", ORANGE)],
    axis_title="altitude [m]")
t1_speed = ts_panel("Ground speed - DIS truth [m/s]",
    [("", [("eagle_speed", BLUE), ("b1_speed", RED), ("b2_speed", ORANGE)])],
    axis_title="speed [m/s]")

# ── Tab 2: Truth vs Perception ───────────────────────────────────────────────
t2_md = md_panel(
    "## Truth vs Perception (Eagle-1)\n"
    "DIS EntityState ground truth against the UCI `PositionReport` stream the mission "
    "system actually publishes on the OMS CAL. Divergence = navigation/reporting error.")
t2_alt = ts_panel("Altitude: truth vs UCI PositionReport [m]",
    [("", [("eagle_alt", BLUE), ("eagle_uci_alt", ORANGE)])], axis_title="altitude [m]")
t2_track = cartesian_panel("Ground track overlay (lon vs lat)",
    [("eagle_lon", "eagle_lat", BLUE), ("eagle_uci_lon", "eagle_uci_lat", ORANGE)],
    "longitude [deg]", "latitude [deg]")
t2_vel = ts_panel("Velocity: UCI NED components vs truth speed [m/s]",
    [("UCI NED", [("eagle_uci_vn", TEAL), ("eagle_uci_ve", PURPLE), ("eagle_uci_vd", YELLOW)]),
     ("Truth speed", [("eagle_speed", BLUE)])], axis_title="[m/s]")
t2_obs = ts_panel("IR observations of bandits (UCI LOS)",
    [("LOS az/el [deg]", [("obs_az", GREEN), ("obs_el", TEAL)]),
     ("measurements/report", [("obs_n", ORANGE)])])

# ── Tab 3: RF Chain ──────────────────────────────────────────────────────────
t3_md = md_panel(
    "## RF chain: Squall RF MFA -> rf-fm-demod skill\n"
    "Raw SC16 IQ tapped from the MEL data plane (second UDP destination via Couloir), "
    "FM demod audio from the skill's PCM socket, and the `SignalReport` UCI product. "
    "Signal drops when Eagle-1 flies out of WOBR-953 range - by scenario design.")
t3_vt = vt_panel("RF status", [[
        ("rf_mode", "enum", None),
        ("rf_fc", "num", None),
        ("rf_fs", "num", None),
        ("rf_rms", "num", [(-80, "red", "floor"), (-60, "yellow", "weak"), (-40, "green", "ok")]),
        ("rf_clip", "num", [(0, "green", "clean"), (0.001, "yellow", "clipping"), (0.05, "red", "saturated")]),
        ("au_act", "num", [(0, "yellow", "silent"), (0.5, "green", "audio")]),
    ]],
    ["Mode", "Center freq [Hz]", "Sample rate", "IQ RMS [dBFS]", "Clip fraction", "Audio active"])
t3_iq = ts_panel("IQ level [dBFS]",
    [("", [("rf_rms", BLUE), ("rf_peak", ORANGE)])],
    thresholds=[(-0.5, "full scale", RED)], axis_title="dBFS")
t3_spec = ts_panel("Spectral peak (8k FFT @ 2.048 Msps)",
    [("offset [Hz]", [("spec_off", TEAL)]), ("power [dB]", [("spec_pwr", PURPLE)])])
t3_audio = ts_panel("FM demod audio level [dBFS]",
    [("", [("au_rms", GREEN), ("au_peak", YELLOW)])],
    thresholds=[(-40, "speech floor", GREEN)], axis_title="dBFS")
t3_sig = ts_panel("UCI SignalReport (rf-fm-demod)",
    [("signals", [("sig_count", BLUE)]), ("freq [Hz]", [("sig_freq", ORANGE)])])
t3_hist = hist_panel("Audio level distribution", [("au_rms", GREEN)])

# ── Tab 4: IR Chain ──────────────────────────────────────────────────────────
t4_md = md_panel(
    "## IR chain: Squall IR MFA -> ir-search-and-track skill\n"
    "Raw mono8 320x200 @ 8 fps frames tapped from the MEL data plane. Bandits appear as "
    "hot targets; they drift out of the 90 x 64 deg FOV each pentagon leg - watch "
    "`target.pixel_count` drop and the UCI observations stop.")
t4_vt = vt_panel("IR status", [[
        ("ir_mode", "enum", None),
        ("ir_rate", "num", [(0, "red", "stalled"), (6, "yellow", "slow"), (7.2, "green", "nominal")]),
        ("ir_fps_cfg", "num", None),
        ("ir_sat", "num", [(0, "green", "ok"), (100, "yellow", "hot"), (2000, "red", "blooming")]),
        ("ir_npx", "num", [(0, "yellow", "no target"), (1, "green", "tracking")]),
        ("obs_n", "num", [(0, "yellow", "none"), (0.5, "green", "reporting")]),
    ]],
    ["Mode", "Frame rate [Hz]", "Configured fps", "Saturated px", "Target px", "UCI meas/report"])
t4_int = ts_panel("Frame intensity",
    [("", [("ir_mean", BLUE), ("ir_p999", ORANGE), ("ir_max", RED)])],
    thresholds=[(255, "saturation", RED)], axis_title="counts (mono8)")
t4_rate = ts_panel("Frame rate and target energy",
    [("rate [Hz]", [("ir_rate", GREEN)]),
     ("target px", [("ir_npx", RED), ("ir_bright", ORANGE)])])
t4_fp = cartesian_panel("Target centroid on focal plane (320x200)",
    [("ir_cx", "ir_cy", RED)], "x [px]", "y [px]")
t4_los = ts_panel("Sensor LOS and platform attitude [deg]",
    [("LOS", [("ir_los_az", BLUE), ("ir_los_el", TEAL)]),
     ("platform", [("plat_yaw", PURPLE), ("plat_pitch", GREEN), ("plat_roll", YELLOW)])],
    axis_title="[deg]")
t4_obs = ts_panel("UCI LOS observations vs sensor pointing [deg]",
    [("", [("obs_az", GREEN), ("obs_el", TEAL)])], axis_title="[deg]")

# ── Tab 5: C2 & System Health ────────────────────────────────────────────────
t5_md = md_panel(
    "## C2 and system health\n"
    "Service and subsystem states from UCI status messages; message flow and process "
    "health from Prometheus metrics scraped off every component (Sleet, Supercell, "
    "Squall, Graupel).")
t5_vt = vt_panel("Service / subsystem states", [[
        ("sys_state", "enum", None),
        ("svc_ir_state", "enum", None),
        ("svc_rf_state", "enum", None),
        ("sub_ir_state", "enum", None),
        ("sub_rf_state", "enum", None),
    ], [
        ("sleet_conn", "num", [(0, "red", "none"), (5, "green", "ok")]),
        ("svc_ir_uptime", "num", None),
        ("svc_rf_uptime", "num", None),
        ("sc_entities", "num", [(0, "red", "none"), (2.5, "green", "ok")]),
        ("gr_tracked", "num", None),
    ]],
    ["System", "IR skill", "RF skill", "IR MFA", "RF MFA"])
t5_msgs = ts_panel("UCI messages captured (cumulative)",
    [("", [("msg_PositionReport", BLUE), ("msg_ObservationMeasurementReport", RED),
           ("msg_SignalReport", GREEN), ("msg_NavigationReport", TEAL),
           ("msg_ServiceStatus", YELLOW), ("msg_SubsystemStatus", PURPLE),
           ("msg_SystemStatus", ORANGE)])], axis_title="messages")
t5_sleet = ts_panel("Sleet LA-CAL throughput",
    [("delivered", [("sleet_delivered", BLUE)]),
     ("ws bytes rx", [("sleet_rx_bytes", TEAL)])])
t5_sc = ts_panel("Supercell sim health",
    [("ticks", [("sc_ticks", BLUE)]), ("DIS PDUs", [("sc_pdus", ORANGE)]),
     ("waypoints", [("sc_waypoints", GREEN)])])
t5_gr = ts_panel("Graupel viz bridge",
    [("entities tracked", [("gr_tracked", BLUE)]),
     ("CZML broadcasts", [("gr_czml", ORANGE)]), ("UCI rx", [("gr_uci_rx", TEAL)])])

# ── layout ───────────────────────────────────────────────────────────────────
def canvas(objs):
    return {"type": "canvas", "canvas": {"id": U(), "objects": {
        cid: {"type": "panel",
              "panel": {"rect": {"x": x, "y": y, "width": w, "height": h},
                        "hideLegend": False}}
        for cid, (x, y, w, h) in objs.items()}}}

def tab(title, objs):
    return {"type": "v1", "v1": {"title": title, "panel": canvas(objs)}}

layout = {"type": "v1", "v1": {"rootPanel": {"type": "tabbed", "tabbed": {
    "type": "v1", "v1": {"id": U(), "tabs": [
        tab("Mission Overview", {
            t1_md: (0, 0, 1600, 130), t1_vt: (0, 130, 1600, 220),
            t1_geo: (0, 350, 800, 560), t1_geo3d: (800, 350, 800, 560),
            t1_alt: (0, 910, 800, 280), t1_speed: (800, 910, 800, 280)}),
        tab("Truth vs Perception", {
            t2_md: (0, 0, 1600, 110),
            t2_track: (0, 110, 800, 520), t2_alt: (800, 110, 800, 260),
            t2_vel: (800, 370, 800, 260), t2_obs: (0, 630, 1600, 280)}),
        tab("RF Chain", {
            t3_md: (0, 0, 1600, 110), t3_vt: (0, 110, 1600, 200),
            t3_iq: (0, 310, 800, 300), t3_spec: (800, 310, 800, 300),
            t3_audio: (0, 610, 640, 300), t3_sig: (640, 610, 560, 300),
            t3_hist: (1200, 610, 400, 300)}),
        tab("IR Chain", {
            t4_md: (0, 0, 1600, 110), t4_vt: (0, 110, 1600, 200),
            t4_int: (0, 310, 800, 300), t4_rate: (800, 310, 800, 300),
            t4_fp: (0, 610, 560, 320), t4_los: (560, 610, 640, 320),
            t4_obs: (1200, 610, 400, 320)}),
        tab("C2 & Health", {
            t5_md: (0, 0, 1600, 110), t5_vt: (0, 110, 1600, 260),
            t5_msgs: (0, 370, 800, 300), t5_sleet: (800, 370, 800, 300),
            t5_sc: (0, 670, 800, 280), t5_gr: (800, 670, 800, 280)}),
    ]}}}}}

# ── data scope inputs ────────────────────────────────────────────────────────
def scope_input(rid, name):
    return {"name": name, "label": name,
            "value": {"type": "asset", "asset": {"assetRid": rid}},
            "colorMode": {"type": "bySeries", "bySeries": {}}, "alignment": None}

data_scope_inputs = {"type": "v1", "v1": {"inputs": {
    MISSION: scope_input(MISSION, "Mission system"),
    EAGLE: scope_input(EAGLE, "Eagle-1"),
    BANDIT1: scope_input(BANDIT1, "Bandit-1"),
    BANDIT2: scope_input(BANDIT2, "Bandit-2"),
}}}

req_json = {
    "title": "AMS GRA Hello World SK - Mission Review",
    "description": ("Live review of the Open Arsenal AMS GRA Hello World Starter Kit: "
                    "DIS ground truth vs UCI perception, raw RF/IR sensor stream health, "
                    "FM demod products, and C2/system observability."),
    "isDraft": True,
    "stateAsJson": "{}",
    "layout": layout,
    "contentV2": {"type": "workbook", "workbook": {
        "charts": CHARTS,
        "channelVariables": CHANNEL_VARS,
        "dataScopeInputs": data_scope_inputs,
        "inputs": {"type": "v1", "v1": {"inputs": []}},
        "settings": None,
    }},
    "dataScope": {"type": "runRids", "runRids": [RUN_RID]},
    "runRid": RUN_RID,
    "workspace": WORKSPACE,
    "eventRefs": [],
    "labels": ["ams-gra", "open-arsenal"],
    "properties": {"scenario": "kitty-hawk-pentagon"},
}

def main():
    client = NominalClient.from_profile(PROFILE)
    c = client._clients
    req = ConjureDecoder().read_from_string(
        json.dumps(req_json), scout_notebook_api.CreateNotebookRequest)
    nb = c.notebook.create(c.auth_header, req)
    check = c.notebook.get(c.auth_header, nb.rid)
    d = json.loads(json.dumps(check, cls=ConjureEncoder))
    print("created workbook", nb.rid)
    print("charts:", len(d["contentV2"]["workbook"]["charts"]),
          "vars:", len(d["contentV2"]["workbook"]["channelVariables"]))
    print("URL:", f"https://app.gov.nominal.io/w/{WORKSPACE}/workbooks/{nb.rid}")

if __name__ == "__main__":
    main()
