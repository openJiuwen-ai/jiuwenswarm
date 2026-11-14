#!/usr/bin/env bash
# Compatibility entrypoint; the deployment implementation lives in ../yuanrong.
set -euo pipefail
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")/../yuanrong" && pwd)/deploy.sh" "$@"
