"""Create (or map onto existing) Nominal datasets, assets and a run from the `model:`
section of a bridge config. Uses only the public Nominal Python SDK.

A dataset whose RID is already supplied (environment variable or --map) is reused as
is; otherwise it is found by exact name, and created only if no match exists.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from .config import ConfigError
from .util import log


def connect(profile: str | None):
    from nominal.core.client import NominalClient
    if profile:
        return NominalClient.from_profile(profile)
    token, base = os.environ.get("NOMINAL_TOKEN"), os.environ.get("NOMINAL_BASE_URL")
    if not token or not base:
        raise ConfigError("give --profile, or set NOMINAL_TOKEN and NOMINAL_BASE_URL")
    return NominalClient.from_token(token, base,
                                    workspace_rid=os.environ.get("NOMINAL_WORKSPACE_RID") or None)


def _find_dataset(client, name: str):
    for ds in client.search_datasets(exact_match=name):
        if ds.name == name:
            return ds
    return None


def _find_asset(client, name: str):
    for a in client.search_assets(search_text=name):
        if a.name == name:
            return a
    return None


def _attach(asset, scopes: dict, datasets: dict) -> None:
    have = {ref for ref, _ in asset.list_datasets()}
    for scope, spec in (scopes or {}).items():
        if scope in have:
            continue
        stream = spec["stream"] if isinstance(spec, dict) else spec
        tags = spec.get("tags") if isinstance(spec, dict) else None
        if stream not in datasets:
            raise ConfigError(f"scope '{scope}' refers to unknown stream '{stream}'")
        if tags:
            asset.add_dataset(scope, datasets[stream], series_tags=tags)
        else:
            asset.add_dataset(scope, datasets[stream])
        log(f"  attached scope {scope} -> {stream}" + (f" {tags}" if tags else ""))


def provision(model: dict, *, profile: str | None, mapping: dict[str, str] | None = None,
              new_run: bool = True) -> dict[str, str]:
    """Returns the environment variables (name -> RID) the bridge config refers to."""
    if not model or not model.get("datasets"):
        raise ConfigError("the config has no `model.datasets` section to provision")
    client = connect(profile)
    labels = list(model.get("labels") or [])
    env: dict[str, str] = {}
    datasets: dict = {}

    for stream, spec in model["datasets"].items():
        rid = (mapping or {}).get(stream) or os.environ.get(spec.get("env", ""), "")
        if rid:
            ds = client.get_dataset(rid)
            log(f"dataset [{stream}] mapped to existing: {ds.name}")
        else:
            ds = _find_dataset(client, spec["name"])
            if ds is None:
                ds = client.create_dataset(
                    spec["name"], description=spec.get("description"), labels=labels,
                    properties=spec.get("properties") or {}, prefix_tree_delimiter=".")
                log(f"dataset [{stream}] created: {ds.name}")
            else:
                log(f"dataset [{stream}] found by name: {ds.name}")
        datasets[stream] = ds
        if spec.get("env"):
            env[spec["env"]] = ds.rid

    assets = []
    for spec in [model.get("asset")] + list(model.get("entity_assets") or []):
        if not spec:
            continue
        asset = _find_asset(client, spec["name"])
        if asset is None:
            asset = client.create_asset(
                spec["name"], description=spec.get("description"),
                properties={k: str(v) for k, v in (spec.get("properties") or {}).items()},
                labels=labels + list(spec.get("labels") or []))
            log(f"asset created: {asset.name}")
        else:
            log(f"asset found: {asset.name}")
        _attach(asset, spec.get("scopes"), datasets)
        if spec.get("env"):
            env[spec["env"]] = asset.rid
        assets.append(asset)

    run_spec = model.get("run")
    if run_spec and new_run and assets:
        start = datetime.now(timezone.utc)
        run = client.create_run(
            start.strftime(run_spec.get("name", "Capture session %Y-%m-%d %H:%MZ")),
            start=start, end=None, description=run_spec.get("description"),
            properties={k: str(v) for k, v in (run_spec.get("properties") or {}).items()},
            labels=labels, assets=[assets[0]])
        log(f"run created: {run.name}")
        if run_spec.get("env"):
            env[run_spec["env"]] = run.rid
    return env


def write_env(path: Path, values: dict[str, str]) -> None:
    """Update KEY=VALUE lines in place, appending keys that are not there yet."""
    lines = path.read_text().splitlines() if path.is_file() else []
    left = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in left and not line.lstrip().startswith("#"):
            out.append(f"{key}={left.pop(key)}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in left.items()]
    path.write_text("\n".join(out) + "\n")
