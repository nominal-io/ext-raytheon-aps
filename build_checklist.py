#!/usr/bin/env python3
"""Build and execute the AMS GRA mission health checklist."""
from __future__ import annotations

import json
import sys

from conjure_python_client import ConjureDecoder
from nominal.core.client import NominalClient
from nominal_api import scout_checks_api

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
WORKSPACE = os.environ["NOMINAL_WORKSPACE_RID"]
RUN_RID = os.environ["NOMINAL_RUN_RID"]

def raw(name):
    return {"type": "raw", "raw": {"name": name}}

def persist(seconds):
    return {"minDuration": {"type": "literal", "literal": {"seconds": seconds, "nanos": 0}},
            "minPoints": None,
            "outputRangeStart": {"type": "afterPersistenceWindow", "afterPersistenceWindow": {}}}

def lit(v):
    return {"type": "literal", "literal": v}

def threshold_check(var, op, value, seconds):
    return {"type": "threshold", "threshold": {
        "input": raw(var), "operator": op, "threshold": lit(value),
        "persistenceWindowConfiguration": persist(seconds)}}

def band_check(var, lo, hi, seconds):
    return {"type": "minMaxThreshold", "minMaxThreshold": {
        "input": raw(var), "lowerBound": lit(lo), "upperBound": lit(hi),
        "operator": "OUTSIDE_BOUNDS",
        "persistenceWindowConfiguration": persist(seconds)}}

def stale_check(var, seconds):
    return {"type": "staleRange", "staleRange": {
        "input": {"type": "numeric", "numeric": raw(var)},
        "threshold": lit({"seconds": seconds, "nanos": 0})}}

def locator(channel, ref, tags=None):
    return {"type": "series", "series": {
        "channel": channel, "dataSourceRef": ref, "tags": tags or {}}}

def check(title, desc, priority, ranges, variables):
    return {"type": "createCheck", "createCheck": {
        "title": title, "description": desc, "priority": priority,
        "condition": {"type": "numRangesV3", "numRangesV3": {
            "functionSpec": {}, "operator": "GREATER_THAN", "threshold": 0,
            "ranges": ranges, "variables": variables}},
        "generatedEventType": "FLAG",
        "generatedEventLabels": ["ams-gra", "auto-check"]}}

CHECKS = [
    check("All DIS entities present",
          "All 4 scenario entities (Eagle-1, Bandit-1, Bandit-2, WOBR-953) publish DIS EntityState. Fails if fewer than 4 active for 15 s.",
          "P1",
          threshold_check("entities", "LESS_THAN", 3.5, 15),
          {"entities": locator("dis.entities_active", "dis_truth")}),
    check("IR frame rate at least 7 Hz",
          "Squall IR MEL stream sustains at least 7 of the configured 8 fps. Fails below 7 Hz for 15 s.",
          "P1",
          threshold_check("rate", "LESS_THAN", 7.0, 15),
          {"rate": locator("ir.frame.rate_hz", "raw_sensor", {"sensor": "squall-ir"})}),
    check("IR focal plane not blooming",
          "Saturated pixel count stays under 2000 (of 64000). Sustained saturation means the sensor model or gain is wrong.",
          "P2",
          threshold_check("sat", "GREATER_THAN", 2000.0, 10),
          {"sat": locator("ir.frame.saturated_px", "raw_sensor", {"sensor": "squall-ir"})}),
    check("RF IQ stream above noise floor",
          "RF MEL IQ RMS level stays above -80 dBFS; below that the stream is effectively dead air.",
          "P1",
          threshold_check("rms", "LESS_THAN", -80.0, 15),
          {"rms": locator("rf.iq.rms_dbfs", "raw_sensor", {"sensor": "squall-rf"})}),
    check("RF ADC not clipping",
          "IQ clip fraction stays under 5 percent full scale.",
          "P2",
          threshold_check("clip", "GREATER_THAN", 0.05, 10),
          {"clip": locator("rf.iq.clip_fraction", "raw_sensor", {"sensor": "squall-rf"})}),
    check("Eagle-1 holds planned altitude band",
          "Ownship DIS truth altitude stays within 1219.2 m plus or minus 150 m per the flight plan.",
          "P2",
          band_check("alt", 1069.2, 1369.2, 30),
          {"alt": locator("dis.position.altitude_m", "dis_truth", {"entity": "Eagle-1"})}),
    check("UCI position reporting fresh",
          "Supercell PositionReports for Eagle-1 arrive at 1 Hz; fails if the perceived position goes stale for 10 s while the system runs.",
          "P1",
          stale_check("alt", 10),
          {"alt": locator("uci.position.altitude_m", "uci_c2", {"entity": "Eagle-1"})}),
    check("OWP connections healthy",
          "Sleet LA-CAL holds at least 6 active OWP WebSocket connections (all services plus the bridge).",
          "P2",
          threshold_check("conn", "LESS_THAN", 5.5, 20),
          {"conn": locator("obs.sleet_connections_active", "obs_metrics", {"service": "sleet"})}),
]

req_json = {
    "title": "AMS GRA Mission System Health",
    "description": ("Automated health checks for the AMS GRA Hello World Starter Kit capture: "
                    "DIS truth integrity, raw RF/IR sensor stream health, UCI reporting "
                    "freshness, and LA-CAL connectivity."),
    "assigneeRid": None,  # filled at runtime
    "commitMessage": "Initial: 8 checks covering DIS, IR/RF MEL, UCI freshness, OWP health",
    "checks": CHECKS,
    "checklistVariables": [],
    "labels": ["ams-gra", "open-arsenal"],
    "properties": {"scenario": "kitty-hawk-pentagon"},
    "isPublished": True,
    "workspace": WORKSPACE,
}

def main():
    client = NominalClient.from_profile(PROFILE)
    c = client._clients
    me = client.get_user()
    req_json["assigneeRid"] = me.rid
    req = ConjureDecoder().read_from_string(
        json.dumps(req_json), scout_checks_api.CreateChecklistRequest)
    cl = c.checklist.create(c.auth_header, req)
    print("checklist:", cl.rid)
    print("URL:", f"https://app.gov.nominal.io/w/{WORKSPACE}/checklists/{cl.rid}")

    if "--execute" in sys.argv:
        checklist = client.get_checklist(cl.rid)
        run = client.get_run(RUN_RID)
        review = checklist.execute(run)
        review = review.poll_for_completion()
        print("data review:", review.nominal_url)

if __name__ == "__main__":
    main()
