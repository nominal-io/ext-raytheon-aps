#!/usr/bin/env bash
# Build the bridge image and write it to a tarball for a network with no registry
# access. On the far side:  podman load -i nominal-bridge-<version>.tar.gz
#
#   scripts/save-image.sh [linux/amd64|linux/arm64]
set -euo pipefail
cd "$(dirname "$0")/.."
platform="${1:-linux/amd64}"
version="$(sed -n 's/^version = "\(.*\)"/\1/p' bridge/pyproject.toml)"
tool="$(command -v docker || command -v podman)"
out="nominal-bridge-${version}-${platform##*/}.tar.gz"
"$tool" build --platform "$platform" -t "nominal-bridge:${version}" bridge
"$tool" save "nominal-bridge:${version}" | gzip > "$out"
echo "wrote $out ($(du -h "$out" | cut -f1))"
