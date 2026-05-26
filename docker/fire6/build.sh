#!/usr/bin/env bash
# Build the fire6 docker image from a FIRE6 source tree.
#
# Why: the FIRE6 Dockerfile lives in this repo (not upstream FIRE), so to
# rebuild after patching FIRE you need to drop it into the FIRE6 dir before
# `docker build`. This script does that for you.
#
# Usage:
#   docker/fire6/build.sh /path/to/fire/FIRE6 [image-tag]
#
# Example (after cloning FIRE upstream):
#   git clone https://gitlab.com/feynmanIntegrals/fire ~/fire
#   docker/fire6/build.sh ~/fire/FIRE6
#   # then in config.yaml, point solvers.fire.docker.image at the tag
#   # (defaults to `fire6`, matching the example config in this repo).

set -euo pipefail

FIRE_DIR="${1:-}"
IMAGE_TAG="${2:-fire6}"

if [[ -z "$FIRE_DIR" ]]; then
    echo "Usage: $0 /path/to/fire/FIRE6 [image-tag]" >&2
    echo "  Defaults: image-tag=fire6" >&2
    exit 1
fi

if [[ ! -d "$FIRE_DIR" ]]; then
    echo "FIRE6 source dir not found: $FIRE_DIR" >&2
    echo "Clone FIRE first: git clone https://gitlab.com/feynmanIntegrals/fire" >&2
    exit 1
fi

if [[ ! -f "$FIRE_DIR/configure" || ! -d "$FIRE_DIR/sources" ]]; then
    echo "Path doesn't look like a FIRE6 source dir (missing configure/ or sources/): $FIRE_DIR" >&2
    echo "Did you point at the FIRE repo root instead of FIRE6/?" >&2
    exit 1
fi

# FIRE6/extra/fuel/ is a git submodule of the FIRE repo (and fuel itself
# has nested submodules: flint, pari, symbolica). The Dockerfile assumes
# fuel is already vendored — if it isn't, the build fails deep inside
# `make depend` with a cryptic `./configure: not found`. Init the
# submodules transparently here.
if [[ ! -f "$FIRE_DIR/extra/fuel/configure" ]]; then
    echo "==> extra/fuel/configure missing — initialising FIRE submodules"
    # The submodule definition lives in the FIRE repo root (one level up
    # from FIRE6/), which is where .git typically is. Walk up to find it.
    GIT_ROOT="$FIRE_DIR"
    while [[ "$GIT_ROOT" != "/" && ! -e "$GIT_ROOT/.git" ]]; do
        GIT_ROOT="$(dirname "$GIT_ROOT")"
    done
    if [[ ! -e "$GIT_ROOT/.git" ]]; then
        echo "Could not find a .git above $FIRE_DIR — can't init submodules." >&2
        echo "Re-clone FIRE with: git clone --recurse-submodules https://gitlab.com/feynmanIntegrals/fire" >&2
        exit 1
    fi
    (cd "$GIT_ROOT" && git submodule update --init --recursive)
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$SCRIPT_DIR/Dockerfile" "$FIRE_DIR/Dockerfile"

echo "==> Building $IMAGE_TAG from $FIRE_DIR"
cd "$FIRE_DIR"
docker build -t "$IMAGE_TAG" .

echo
echo "==> Built $IMAGE_TAG. In feynman-ibp-bench/config.yaml set:"
echo "    solvers:"
echo "      fire:"
echo "        docker:"
echo "          image: $IMAGE_TAG"
echo "          binary: /fire/bin/FIRE6p"
