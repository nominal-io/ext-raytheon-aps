#!/usr/bin/env python3
"""Create or map the Nominal resources for a bridge config and write them to .env.

Thin wrapper kept for continuity; it is the same as

    nominal-bridge provision -c configs/hello-world-sk.yaml --profile <profile> --env-out .env

It shows a plan first (reuse which existing datasets, create which new ones) and asks
before changing anything. For each stream it reuses, in order: a dataset you name with
--map, the one already in .env (ignored if it's not in this workspace), one with the
exact name in the config, or one you pick from similar datasets. Otherwise it creates it.

    setup_nominal.py <profile>                              # plan, ask, then apply
    setup_nominal.py <profile> --plan                       # show the plan only
    setup_nominal.py <profile> --map dis=<dataset id> --map uci=<dataset id>
    setup_nominal.py <profile> --new                        # always create new datasets
    setup_nominal.py <profile> --existing                   # never create; map only
    setup_nominal.py <profile> -c configs/my-lab.yaml

Requires `pip install ./bridge` (public Nominal Python SDK only).
"""
import sys
from pathlib import Path

from nominal_bridge.__main__ import main

HERE = Path(__file__).parent

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1].startswith("-"):
        raise SystemExit(__doc__)
    args = sys.argv[2:]
    if "-c" not in args and "--config" not in args:
        args += ["-c", str(HERE / "configs" / "hello-world-sk.yaml")]
    env = HERE / ".env"
    if not env.exists():
        env.write_text("NAMESPACE_UUID=507e1ce1-1111-4444-8888-507e1ce11111\nMISSION_NAME=mission\n")
    main(["provision", "--profile", sys.argv[1], "--env-file", str(env),
          "--env-out", str(env), *args])
