#!/usr/bin/env bash
# Fetch the small, browser-required Pyodide set and the frozen sqlglot wheel.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
pyodide_version=0.27.7
sqlglot_version=30.17.0

declare -a assets=(
  "b4cb23a53aba19c221659b9fb40a2f18281d685691dc06647fb0afd0681baf98 pyodide pyodide.js https://cdn.jsdelivr.net/pyodide/v${pyodide_version}/full/pyodide.js"
  "6b4c90de5b7172873f04f21884d0e9d2274e305fe32116558fe3e4fbe3618d51 pyodide pyodide.asm.js https://cdn.jsdelivr.net/pyodide/v${pyodide_version}/full/pyodide.asm.js"
  "a50dd1843f805a0b7c45b61037ee0d7b26dfe85efe0e18ef95a34ad24e401f5f pyodide pyodide.asm.wasm https://cdn.jsdelivr.net/pyodide/v${pyodide_version}/full/pyodide.asm.wasm"
  "16611534726e5d8ac2bd8f926410b2dcb8d6f49aa24913463533b457a2115c16 pyodide python_stdlib.zip https://cdn.jsdelivr.net/pyodide/v${pyodide_version}/full/python_stdlib.zip"
  "9c45b916001a750f4102fc287494f3eab215909c7535c626a20db80fd6333e2c pyodide pyodide-lock.json https://cdn.jsdelivr.net/pyodide/v${pyodide_version}/full/pyodide-lock.json"
  "84435ac283a60173da31b5fd7d11a725037a1c3fd6ed1e21fb065de74ddb579f sqlglot sqlglot-${sqlglot_version}-py3-none-any.whl https://files.pythonhosted.org/packages/f3/bc/2a07cef49046e6cf5d1a8b1de553aaef8491b4170dbfe254c8687c764408/sqlglot-${sqlglot_version}-py3-none-any.whl"
)

for item in "${assets[@]}"; do
  read -r expected directory name url <<<"$item"
  target="$root/web/vendor/$directory/$name"
  mkdir -p "$(dirname "$target")"
  if [[ -f "$target" ]] && [[ $(sha256sum "$target" | awk '{print $1}') == "$expected" ]]; then
    printf 'verified %s\n' "$target"
    continue
  fi
  temp=$(mktemp)
  trap 'rm -f "$temp"' EXIT
  curl --fail --location --retry 3 --output "$temp" "$url"
  printf '%s  %s\n' "$expected" "$temp" | sha256sum --check --status
  mv "$temp" "$target"
  trap - EXIT
  printf 'downloaded %s\n' "$target"
done

"$root/tools/sync-analyzer.sh"
