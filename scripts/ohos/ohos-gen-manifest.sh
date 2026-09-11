#!/bin/sh
# 安装时动态生成 manifest TSV（替代仓库内 agentserver/agentcore-minimal-manifest.tsv）
#
# 用法:
#   sh scripts/ohos/ohos-gen-manifest.sh agentserver-minimal /path/to/out.tsv
#   sh scripts/ohos/ohos-gen-manifest.sh agentcore-minimal /path/to/out.tsv
#
# 数据源:
#   agentserver-minimal — requirements-minimal.txt + native 传递依赖
#   agentcore-minimal   — agent-core/harmonyos/pyproject.toml − requirements-minimal

set -u

PROFILE="${1:-}"
OUT="${2:-}"

[ -n "$PROFILE" ] || { echo "usage: ohos-gen-manifest.sh <profile> <out.tsv>" >&2; exit 1; }
[ -n "$OUT" ] || { echo "usage: ohos-gen-manifest.sh <profile> <out.tsv>" >&2; exit 1; }

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
export OHOS_ENV_SCRIPTS_DIR="$SCRIPT_DIR"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/ohos-env.sh"

REPO_ROOT=${OHOS_REPO_ROOT:-$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)}
export OHOS_REPO_ROOT="$REPO_ROOT"
GEN_PY="${OHOS_DEPS_MANIFEST_PY:-$SCRIPT_DIR/ohos-deps-manifest.py}"
REQ="${REQUIREMENTS_MINIMAL:-$REPO_ROOT/requirements-minimal.txt}"
PY="${OHOS_MANIFEST_PYTHON:-${PYTHON:-${OHOS_REAL_PYTHON:-python3}}}"

resolve_harmonyos_pyproject() {
  for _p in \
    "${HARMONYOS_PYPROJECT:-}" \
    "${OPENJIUWEN_SRC_DIR:-$REPO_ROOT/.cache/openjiuwen-src}/harmonyos/pyproject.toml" \
    "${AGENT_CORE_PATH:-}/harmonyos/pyproject.toml" \
    "${OFFICE_CLAW:-}/agent-core/harmonyos/pyproject.toml" \
    "${OFFICE_CLAW:-}/agent-core_5969/harmonyos/pyproject.toml"; do
    [ -n "$_p" ] || continue
    [ -f "$_p" ] || continue
    echo "$_p"
    return 0
  done
  return 1
}

[ -f "$GEN_PY" ] || { echo "ERROR: $GEN_PY not found" >&2; exit 1; }

_extra_args=
if [ "$PROFILE" = "agentcore-minimal" ]; then
  if _hp=$(resolve_harmonyos_pyproject); then
    _extra_args="--harmonyos-pyproject $_hp"
  fi
fi

# shellcheck disable=SC2086
OHOS_REPO_ROOT="$REPO_ROOT" \
OFFICE_CLAW="${OFFICE_CLAW:-}" \
AGENT_CORE_PATH="${AGENT_CORE_PATH:-}" \
OPENJIUWEN_SRC_DIR="${OPENJIUWEN_SRC_DIR:-}" \
MANIFEST_OUT="$OUT" \
"$PY" "$GEN_PY" \
  --profile "$PROFILE" \
  --requirements "$REQ" \
  $_extra_args >&2

[ -f "$OUT" ] || { echo "ERROR: manifest not written: $OUT" >&2; exit 1; }
printf 'generated %s (%s lines)\n' "$OUT" "$(wc -l <"$OUT" | tr -d ' ')" >&2
exit 0
