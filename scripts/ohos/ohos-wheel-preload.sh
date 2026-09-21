#!/bin/sh
# 预装鸿蒙 native wheel（应在 manifest / openjiuwen 之前执行）
#
# 供给顺序（2026-09 评审：离线 wheel 不再入团队仓库）：
#   1. 本地 WHEEL_DIR 离线 wheel（预编 harmonyos_aarch64；自带 wheelhouse
#      的构建机导出 WHEEL_DIR 即可，行为与旧版一致）
#   2. 在线镜像下载 musllinux wheel + ohos_musl_wheel_convert.py 现场转换
#      （同余修复 + DT_NEEDED libpython + 本机签名；与 deepsearch 安装链
#      路径 3 同一机制，2026-09 真机验证）
#
# 用法:
#   PYTHON=$VENV_DIR/bin/python WHEEL_DIR=/path/to/wheelhouse sh scripts/ohos/ohos-wheel-preload.sh
#   # 无 wheelhouse 时（纯在线 + 现场转换）:
#   PYTHON=$VENV_DIR/bin/python sh scripts/ohos/ohos-wheel-preload.sh
#
# 环境变量:
#   PYTHON / WHEEL_DIR / OHOS_REAL_PYTHON / OPENSSL_DIR — 由 ohos-env.sh 解析
#   OHOS_WHEEL_PACKAGES — 空格分隔包名，默认见 OHOS_WHEEL_DEFAULT_PACKAGES
#   OHOS_WHEEL_VERSIONS — 包名==版本 映射（在线下载路径钉版，默认内置表）
#   PIP_MIRROR — pip 镜像（默认 repo.huaweicloud.com）
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

# WHEEL_DIR 可选：目录不存在走在线下载 + 现场转换路径
if [ -n "${WHEEL_DIR:-}" ] && [ ! -d "$WHEEL_DIR" ]; then
  log "WARN: WHEEL_DIR 不存在（${WHEEL_DIR}），改用在线镜像 + 现场转换"
  WHEEL_DIR=""
fi

OHOS_WHEEL_DEFAULT_PACKAGES="cryptography pydantic_core rpds_py numpy greenlet tiktoken jiter lxml lupa"
OHOS_WHEEL_PACKAGES=${OHOS_WHEEL_PACKAGES:-$OHOS_WHEEL_DEFAULT_PACKAGES}

# 在线下载路径的钉版表（与原 wheels/ 预编 wheel 版本一致；
# 第三列：musllinux 优先 tag —— cp312 ABI 轮子普遍有 1_2，abi3/py3 用 1_1）
OHOS_WHEEL_VERSIONS_DEFAULT="cryptography==48.0.0
pydantic_core==2.46.4
rpds_py==0.30.0
numpy==2.4.5
greenlet==3.5.1
tiktoken==0.13.0
jiter==0.15.0
lxml==6.1.0
lupa==2.8
Pillow==12.2.0
pandas==2.3.1
pypdfium2==4.30.0
aiohttp==3.14.1
cffi==2.0.0
pycryptodome==3.23.0
regex==2026.5.9
multidict==6.6.3
frozenlist==1.7.0
yarl==1.20.1
propcache==0.5.2
MarkupSafe==3.0.3
burner_redis==0.1.7"
OHOS_WHEEL_VERSIONS=${OHOS_WHEEL_VERSIONS:-$OHOS_WHEEL_VERSIONS_DEFAULT}
PIP_MIRROR=${PIP_MIRROR:-https://repo.huaweicloud.com/repository/pypi/simple}

CONVERTER="$SCRIPT_DIR/ohos_musl_wheel_convert.py"
[ -f "$CONVERTER" ] || CONVERTER="$SCRIPT_DIR/ohos-musl-wheel-convert.py"
MUSL_TMP="${TMPDIR:-/tmp}/ohos-musl-convert-$$"

VERIFY_WHEEL_IMPORTS=${VERIFY_WHEEL_IMPORTS:-1}
REUSE_INSTALLED=${REUSE_INSTALLED:-1}
FORCE_REINSTALL=${FORCE_REINSTALL:=0}

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

version_of() {
  # version_of <包名> — 从钉版表取 "pkg==ver"，无则输出空
  _want=$1
  _norm=$(printf '%s' "$_want" | tr '[:upper:]' '[:lower:]')
  while IFS= read -r _line; do
    [ -n "$_line" ] || continue
    _entry=${_line%%==*}
    _entry_norm=$(printf '%s' "$_entry" | tr '[:upper:]' '[:lower:]')
    if [ "$_entry_norm" = "$_norm" ]; then
      echo "$_line"
      return 0
    fi
  done <<EOF
$OHOS_WHEEL_VERSIONS
EOF
  return 1
}

download_musl_wheel() {
  # download_musl_wheel <pkg==ver> — 下载 musllinux wheel 到 MUSL_TMP，
  # 1_2 优先、1_1 兜底；成功输出文件路径
  _spec=$1
  mkdir -p "$MUSL_TMP"
  for _mplat in musllinux_1_2_aarch64 musllinux_1_1_aarch64; do
    if "$PYTHON" -m pip download --no-deps --only-binary :all: \
        --platform "$_mplat" --python-version 3.12 \
        -d "$MUSL_TMP" -i "$PIP_MIRROR" "$_spec" >/dev/null 2>&1; then
      _dlpkg=${_spec%%==*}
      _dlpkg_norm=$(printf '%s' "$_dlpkg" | tr '-' '_')
      for _f in "$MUSL_TMP"/"${_dlpkg_norm}"-*.whl; do
        [ -f "$_f" ] && { echo "$_f"; return 0; }
      done
    fi
  done
  return 1
}

convert_and_install_musl() {
  # convert_and_install_musl <pkg> — 在线下载 musl 源并现场转换安装；
  # 需要 $PYTHON 有 cryptography（转换器签名路径依赖）……实际转换器只依赖
  # stdlib + 本机 SIGN_TOOL，venv 阶段即可用。
  _pkg=$1
  _spec=$(version_of "$_pkg") || {
    log "online skip $_pkg (无钉版映射)"
    return 1
  }
  log "online: 下载 musl 源 $_spec ..."
  _src=$(download_musl_wheel "$_spec") || {
    log "online skip $_pkg (镜像无 musllinux wheel: $_spec)"
    return 1
  }
  [ -f "$CONVERTER" ] || {
    log "online skip $_pkg (转换器不存在: $CONVERTER)"
    return 1
  }
  log "online: 现场转换 $(basename "$_src")（同余修复 + libpython + 本机签名）..."
  if ! "$PYTHON" "$CONVERTER" "$_src" >/dev/null 2>&1; then
    log "online skip $_pkg (musl 转换失败)"
    return 1
  fi
  _pkg_norm=$(printf '%s' "$_pkg" | tr '-' '_')
  for _out in "$MUSL_TMP"/"${_pkg_norm}"-*harmonyos_aarch64.whl; do
    [ -f "$_out" ] || continue
    log "online: 安装转换产物 $(basename "$_out")"
    pip_wheel "$_out" || continue
    return 0
  done
  return 1
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
_online=0
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
  # --- 路径 1: 本地 wheelhouse 离线 wheel ---
  _wheel=""
  if [ -n "$WHEEL_DIR" ]; then
    _wheel=$(find_wheel_file "$_base" "$_plat") || _wheel=""
  fi
  if [ -n "$_wheel" ]; then
    log "preload wheel: $_wheel"
    pip_wheel "$_wheel" || die "$_base wheel install failed"
    _installed=$((_installed + 1))
    continue
  fi
  # --- 路径 2: 在线镜像下载 musl 源 + 现场转换 ---
  if convert_and_install_musl "$_base"; then
    _online=$((_online + 1))
    continue
  fi
  log "preload skip $_base (无本地 wheel，在线转换不可用)"
  _skipped=$((_skipped + 1))
done

log "wheels: installed=$_installed online=$_online skipped=$_skipped"
rm -rf "$MUSL_TMP" 2>/dev/null || true
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
