#!/usr/bin/env bash
set -euo pipefail

# Fetch only Aurochs' core, PPTX parser, and PPTX SVG-renderer dependency trees.
# The checkout is intentionally external to the Python package.
root="${AUROCHS_ROOT:-vendor/aurochs}"
repo="https://github.com/trkbt10/aurochs.git"

if [[ ! -d "$root/.git" ]]; then
  git clone --filter=blob:none --sparse "$repo" "$root"
fi

git -C "$root" sparse-checkout set \
  package.json bun.lock bunfig.toml tsconfig.json \
  packages/@aurochs \
  packages/@aurochs-office/chart \
  packages/@aurochs-office/diagram \
  packages/@aurochs-office/drawing-ml \
  packages/@aurochs-office/ooxml \
  packages/@aurochs-office/opc \
  packages/@aurochs-office/pptx \
  packages/@aurochs-office/vba \
  packages/@aurochs-office/xlsx \
  packages/@aurochs-renderer/chart \
  packages/@aurochs-renderer/diagram \
  packages/@aurochs-renderer/drawing-ml \
  packages/@aurochs-renderer/pptx \
  packages/@aurochs-renderer/svg

if ! command -v bun >/dev/null 2>&1; then
  printf '%s\n' "Aurochs source fetched. Install Bun 1.3.x before installing dependencies." >&2
  exit 0
fi

bun --cwd "$root" install --frozen-lockfile
