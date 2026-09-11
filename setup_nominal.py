#!/usr/bin/env python3
"""Create or map the Nominal resources for the AMS GRA Hello World demo, and emit .env.

By default this is idempotent: each dataset is found by exact name and reused,
or created if absent. To map the streams onto datasets you already have instead,
pass their RIDs explicitly:

    setup_nominal.py <profile>                              # find-or-create by name
    setup_nominal.py <profile> --ds-dis <rid> --ds-uci <rid> \
                     --ds-sensor <rid> --ds-obs <rid>       # map to existing datasets

Any subset of the --ds-* flags works; unspecified stream families fall back to
find-or-create. Writes RIDs + credentials into .env for docker compose.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from nominal.core.client import NominalClient
from nominal.config import NominalConfig

parser = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("profile", help="Nominal config profile name")
parser.add_argument("--ds-dis", help="Existing dataset RID for DIS ground truth")
parser.add_argument("--ds-uci", help="Existing dataset RID for UCI C2 messages")
parser.add_argument("--ds-sensor", help="Existing dataset RID for raw sensor streams")
parser.add_argument("--ds-obs", help="Existing dataset RID for observability metrics")
args = parser.parse_args()

PROFILE = args.profile
EXISTING = {
    "NOMINAL_DS_DIS": args.ds_dis,
    "NOMINAL_DS_UCI": args.ds_uci,
    "NOMINAL_DS_SENSOR": args.ds_sensor,
    "NOMINAL_DS_OBS": args.ds_obs,
}

DATASETS = {
    "NOMINAL_DS_DIS": (
        "AMS GRA - DIS Ground Truth",
        "IEEE 1278.1 DIS EntityState ground truth from the Supercell environment sim "
        "(multicast 239.1.2.3:21100). Per-entity position, velocity, and orientation, "
        "tagged by entity callsign.",
    ),
    "NOMINAL_DS_UCI": (
        "AMS GRA - UCI C2 Messages",
        "UCI 002.5.0 messages captured from the Sleet LA-CAL OWP WebSocket (port 21402): "
        "PositionReport, ObservationMeasurementReport, SignalReport, NavigationReport, "
        "SubsystemStatus, ServiceStatus, SystemStatus.",
    ),
    "NOMINAL_DS_SENSOR": (
        "AMS GRA - Raw Sensor Streams",
        "Derived telemetry from the raw MFA MEL data planes: Squall IR mono8 frame "
        "statistics (UDP tap), Squall RF SC16 IQ statistics (UDP tap), rf-fm-demod "
        "PCM audio levels (TCP 21602), and Squall control-plane status (Couloir gRPC).",
    ),
    "NOMINAL_DS_OBS": (
        "AMS GRA - Observability Metrics",
        "Prometheus metrics scraped from Sleet, Supercell, Squall RF/Optical, Couloir, "
        "and Graupel admin endpoints, tagged by service.",
    ),
}

ASSET_NAME = "AMS GRA Hello World Starter Kit"


def find_dataset(client, name):
    for ds in client.search_datasets(exact_match=name):
        if ds.name == name:
            return ds
    return None


def main() -> None:
    client = NominalClient.from_profile(PROFILE)
    print("user:", client.get_user().email)

    env: dict[str, str] = {}

    for env_key, (name, desc) in DATASETS.items():
        if EXISTING.get(env_key):
            ds = client.get_dataset(EXISTING[env_key])
            print("mapped to existing dataset", ds.name, ds.rid)
            env[env_key] = ds.rid
            continue
        ds = find_dataset(client, name)
        if ds is None:
            ds = client.create_dataset(
                name,
                description=desc,
                labels=["ams-gra", "open-arsenal"],
                properties={"source": "hello-world-sk", "capture": "nominal-bridge"},
                prefix_tree_delimiter=".",
            )
            print("created dataset", name, ds.rid)
        else:
            print("found dataset", name, ds.rid)
        env[env_key] = ds.rid

    # Mission-level asset
    assets = {a.name: a for a in client.search_assets(search_text="AMS GRA")}
    if ASSET_NAME in assets:
        asset = assets[ASSET_NAME]
        print("found asset", asset.rid)
    else:
        asset = client.create_asset(
            ASSET_NAME,
            description=(
                "Open Arsenal AMS GRA Hello World Starter Kit simulation: Eagle-1 ownship "
                "with simulated RF and IR sensors, two red bandits, and the WOBR-953 FM "
                "tower over Kitty Hawk, NC. Sensors: Squall MFA (RF + IR), skills: "
                "rf-fm-demod, ir-search-and-track, C2: Sleet LA-CAL (OMS/UCI)."
            ),
            properties={
                "standard": "AMS GRA v2026.01",
                "kit_version": "v2026.06.01",
                "ownship": "Eagle-1",
                "scenario": "Kitty Hawk pentagon",
            },
            labels=["ams-gra", "open-arsenal", "simulation"],
        )
        print("created asset", asset.rid)

    refname_to_env = {
        "dis_truth": "NOMINAL_DS_DIS",
        "uci_c2": "NOMINAL_DS_UCI",
        "raw_sensor": "NOMINAL_DS_SENSOR",
        "obs_metrics": "NOMINAL_DS_OBS",
    }
    existing_refs = {r: d for r, d in asset.list_datasets()}
    for ref, env_key in refname_to_env.items():
        if ref not in existing_refs:
            ds = client.get_dataset(env[env_key])
            asset.add_dataset(ref, ds)
            print("attached", ref)
        else:
            print("already attached", ref)

    env["NOMINAL_ASSET_RID"] = asset.rid

    # Extra tag-scoped refs on the mission asset so per-service/subsystem enum
    # state channels resolve to a single series in app panels.
    extra_scopes = {
        "uci_svc_ir": (env["NOMINAL_DS_UCI"], {"service": "ir-search-and-track"}),
        "uci_svc_rf": (env["NOMINAL_DS_UCI"], {"service": "rf-fm-demod"}),
        "uci_sub_ir": (env["NOMINAL_DS_UCI"], {"subsystem": "squall.ir.mfa"}),
        "uci_sub_rf": (env["NOMINAL_DS_UCI"], {"subsystem": "squall.rf.mfa"}),
        "obs_sleet": (env["NOMINAL_DS_OBS"], {"service": "sleet"}),
        "obs_supercell": (env["NOMINAL_DS_OBS"], {"service": "supercell"}),
        "obs_graupel": (env["NOMINAL_DS_OBS"], {"service": "graupel"}),
    }
    have = {r for r, _ in asset.list_datasets()}
    for ref, (ds_rid, tags) in extra_scopes.items():
        if ref not in have:
            asset.add_dataset(ref, client.get_dataset(ds_rid), series_tags=tags)
            print("added scope", ref, tags)

    # Per-platform assets over the shared tagged datasets ("each unit is an asset")
    entities = [
        ("NOMINAL_ASSET_EAGLE", "Eagle-1",
         "Blue ownship carrying the Squall RF and IR MFA sensors", "FRIENDLY"),
        ("NOMINAL_ASSET_BANDIT1", "Bandit-1",
         "Red bandit lead aircraft, one leg ahead of ownship, 300 ft lower", "OPPOSING"),
        ("NOMINAL_ASSET_BANDIT2", "Bandit-2",
         "Red bandit wing aircraft, one leg ahead of ownship, 300 ft higher", "OPPOSING"),
        ("NOMINAL_ASSET_WOBR", "WOBR-953",
         "Fixed FM radio tower broadcasting at 915 MHz (sim), Kitty Hawk NC", "NEUTRAL"),
    ]
    existing_entities = {a.name: a for a in client.search_assets(search_text="(AMS GRA)")}
    for env_key, name, desc, force in entities:
        full = f"{name} (AMS GRA)"
        if full in existing_entities:
            ent = existing_entities[full]
            print("found entity asset", full, ent.rid)
        else:
            ent = client.create_asset(
                full, description=desc,
                properties={"entity": name, "force": force,
                            "scenario": "kitty-hawk-pentagon"},
                labels=["ams-gra", "open-arsenal"])
            ent.add_dataset("dis_truth", client.get_dataset(env["NOMINAL_DS_DIS"]),
                            series_tags={"entity": name})
            if name == "Eagle-1":
                ent.add_dataset("uci_c2", client.get_dataset(env["NOMINAL_DS_UCI"]),
                                series_tags={"entity": name})
                ent.add_dataset("raw_sensor", client.get_dataset(env["NOMINAL_DS_SENSOR"]))
            print("created entity asset", full, ent.rid)
        env[env_key] = ent.rid

    # Run for this capture session
    start = datetime.now(timezone.utc)
    run_name = f"AMS GRA Sim Session {start.strftime('%Y-%m-%d %H:%M')}Z"
    run = client.create_run(
        run_name,
        start=start,
        end=None,
        description=(
            "Live capture session of the AMS GRA Hello World Starter Kit streamed into "
            "Nominal by nominal-bridge (DIS truth, UCI C2, raw sensor taps, observability)."
        ),
        properties={"scenario": "kitty-hawk-pentagon", "capture": "nominal-bridge"},
        labels=["ams-gra", "open-arsenal"],
        assets=[asset],
    )
    print("created run", run.rid, run.nominal_url if hasattr(run, "nominal_url") else "")
    env["NOMINAL_RUN_RID"] = run.rid

    cfg = NominalConfig.from_yaml()
    prof = cfg.profiles[PROFILE]
    env["NOMINAL_BASE_URL"] = prof.base_url
    env["NOMINAL_TOKEN"] = prof.token
    if getattr(prof, "workspace_rid", None):
        env["NOMINAL_WORKSPACE_RID"] = prof.workspace_rid

    env_path = Path(__file__).parent / ".env"
    lines = [
        "NAMESPACE_UUID=507e1ce1-1111-4444-8888-507e1ce11111",
        "MISSION_NAME=mission",
    ] + [f"{k}={v}" for k, v in env.items()]
    env_path.write_text("\n".join(lines) + "\n")
    print("wrote", env_path)
    for k, v in env.items():
        if k != "NOMINAL_TOKEN":
            print(f"  {k}={v}")


if __name__ == "__main__":
    main()
