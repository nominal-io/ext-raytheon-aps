"""Command line: nominal-bridge run | validate | types | provision"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from . import __version__, config as cfgmod
from .core import SINK_TYPES, SOURCE_TYPES, load_plugins

DEFAULT_CONFIG = os.environ.get("BRIDGE_CONFIG", "/config/bridge.yaml")


def _load(args, *, dry_run: bool = False) -> dict:
    if args.env_file:
        cfgmod.load_env_file(args.env_file)
    cfg = cfgmod.load(args.config)
    if getattr(args, "capture_dir", None):
        cfg["capture_dir"] = args.capture_dir
    from . import sinks, sources  # noqa: F401  (register built-ins)
    from .util import log
    loaded = load_plugins(cfg["plugins"])
    if loaded:
        log("plugins loaded:", ", ".join(Path(f).name for f in loaded))
    problems = cfgmod.validate(cfg, dry_run=dry_run)
    if problems:
        print(f"{args.config}: {len(problems)} problem(s)", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        raise SystemExit(2)
    return cfg


def cmd_run(args) -> None:
    from .runner import run
    cfg = _load(args, dry_run=args.dry_run)
    only = set(args.only.split(",")) if args.only else None
    asyncio.run(run(cfg, dry_run=args.dry_run, only=only, duration_s=args.duration))


def cmd_validate(args) -> None:
    cfg = _load(args, dry_run=args.offline)
    srcs = [s for s in cfg["sources"] if s.get("enabled", True) is not False]
    print(f"{args.config}: OK ({len(srcs)} source(s) enabled)")
    for s in srcs:
        stream = {**SOURCE_TYPES[s["type"]].merged_defaults(), **s}["stream"]
        print(f"  {s['name']:<22} {s['type']:<18} -> stream '{stream}'")


def cmd_types(args) -> None:
    from . import sinks, sources  # noqa: F401
    from .sources.uci_handlers import UCI_HANDLERS
    if args.config and Path(args.config).is_file():
        load_plugins(cfgmod.load(args.config)["plugins"])
    for p in args.plugins or []:
        load_plugins([p])
    print("source types:")
    for name, cls in sorted(SOURCE_TYPES.items()):
        doc = (sys.modules[cls.__module__].__doc__ or "").strip().splitlines()
        print(f"  {name:<18} {doc[0] if doc else ''}")
        if args.verbose:
            for k, v in cls.merged_defaults().items():
                print(f"      {k}: {v!r}")
    print("sink types:  ", ", ".join(sorted(SINK_TYPES)))
    print("UCI handlers:", ", ".join(sorted(UCI_HANDLERS)),
          "(anything else goes through the generic flattener)")


def cmd_provision(args) -> None:
    from .provision import provision, write_env
    if args.env_file:
        cfgmod.load_env_file(args.env_file)
    cfg = cfgmod.load(args.config)
    mapping = dict(m.split("=", 1) for m in args.map or [])
    try:
        env = provision(cfg.get("model") or {}, profile=args.profile, mapping=mapping,
                        new_run=not args.no_run)
    except cfgmod.ConfigError as e:
        raise SystemExit(f"error: {e}")
    if args.env_out:
        if args.profile:                 # connection details for the container
            from nominal.config import NominalConfig
            prof = NominalConfig.from_yaml().profiles[args.profile]
            env["NOMINAL_BASE_URL"] = prof.base_url
            env["NOMINAL_TOKEN"] = prof.token
            if getattr(prof, "workspace_rid", None):
                env["NOMINAL_WORKSPACE_RID"] = prof.workspace_rid
        write_env(Path(args.env_out), env)
        print(f"wrote {len(env)} value(s) to {args.env_out}")
        for k, v in env.items():
            if k != "NOMINAL_TOKEN":
                print(f"  {k}={v}")
    else:
        for k, v in env.items():
            print(f"{k}={v}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="nominal-bridge", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("-c", "--config", default=DEFAULT_CONFIG,
                       help=f"bridge config (default {DEFAULT_CONFIG})")
        p.add_argument("--env-file", help="KEY=VALUE file to load before reading the config")

    p = sub.add_parser("run", help="run the bridge")
    common(p)
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be sent instead of sending it to Nominal")
    p.add_argument("--only", help="comma-separated source names to run")
    p.add_argument("--duration", type=float, help="stop after this many seconds")
    p.add_argument("--capture-dir", help="override capture_dir from the config")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("validate", help="check a config and list its sources")
    common(p)
    p.add_argument("--offline", action="store_true",
                   help="skip checks that need Nominal credentials / RIDs")
    p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("types", help="list source types, sink types and UCI handlers")
    p.add_argument("-c", "--config", default=None)
    p.add_argument("--plugins", action="append", help="extra plugin directory")
    p.add_argument("-v", "--verbose", action="store_true", help="show every option and default")
    p.set_defaults(fn=cmd_types)

    p = sub.add_parser("provision", help="create or map the Nominal datasets / assets / run "
                                         "described in the config's `model:` section")
    common(p)
    p.add_argument("--profile", help="Nominal profile (else NOMINAL_TOKEN + NOMINAL_BASE_URL)")
    p.add_argument("--map", action="append", metavar="STREAM=DATASET_RID",
                   help="use an existing dataset for a stream (repeatable)")
    p.add_argument("--no-run", action="store_true", help="do not create a new run")
    p.add_argument("--env-out", help="write the resulting RIDs into this env file")
    p.set_defaults(fn=cmd_provision)

    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except cfgmod.ConfigError as e:
        raise SystemExit(f"error: {e}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
