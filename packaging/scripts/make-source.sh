#!/bin/sh
# Build one signed-ready Debian source package per Ubuntu series.
#
# Usage: make-source.sh OUTDIR SERIES...   e.g. make-source.sh dist/ppa jammy noble
#
# Produces OUTDIR/<pkg>_<version>.orig.tar.gz (shared, byte-identical for all
# series) and, per series, <pkg>_<version>-<rev>~<series>1_source.changes.
#
# Needs GNU tar and dpkg-dev. Only committed files (HEAD) are packaged.

set -eu
. "$(dirname "$0")/common.sh"

outdir=$1
shift
mkdir -p "$outdir"
outdir=$(cd "$outdir" && pwd)

version=$(upstream_version)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

committed=$(dpkg-parsechangelog -l "$root/packaging/debian/changelog" -S Version)
if [ "${committed%-*}" = "$version" ]; then
    revision=${committed##*-}
else
    revision=1
fi

orig=${pkg}_$version.orig.tar.gz
if [ ! -f "$outdir/$orig" ]; then
    stage_tree "$work/$pkg-$version"
    tar --sort=name --owner=0 --group=0 --numeric-owner \
        --mtime="@$(git -C "$root" log -1 --format=%ct)" \
        -C "$work" -cf - "$pkg-$version" | gzip -n > "$outdir/$orig"
fi

for series in "$@"; do
    dir=$work/build-$series/$pkg-$version
    mkdir -p "$dir"
    tar -xzf "$outdir/$orig" -C "$work/build-$series"
    cp "$outdir/$orig" "$work/build-$series/$orig"
    cp -R "$root/packaging/debian" "$dir/debian"

    P=$pkg V="$version-$revision~${series}1" S=$series D=$(date -R) perl -pi -e '
        s/^\Q$ENV{P}\E \(.*?\) .*?;/$ENV{P} ($ENV{V}) $ENV{S};/ if $. == 1;
        s/^( -- .*?  ).*$/$1$ENV{D}/ if /^ -- / && !$done++;
    ' "$dir/debian/changelog"

    (cd "$dir" && dpkg-buildpackage -S -sa -d -us -uc)
    mv "$work/build-$series"/${pkg}_*"~${series}1"* "$outdir/"
done

ls -l "$outdir"
