#!/usr/bin/env bash
# Run from repo root, or any cwd — resolves this script's directory (skill name uses underscores).
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <file-or-dir>"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/validate_trace_points.py" "$1"
