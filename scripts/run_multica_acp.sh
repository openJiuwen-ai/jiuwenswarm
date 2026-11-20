#!/bin/sh
set -eu

repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
venv_bin="$repo_dir/.venv/bin"

case "${1-}" in
    --version)
        exec "$venv_bin/python" -c 'from jiuwenswarm.common.version import get_runtime_version; print(get_runtime_version())'
        ;;
    acp)
        shift
        exec "$venv_bin/jiuwenswarm-acp" "$@"
        ;;
    *)
        printf 'Usage: %s --version | acp [--gateway-url URL]\n' "$0" >&2
        exit 2
        ;;
esac
