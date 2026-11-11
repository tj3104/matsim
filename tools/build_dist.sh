#!/usr/bin/env bash
# 3 つのパッケージの wheel と sdist を dist/ に作る (uv を使う。pip なら python -m build)
set -euo pipefail
cd "$(dirname "$0")/.."
for package in atomsk_kit matsimapi matsimapp; do
  uv build --out-dir dist "packages/$package"
done
