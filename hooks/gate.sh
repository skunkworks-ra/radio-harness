#!/usr/bin/env bash
set -uo pipefail
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HOOK_DIR/.." && pwd)"
exec pixi run --manifest-path "$REPO_ROOT/pixi.toml" --quiet python "$HOOK_DIR/gate.py"
