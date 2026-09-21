#!/bin/sh
# Build the binary .deb (for GitHub Releases) inside a clean container.
#
# Usage: build-deb.sh [IMAGE]    default IMAGE: ubuntu:22.04
#
# Builds for the container's architecture. The package bundles a
# Python virtualenv and a compiled libdash, so it only works on the release
# it was built on. 

set -eu
. "$(dirname "$0")/common.sh"

image=${1:-ubuntu:22.04}
runtime=${CONTAINER_RUNTIME:-docker}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

stage_tree "$work/sash"
cp -R "$root/packaging/debian" "$work/sash/debian"
mkdir -p "$root/dist"

tag=$(echo "$image" | tr ':/' '--')

"$runtime" run --rm -e TAG="$tag" -v "$work":/build -v "$root/dist":/dist -w /build/sash "$image" sh -ec '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends devscripts equivs
    mk-build-deps -ir -t "apt-get -y --no-install-recommends" debian/control
    dpkg-buildpackage -b -us -uc
    for f in ../sash_*.deb; do
        b=$(basename "$f" .deb)
        cp "$f" "/dist/${b%_*}_${TAG}_${b##*_}.deb"
    done
'
ls -l "$root"/dist/sash_*.deb
