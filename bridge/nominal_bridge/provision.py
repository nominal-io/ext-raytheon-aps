"""Create, or map onto existing, Nominal datasets, assets and a run from the `model:`
section of a bridge config. Uses only the public Nominal Python SDK.

It plans first and changes nothing until the plan is accepted. For each dataset, in order:
  1. --map STREAM=<id>        must exist, or it's an error
  2. the ID already in .env   reused if it exists in this workspace, otherwise ignored
                              with a note (e.g. .env from another tenant)
  3. a dataset with the exact name in the config
  4. interactively: pick from similar datasets in the workspace
  5. create it
--new skips 2-4 and creates everything; --existing never creates.
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


def _try_get(getter, rid: str):
    """The object, or None if this ID doesn't exist or isn't visible to this login."""
    try:
        return getter(rid)
    except Exception:
        return None


def _similar_datasets(client, spec: dict, labels: list[str], limit: int = 8) -> list:
    """Datasets that look like candidates for a stream: same labels, or a name sharing
    words with the one we would create."""
    seen, out = set(), []
    words = [w for w in spec["name"].replace("-", " ").split() if len(w) > 2]
    queries = [{"labels": labels}] if labels else []
    queries += [{"search_text": w} for w in words[-2:]]
    for q in queries:
        try:
            found = client.search_datasets(**q)
        except Exception:
            continue
        for ds in found:
            if ds.rid not in seen:
                seen.add(ds.rid)
                out.append(ds)
    return out[:limit]


class Decision:
    """What provision will do for one dataset or asset."""

    def __init__(self, kind: str, key: str, spec: dict):
        self.kind, self.key, self.spec = kind, key, spec
        self.obj = None          # existing object to reuse
        self.how = "create"      # map | env | name | pick | create
        self.notes: list[str] = []

    def describe(self) -> str:
        if self.obj is None:
            target = f"create new  '{self.spec['name']}'"
        else:
            src = {"map": "--map", "env": ".env", "name": "same name", "pick": "you chose"}[self.how]
            target = f"use existing '{self.obj.name}' ({src})"
        return f"  {self.kind:<8} {self.key:<22} {target}"


def plan(client, model: dict, *, mapping: dict[str, str], mode: str, env_ok: bool,
         choose=None) -> list[Decision]:
    """Decide, without changing anything, which existing datasets/assets to reuse and
    which to create.

    mode   auto      reuse what is clearly meant (--map, .env, exact name), else create
           new       create everything (ignores .env and name matches)
           existing  never create; every dataset must resolve to an existing one
    env_ok False when the IDs in .env belong to a different Nominal tenant, so they are
           ignored instead of failing.
    choose optional callback(decision, candidates) -> dataset | None, used to let a
           person pick an existing dataset when nothing matched automatically.
    """
    labels = list(model.get("labels") or [])
    decisions: list[Decision] = []
    for stream, spec in (model.get("datasets") or {}).items():
        d = Decision("dataset", stream, spec)
        explicit = mapping.get(stream)
        env_rid = os.environ.get(spec.get("env", ""), "") if env_ok else ""
        if explicit:                                   # an explicit choice must work
            d.obj = _try_get(client.get_dataset, explicit)
            if d.obj is None:
                raise ConfigError(f"--map {stream}={explicit}: no such dataset in this "
                                  "workspace, or this login can't see it")
            d.how = "map"
        elif mode != "new":
            if env_rid:
                d.obj = _try_get(client.get_dataset, env_rid)
                if d.obj is not None:
                    d.how = "env"
                else:
                    d.notes.append(f"ignored {spec['env']} from .env: that dataset isn't "
                                   "in this workspace")
            if d.obj is None:
                d.obj = _find_dataset(client, spec["name"])
                d.how = "name" if d.obj is not None else "create"
            if d.obj is None and choose is not None:
                picked = choose(d, _similar_datasets(client, spec, labels))
                if picked is not None:
                    d.obj, d.how = picked, "pick"
        decisions.append(d)

    for spec in [model.get("asset")] + list(model.get("entity_assets") or []):
        if not spec:
            continue
        d = Decision("asset", spec.get("env") or "asset", spec)
        if mode != "new":
            env_rid = os.environ.get(spec.get("env", ""), "") if env_ok else ""
            if env_rid:
                d.obj = _try_get(client.get_asset, env_rid)
                if d.obj is not None:
                    d.how = "env"
                else:
                    d.notes.append(f"ignored {spec['env']} from .env: that asset isn't in "
                                   "this workspace")
            if d.obj is None:
                d.obj = _find_asset(client, spec["name"])
                d.how = "name" if d.obj is not None else "create"
        decisions.append(d)

    if mode == "existing":
        missing = [d for d in decisions if d.kind == "dataset" and d.obj is None]
        if missing:
            raise ConfigError(
                "no existing dataset for: " + ", ".join(d.key for d in missing)
                + ". Pass --map STREAM=<dataset id>, or drop --existing to create them.")
    return decisions


def apply(client, model: dict, decisions: list[Decision], *, new_run: bool) -> dict[str, str]:
    """Create what the plan says to create, attach scopes, open a run. Returns the
    environment variables (name -> ID) the bridge config refers to."""
    labels = list(model.get("labels") or [])
    env: dict[str, str] = {}
    datasets: dict = {}
    assets = []
    for d in decisions:
        if d.kind != "dataset":
            continue
        ds = d.obj or client.create_dataset(
            d.spec["name"], description=d.spec.get("description"), labels=labels,
            properties=d.spec.get("properties") or {}, prefix_tree_delimiter=".")
        if d.obj is None:
            log(f"dataset [{d.key}] created: {ds.name}")
        datasets[d.key] = ds
        if d.spec.get("env"):
            env[d.spec["env"]] = ds.rid
    for d in decisions:
        if d.kind != "asset":
            continue
        spec = d.spec
        asset = d.obj or client.create_asset(
            spec["name"], description=spec.get("description"),
            properties={k: str(v) for k, v in (spec.get("properties") or {}).items()},
            labels=labels + list(spec.get("labels") or []))
        if d.obj is None:
            log(f"asset created: {asset.name}")
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


def provision(model: dict, *, profile: str | None, mapping: dict[str, str] | None = None,
              new_run: bool = True, mode: str = "auto", env_ok: bool = True,
              choose=None, confirm=None) -> dict[str, str]:
    """Plan, show the plan, optionally ask, then create. Returns env var -> ID."""
    if not model or not model.get("datasets"):
        raise ConfigError("the config has no `model.datasets` section to provision")
    client = connect(profile)
    decisions = plan(client, model, mapping=mapping or {}, mode=mode, env_ok=env_ok,
                     choose=choose)
    print("Plan:")
    for d in decisions:
        print(d.describe())
        for n in d.notes:
            print(f"      note: {n}")
    if new_run and model.get("run"):
        print("  run      (new)          create a new run for this session")
    if confirm is not None and not confirm():
        raise ConfigError("cancelled; nothing was changed")
    return apply(client, model, decisions, new_run=new_run)


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
