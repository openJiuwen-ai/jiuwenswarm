#!/usr/bin/env bash
set -euo pipefail
VENV="${SKILLSBENCH_WSL_VENV:-$HOME/.cache/openjiuwen-skillsbench-verifier}"
PY="$VENV/bin/python"
PIP_INDEX="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
PIP_HOST="${PIP_TRUSTED_HOST:-mirrors.aliyun.com}"
# Official get-pip (supports py3.14+); package installs still use Aliyun.
GET_PIP_URL="${GET_PIP_URL:-https://bootstrap.pypa.io/get-pip.py}"
export PIP_INDEX_URL="$PIP_INDEX"
export PIP_TRUSTED_HOST="$PIP_HOST"
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy || true
rm -rf "$VENV"
mkdir -p "$(dirname "$VENV")"
python3 -m venv --without-pip "$VENV" 2>/dev/null || python3 -m venv "$VENV"
if [[ ! -x "$PY" ]]; then echo "venv missing"; exit 1; fi
if ! "$PY" -m pip --version >/dev/null 2>&1; then
  echo "Downloading get-pip from $GET_PIP_URL ..."
  curl -fsSL "$GET_PIP_URL" -o /tmp/get-pip.py
  "$PY" /tmp/get-pip.py --index-url "$PIP_INDEX" --trusted-host "$PIP_HOST"
fi
echo "Installing pytest via $PIP_INDEX ..."
"$PY" -m pip install -q --disable-pip-version-check --index-url "$PIP_INDEX" --trusted-host "$PIP_HOST" pytest==9.0.2
"$PY" -c "import pytest; print('pytest', pytest.__version__, 'ok')"
