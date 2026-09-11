#!/bin/sh
# 预装 WHEEL_DIR 中的鸿蒙 native wheel（应在 manifest / openjiuwen 之前执行）
#
# 用法:
#   PYTHON=$VENV_DIR/bin/python WHEEL_DIR=$REPO_ROOT/wheels sh scripts/ohos/ohos-wheel-preload.sh
#
# 环境变量:
#   PYTHON / WHEEL_DIR / OHOS_REAL_PYTHON / OPENSSL_DIR — 由 ohos-env.sh 解析
#   OHOS_WHEEL_PACKAGES — 空格分隔包名，默认见 OHOS_WHEEL_DEFAULT_PACKAGES
#   VERIFY_WHEEL_IMPORTS — 1 时对 cryptography/lupa 做 import 探针（默认 1）
#   REPORT_DIR — 可选，写入 native-import-*.log

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
export OHOS_ENV_SCRIPTS_DIR="$SCRIPT_DIR"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/ohos-env.sh"

log() {
  printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$1"
}

die() {
  log "ERROR: $*"
  exit 1
}

[ -n "${PYTHON:-}" ] || die "set PYTHON=.../venv/bin/python"

FIND_WHEEL="${OHOS_FIND_WHEEL_SCRIPT:-$SCRIPT_DIR/find-ohos-wheel.sh}"
[ -f "$FIND_WHEEL" ] || die "find-ohos-wheel.sh not found: $FIND_WHEEL"
[ -n "${WHEEL_DIR:-}" ] && [ -d "$WHEEL_DIR" ] || die "WHEEL_DIR not found: ${WHEEL_DIR:-unset}"

OHOS_WHEEL_DEFAULT_PACKAGES="cryptography pydantic_core rpds_py numpy greenlet tiktoken jiter lxml lupa"
OHOS_WHEEL_PACKAGES=${OHOS_WHEEL_PACKAGES:-$OHOS_WHEEL_DEFAULT_PACKAGES}
VERIFY_WHEEL_IMPORTS=${VERIFY_WHEEL_IMPORTS:-1}
REUSE_INSTALLED=${REUSE_INSTALLED:-1}
FORCE_REINSTALL=${FORCE_REINSTALL:-0}

pip_wheel() {
  "$PYTHON" -m pip install --no-cache-dir --force-reinstall --no-deps "$@"
}

detect_wheel_platform_tag() {
  # 优先检测 WHEEL_DIR 里实际存在的 ABI，避免维护两套 wheel
  # 如果 WHEEL_DIR 里有 harmonyos_aarch64 的 wheel，优先用它（统一 ABI）
  if [ -n "${WHEEL_DIR:-}" ] && [ -d "$WHEEL_DIR" ]; then
    if ls "$WHEEL_DIR"/*-harmonyos_aarch64.whl >/dev/null 2>&1; then
      echo "harmonyos_aarch64"
      return
    fi
    if ls "$WHEEL_DIR"/*-ohos_aarch64.whl >/dev/null 2>&1; then
      echo "ohos_aarch64"
      return
    fi
  fi
  # fallback：问当前 Python 接受什么 ABI
  "$PYTHON" -c "
import re, subprocess, sys
text = subprocess.check_output(
    [sys.executable, '-m', 'pip', 'debug', '--verbose'],
    stderr=subprocess.STDOUT, text=True, errors='replace',
)
tags = re.findall(r'cp\d+-cp\d+-(\S+)', text)
for prefer in ('harmonyos_aarch64', 'ohos_aarch64'):
    if prefer in tags:
        print(prefer)
        break
else:
    for t in tags:
        if 'ohos' in t or 'harmony' in t:
            print(t)
            break
    else:
        print(tags[0] if tags else 'harmonyos_aarch64')
" 2>/dev/null || echo "harmonyos_aarch64"
}

find_wheel_file() {
  _pkg=$1
  _plat=$2
  _base=$(printf '%s' "$_pkg" | tr '[:upper:]' '[:lower:]' | tr '-' '_')
  for _w in \
    "$WHEEL_DIR"/${_base}-*-"${_plat}".whl \
    "$WHEEL_DIR"/${_base}-*-ohos_aarch64.whl \
    "$WHEEL_DIR"/${_base}-*-aarch64*.whl \
    "$WHEEL_DIR"/${_base}-*.whl; do
    if [ -f "$_w" ]; then
      echo "$_w"
      return 0
    fi
  done
  _w=$(sh "$FIND_WHEEL" "$_pkg" 2>/dev/null) || return 1
  [ -f "$_w" ] && echo "$_w"
}

verify_openssl_for_cryptography() {
  if [ -z "${OPENSSL_DIR:-}" ] || [ ! -d "${OPENSSL_DIR}/lib" ]; then
    die "OPENSSL_DIR 未找到 — 请确认 cmd-pkgs openssl 已安装到 ~/usr/local"
  fi
  _ssl="${OPENSSL_DIR}/lib/libssl.so.3"
  [ -f "$_ssl" ] || _ssl="${OPENSSL_DIR}/lib/libssl.so"
  [ -f "$_ssl" ] || die "未找到 libssl: ${OPENSSL_DIR}/lib"
  log "libssl: $_ssl"
}

run_import_probe() {
  _pkg=$1
  _import_py=$2
  _detected_ld=$(ohos_native_ld_library_path)
  # Keep the parent shell's known-good HNP runtime first. The detected list
  # is an augmentation, not a replacement for that environment.
  _native_ld=${LD_LIBRARY_PATH:-}
  if [ -n "$_detected_ld" ]; then
    _native_ld="${_native_ld:+${_native_ld}:}${_detected_ld}"
  fi
  mkdir -p "${REPORT_DIR:-/dev/null}"
  _log="${REPORT_DIR:-.}/native-import-${_pkg}.log"
  # OhOS/HNP Python needs the complete parent shell environment. Do not use
  # an external env wrapper here, because this phase runs before dependency
  # installation and a loader failure would abort the entire install.
  log "import probe LD_LIBRARY_PATH=$_native_ld"
  if LD_LIBRARY_PATH="$_native_ld" OPENSSL_DIR="${OPENSSL_DIR:-}" \
    "$PYTHON" -c "$_import_py" >>"$_log" 2>&1; then
    log "import $_pkg: OK"
    return 0
  fi
  log "import $_pkg: FAIL"
  LD_LIBRARY_PATH="$_native_ld" OPENSSL_DIR="${OPENSSL_DIR:-}" \
    "$PYTHON" -c "$_import_py" 2>&1 | while IFS= read -r _line; do log "  $_line"; done
  return 1
}

normalize_native_extension_permissions() {
  _site_packages=$("$PYTHON" -c 'import site; print(site.getsitepackages()[0])' 2>/dev/null || true)
  if [ -z "$_site_packages" ] || [ ! -d "$_site_packages" ]; then
    log "WARN: skip native permission normalization (site-packages unavailable)"
    return 0
  fi

  # Some HNP deployment paths retain wheels with non-executable .so files.
  # Normalize only the virtual environment after wheel installation.
  if find "$_site_packages" -type f -name '*.so*' ! -perm -111 -exec chmod 755 {} \; 2>/dev/null \
    && find "$_site_packages" -type d ! -perm -111 -exec chmod 755 {} \; 2>/dev/null; then
    log "native extension permissions normalized: $_site_packages"
  else
    log "WARN: unable to normalize native extension permissions: $_site_packages"
  fi
}

log "======== preload ohos wheels (first) ========"
log "WHEEL_DIR=$WHEEL_DIR"
log "PYTHON=$PYTHON"
_plat=$(detect_wheel_platform_tag)
log "platform tag: $_plat"

_installed=0
_skipped=0
for _base in $OHOS_WHEEL_PACKAGES; do
  _import_mod=$_base
  case $_base in
    rpds_py) _import_mod=rpds ;;
    lupa) _import_mod=lupa.luajit21 ;;
  esac
  if [ "$REUSE_INSTALLED" = "1" ] && [ "$FORCE_REINSTALL" != "1" ] \
    && "$PYTHON" -c "import $_import_mod" >/dev/null 2>&1; then
    log "preload skip installed: $_base"
    _skipped=$((_skipped + 1))
    continue
  fi
  _wheel=$(find_wheel_file "$_base" "$_plat") || {
    log "preload skip $_base (no wheel in $WHEEL_DIR)"
    _skipped=$((_skipped + 1))
    continue
  }
  log "preload wheel: $_wheel"
  pip_wheel "$_wheel" || die "$_base wheel install failed"
  _installed=$((_installed + 1))
done

log "wheels: installed=$_installed skipped=$_skipped"
normalize_native_extension_permissions

if [ "$VERIFY_WHEEL_IMPORTS" = "1" ]; then
  if find_wheel_file cryptography "$_plat" >/dev/null 2>&1; then
    verify_openssl_for_cryptography
    run_import_probe cryptography \
      "import cryptography; print('cryptography OK', cryptography.__version__)" \
      || die "cryptography wheel import failed"
  fi
  if find_wheel_file lupa "$_plat" >/dev/null 2>&1; then
    run_import_probe lupa \
      "import lupa.luajit21 as lupa; print('lupa OK', lupa.LuaRuntime().eval('1+1'))" \
      || log "WARN: lupa wheel import failed (optional)"
  fi
fi

# pydantic 纯 Python 层在 pydantic_core wheel 之后补装
if find_wheel_file pydantic_core "$_plat" >/dev/null 2>&1; then
  if [ "$REUSE_INSTALLED" = "1" ] && [ "$FORCE_REINSTALL" != "1" ] \
    && "$PYTHON" -c "import pydantic" >/dev/null 2>&1; then
    log "post-wheel skip installed: pydantic"
  else
    log "post-wheel: pydantic (after pydantic_core wheel)"
    "$PYTHON" -m pip install --no-cache-dir "pydantic>=2.11" >/dev/null 2>&1 || true
  fi
fi

log "preload ohos wheels: done"
exit 0
