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


def _ask_yes(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _choose_dataset(decision, candidates):
    """Interactive pick when no dataset matched automatically."""
    if not candidates:
        return None
    print(f"\nNo dataset named '{decision.spec['name']}' for stream "
          f"'{decision.key}'. Similar ones in this workspace:")
    for i, ds in enumerate(candidates, 1):
        print(f"  {i}. {ds.name}")
    print("  n. create a new one")
    while True:
        try:
            ans = input(f"Use which for '{decision.key}'? [n] ").strip().lower()
        except EOFError:
            return None
        if ans in ("", "n", "new"):
            return None
        if ans.isdigit() and 1 <= int(ans) <= len(candidates):
            return candidates[int(ans) - 1]
        print("  enter a number from the list, or n")


def cmd_provision(args) -> None:
    from .provision import provision, write_env
    profile_url = None
    if args.profile:
        from nominal.config import NominalConfig
        prof = NominalConfig.from_yaml().profiles.get(args.profile)
        if prof is None:
            raise SystemExit(f"error: no Nominal profile named '{args.profile}'. "
                             "Create one with: nominal config profile add <name> --token ...")
        profile_url = prof.base_url
    env_url_before = os.environ.get("NOMINAL_BASE_URL")
    if args.env_file:
        cfgmod.load_env_file(args.env_file)
    # IDs in .env from a different tenant can't exist here: ignore them, don't fail
    env_url = env_url_before or os.environ.get("NOMINAL_BASE_URL")
    env_ok = not (profile_url and env_url and env_url.rstrip("/") != profile_url.rstrip("/"))
    if not env_ok:
        print(f"note: .env was written for {env_url}, but profile '{args.profile}' is "
              f"{profile_url}; ignoring the dataset and asset IDs in .env")
    cfg = cfgmod.load(args.config)
    mapping = dict(m.split("=", 1) for m in args.map or [])
    interactive = sys.stdin.isatty() and not args.yes and not args.plan
    mode = "new" if args.new else "existing" if args.existing else "auto"
    try:
        env = provision(cfg.get("model") or {}, profile=args.profile, mapping=mapping,
                        new_run=not args.no_run, mode=mode, env_ok=env_ok,
                        choose=_choose_dataset if interactive and mode == "auto" else None,
                        confirm=(lambda: False) if args.plan else
                        (lambda: _ask_yes("\nGo ahead? [y/N] ")) if interactive else None)
    except cfgmod.ConfigError as e:
        if args.plan and "cancelled" in str(e):
            print("\n--plan: nothing was changed")
            return
        raise SystemExit(f"error: {e}")
    if args.env_out:
        if args.profile:                 # connection details for the container
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
                                         "described in the config's `model:` section",
                       description=sys.modules["nominal_bridge.provision"].__doc__
                       if "nominal_bridge.provision" in sys.modules else None,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    common(p)
    p.add_argument("--profile", help="Nominal profile (else NOMINAL_TOKEN + NOMINAL_BASE_URL)")
    p.add_argument("--map", action="append", metavar="STREAM=DATASET_RID",
                   help="use an existing dataset for a stream (repeatable)")
    p.add_argument("--no-run", action="store_true", help="do not create a new run")
    how = p.add_mutually_exclusive_group()
    how.add_argument("--new", action="store_true",
                     help="create new datasets and assets even if matching ones exist")
    how.add_argument("--existing", action="store_true",
                     help="only use existing datasets; fail instead of creating any")
    p.add_argument("--plan", action="store_true",
                   help="show what would be reused or created, change nothing")
    p.add_argument("-y", "--yes", action="store_true",
                   help="don't ask: skip the confirmation and the dataset picker")
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
