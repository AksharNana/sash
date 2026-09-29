#!/bin/sh
# Build the binary .deb (for GitHub Releases) inside a clean container.
#
# Usage: build-deb.sh [IMAGE]    default IMAGE: ubuntu:22.04
#
# Builds for the container's architecture. The package bundles a Python
# virtualenv and a compiled libdash, so it only works on the release it was
# built on.
set -eu
. "$(dirname "$0")/common.sh"

image=${1:-ubuntu:22.04}
runtime=${CONTAINER_RUNTIME:-docker}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

stage_tree "$work/$pkg"
cp -R "$root/packaging/debian" "$work/$pkg/debian"
mkdir -p "$root/dist"

tag=$(echo "$image" | tr ':/' '--')

# The container runs as root, so hand everything it created back to the
# invoking user (even on failure); otherwise the cleanup above cannot delete it.
"$runtime" run --rm -e TAG="$tag" -e PKG="$pkg" -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" -v "$work":/build -v "$root/dist":/dist -w "/build/$pkg" "$image" sh -ec '
    trap "chown -R $HOST_UID:$HOST_GID /build /dist" EXIT
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends devscripts equivs
    mk-build-deps -ir -t "apt-get -y --no-install-recommends" debian/control
    dpkg-buildpackage -b -us -uc
    for f in ../${PKG}_*.deb; do
        b=$(basename "$f" .deb)
        cp "$f" "/dist/${b%_*}_${TAG}_${b##*_}.deb"
    done
'
ls -l "$root"/dist/${pkg}_*.deb
