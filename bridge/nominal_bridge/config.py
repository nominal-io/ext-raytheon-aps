"""YAML config loading: ${ENV} interpolation, defaults, validation."""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from .core import SINK_TYPES, SOURCE_TYPES

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(Exception):
    pass


def _interp(value, missing: set[str]):
    if isinstance(value, str):
        def sub(m):
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            missing.add(name)
            return ""
        return _ENV.sub(sub, value)
    if isinstance(value, dict):
        return {k: _interp(v, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [_interp(v, missing) for v in value]
    return value


def load_env_file(path: str | Path) -> None:
    """Read KEY=VALUE lines into the environment (existing variables win)."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def load(path: str | Path, *, strict_env: bool = True) -> dict:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    missing: set[str] = set()
    cfg = _interp(raw, missing)
    cfg.setdefault("sinks", [{"type": "nominal"}])
    cfg.setdefault("sources", [])
    cfg.setdefault("plugins", ["/plugins"])
    cfg.setdefault("capture_dir", "/capture")
    # relative plugin paths are relative to the config file
    cfg["plugins"] = [str(q if (q := Path(x)).is_absolute() else (p.parent / q).resolve())
                      for x in cfg["plugins"] or []]
    cfg["_missing_env"] = sorted(missing)
    cfg["_path"] = str(p)
    return cfg


def validate(cfg: dict, *, dry_run: bool = False) -> list[str]:
    """Return every problem found, so a user can fix them in one pass."""
    errs: list[str] = []
    sources = cfg.get("sources") or []
    if not sources:
        errs.append("no sources defined")
    seen: set[str] = set()
    streams: set[str] = set()
    for i, s in enumerate(sources):
        label = s.get("name") or f"sources[{i}]"
        if not s.get("name"):
            errs.append(f"{label}: missing 'name'")
        elif s["name"] in seen:
            errs.append(f"{label}: duplicate source name")
        seen.add(s.get("name"))
        typ = s.get("type")
        if typ not in SOURCE_TYPES:
            errs.append(f"{label}: unknown type '{typ}'. Available: {', '.join(sorted(SOURCE_TYPES))}")
            continue
        if s.get("enabled", True) is False:
            continue
        cls = SOURCE_TYPES[typ]
        merged = {**cls.merged_defaults(), **s}
        if not merged.get("stream"):
            errs.append(f"{label}: missing 'stream'")
        else:
            streams.add(merged["stream"])
        errs += [f"{label}: {e}" for e in cls.validate(merged)]

    health = (cfg.get("health") or {}).get("stream")
    if health:
        streams.add(health)

    for i, sink in enumerate(cfg.get("sinks") or []):
        typ = sink.get("type")
        if typ not in SINK_TYPES:
            errs.append(f"sinks[{i}]: unknown type '{typ}'. Available: {', '.join(sorted(SINK_TYPES))}")
            continue
        if typ == "nominal" and not dry_run:
            targets = sink.get("targets") or []
            if not targets:
                errs.append("sinks: nominal sink has no 'targets'")
            for t in targets:
                tn = t.get("name", "?")
                if not (t.get("token") or t.get("profile")):
                    errs.append(f"nominal target '{tn}': set 'token' or 'profile'")
                for st in sorted(streams):
                    if not (t.get("datasets") or {}).get(st):
                        errs.append(f"nominal target '{tn}': no dataset RID for stream '{st}'")
    if cfg.get("_missing_env") and not dry_run:
        errs.append("unset environment variables referenced in config: "
                    + ", ".join(cfg["_missing_env"]))
    return errs
