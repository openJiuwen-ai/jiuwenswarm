#!/bin/sh
# 在 WHEEL_DIR 中查找指定 Python 包的 .whl 文件
#
# 用法:
#   sh scripts/find-ohos-wheel.sh <package_name>
#   sh scripts/find-ohos-wheel.sh cryptography
#
# 输出: 匹配的 .whl 完整路径（第一个），未找到则退出码 1
#
# 搜索策略:
#   1. WHEEL_DIR 环境变量（默认 $REPO_ROOT/wheels）
#   2. 支持标准 platform tag（manylinux/openharmony/ohos_aarch64）

set -u

PKG="${1:-}"
[ -n "$PKG" ] || { echo "usage: find-ohos-wheel.sh <package_name>" >&2; exit 1; }

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
export OHOS_ENV_SCRIPTS_DIR="$SCRIPT_DIR"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/ohos-env.sh"
REPO_ROOT=${OHOS_REPO_ROOT:-$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)}
WHEEL_DIR="${WHEEL_DIR:-$REPO_ROOT/wheels}"

if [ ! -d "$WHEEL_DIR" ]; then
  echo "WHEEL_DIR not found: $WHEEL_DIR" >&2
  exit 1
fi

# 规范化包名: pip 包名 → wheel 文件名前缀
#   cryptography → cryptography
#   pydantic-core → pydantic_core
#   SQLAlchemy → SQLAlchemy
_normalized=$(printf '%s' "$PKG" | tr '[:upper:]' '[:lower:]' | tr '-' '_')

# 搜索顺序: 精确名 → 规范化名
for _pattern in "$PKG" "$PKG" "$_normalized"; do
  for _whl in \
    "$WHEEL_DIR"/${_pattern}-*any*.whl \
    "$WHEEL_DIR"/${_pattern}-*aarch64*.whl \
    "$WHEEL_DIR"/${_pattern}-*ohos*.whl \
    "$WHEEL_DIR"/${_pattern}-*linux*.whl \
    "$WHEEL_DIR"/${_pattern}-*.whl; do
    if [ -f "$_whl" ]; then
      echo "$_whl"
      exit 0
    fi
  done
done

echo "no $PKG wheel in $WHEEL_DIR" >&2
exit 1
