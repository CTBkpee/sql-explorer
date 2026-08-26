#!/usr/bin/env bash
# Copy the frozen browser package from the authoritative P1 package.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
destination="$root/web/analyzer"
rm -rf "$destination"
mkdir -p "$destination"
find "$root/analyzer" -maxdepth 1 -type f -name '*.py' -exec cp {} "$destination"/ \;
printf 'synced analyzer package to %s\n' "$destination"
