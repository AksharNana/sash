#!/bin/sh
# What is vendored:
#   - z3-solver, libdash, pash-annotations, shasta, uv_build and setuptools
set -eu
. "$(dirname "$0")/common.sh"

out=$root/vendor
rm -rf "$out"
mkdir -p "$out"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

uv_build=$(sed -n 's/^requires = \["\(uv_build[^"]*\)"\]/\1/p' "$root/pyproject.toml")
[ -n "$uv_build" ] || { echo "vendor.sh: uv_build requirement not found in pyproject.toml" >&2; exit 1; }

# The locked runtime dependencies, with hashes. shasta comes from git and has
# no hash, so its URL is exported separately.
uv export --project "$root" --frozen --no-dev --no-emit-project \
    --no-emit-package shasta -o "$work/locked.txt" > /dev/null
uv export --project "$root" --frozen --no-dev --no-emit-project --no-hashes \
    | sed -n 's/^shasta @ git+\(.*\)$/\1/p' > "$work/shasta.url"
[ -s "$work/shasta.url" ] || { echo "vendor.sh: shasta not found in uv.lock" >&2; exit 1; }

# One download per target architecture. The aarch64 z3 wheel needs glibc >= 2.34.
for platforms in "manylinux_2_17_x86_64 manylinux2014_x86_64" \
                 "manylinux_2_34_aarch64 manylinux2014_aarch64"; do
    args=""
    for platform in $platforms; do
        args="$args --platform $platform"
    done
    # shellcheck disable=SC2086
    python3 -m pip download --no-deps --require-hashes \
        --only-binary=z3-solver,pash-annotations --no-binary=libdash \
        --python-version 3.10 $args -r "$work/locked.txt" -d "$out"
    # shellcheck disable=SC2086
    python3 -m pip download --no-deps --only-binary=:all: \
        --python-version 3.10 $args -d "$out" "$uv_build"
done

python3 -m pip download --no-deps --only-binary=:all: -d "$out" setuptools "wheel<0.38"

python3 -m pip wheel --no-deps -w "$out" "git+$(cat "$work/shasta.url")"

shasta_whl=$(basename "$(ls "$out"/shasta-*.whl)")
shasta_version=${shasta_whl#shasta-}
shasta_version=${shasta_version%%-*}
{
    grep -Eo '^[A-Za-z0-9._-]+==[A-Za-z0-9._+!-]+' "$work/locked.txt"
    echo "shasta==$shasta_version"
} > "$out/requirements.txt"

cat "$out/requirements.txt"
ls -l "$out"
