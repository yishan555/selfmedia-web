#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root"
exec python3 -B -m apps.workbench --workspace "$root" --host codex --skills "$root/bundles/codex" serve --port "${1:-5051}"
