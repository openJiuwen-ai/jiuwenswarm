#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
output="${1:?Usage: bash deploy/yuanrong/package.sh /absolute/path/deploy.tar.gz}"
case "$output" in /*) ;; *) echo "output must be absolute" >&2; exit 1;; esac
COPYFILE_DISABLE=1 tar --exclude='__pycache__' --exclude='*.pyc' -czf "$output" -C "$root" deploy/yuanrong deploy/adx
