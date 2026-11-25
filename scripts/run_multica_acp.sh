#!/bin/sh
set -eu

repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
if [ "${1-}" = acp ]; then
    shift
fi
exec "$repo_dir/.venv/bin/jiuwenswarm-acp" "$@"
