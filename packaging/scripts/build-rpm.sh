#!/bin/sh
# Build the binary .rpm (for GitHub Releases) inside a clean container.
#
# Usage: build-rpm.sh [IMAGE]    default IMAGE: fedora:41
#
# Builds for the container's architecture. The package bundles a Python
# virtualenv and a compiled libdash, so it only works on the release it was
# built on. Works with a dnf-based image (Fedora, RHEL, Rocky, Alma, ...).
set -eu
. "$(dirname "$0")/common.sh"

image=${1:-fedora:41}
runtime=${CONTAINER_RUNTIME:-docker}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

stage_tree "$work/$pkg"
mkdir -p "$root/dist"

version=$(upstream_version)

# The container runs as root, so hand everything it created back to the
# invoking user (even on failure); otherwise the cleanup above cannot delete it.
"$runtime" run --rm -e VERSION="$version" -e PKG="$pkg" \
    -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
    -v "$work":/build -v "$root/packaging/rpm":/spec:ro -v "$root/dist":/dist \
    -w "/build/$pkg" "$image" sh -ec '
    trap "chown -R $HOST_UID:$HOST_GID /build /dist" EXIT
    dnf install -y --setopt=install_weak_deps=False \
        rpm-build python3 python3-devel python3-pip \
        gcc gcc-c++ make autoconf automake libtool
    rpmbuild -bb \
        --define "_topdir /build/rpmbuild" \
        --define "srcdir $PWD" \
        --define "_version $VERSION" \
        /spec/${PKG}.spec
    for f in /build/rpmbuild/RPMS/*/*.rpm; do
        cp "$f" /dist/
    done
'
ls -l "$root"/dist/${pkg}-*.rpm
