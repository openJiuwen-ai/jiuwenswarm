#!/bin/sh
# OfficeClaw / JiuwenClaw 鸿蒙公共环境（PATH、Python、wheel、OpenSSL、运行时 LD_LIBRARY_PATH）
#
# 自动：install-ohos-*.sh 会 source；patch_venv_activate_ohos 写入 .venv/bin/activate
# 手动：. ~/officeClaw/jiuwenswarm/scripts/ohos-env.sh
#
# 机器专属覆盖（可选，勿提交 git）：同目录 ohos-env.local.sh

# ---------- 可配置路径（机器专属见 ohos-env.local.sh）----------
OHOS_HNP_ROOT=${OHOS_HNP_ROOT:-/data/service/hnp}
OHOS_HNP_BIN=${OHOS_HNP_BIN:-$OHOS_HNP_ROOT/bin}
OHOS_HNP_LIB=${OHOS_HNP_LIB:-$OHOS_HNP_ROOT/lib}
OHOS_HNP_PYTHON=${OHOS_HNP_PYTHON:-$OHOS_HNP_ROOT/python.org/python_3.12/bin/python3.12}
OHOS_STORAGE_ROOT=${OHOS_STORAGE_ROOT:-/storage/Users/currentUser}
OHOS_USR_LOCAL=${OHOS_USR_LOCAL:-$HOME/usr/local}
OHOS_RUST_BIN=${OHOS_RUST_BIN:-$HOME/usr/rust-1.95.0-aarch64-unknown-linux-ohos/bin}
# 空格分隔；未设置时 ohos_find_git 使用内置候选
OHOS_GIT_SEARCH_PATHS=${OHOS_GIT_SEARCH_PATHS:-}
# 空格分隔 HNP cmd-pkgs 包根目录（libxml2/libxslt 等）
OHOS_HNP_PKG_ROOTS=${OHOS_HNP_PKG_ROOTS:-}

_ohos_prepend_path() {
  _dir=$1
  [ -n "$_dir" ] || return 0
  [ -d "$_dir" ] || return 0
  case ":${PATH}:" in
    *":$_dir:"*) ;;
    *) PATH="$_dir:${PATH:-}" ;;
  esac
}

_ohos_prepend_ld() {
  _dir=$1
  [ -n "$_dir" ] || return 0
  [ -d "$_dir" ] || return 0
  case ":${LD_LIBRARY_PATH:-}:" in
    *":$_dir:"*) ;;
    *) LD_LIBRARY_PATH="${_dir}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}" ;;
  esac
}

_ohos_force_prepend_ld() {
  _dir=$1
  [ -n "$_dir" ] && [ -d "$_dir" ] || return 0
  _cur=${LD_LIBRARY_PATH:-}
  _out=
  _old_ifs=$IFS
  IFS=:
  # shellcheck disable=SC2086
  set -- $_cur
  IFS=$_old_ifs
  for _p in "$@"; do
    [ -n "$_p" ] || continue
    [ "$_p" = "$_dir" ] && continue
    case ":${_out}:" in
      *":$_p:"*) ;;
      *) _out="${_out:+${_out}:}${_p}" ;;
    esac
  done
  LD_LIBRARY_PATH="$_dir${_out:+:${_out}}"
  export LD_LIBRARY_PATH
}

_ohos_resolve_scripts_dir() {
  if [ -n "${OHOS_ENV_SCRIPTS_DIR:-}" ] && [ -d "$OHOS_ENV_SCRIPTS_DIR" ]; then
    CDPATH= cd -- "$OHOS_ENV_SCRIPTS_DIR" && pwd
    return 0
  fi
  _d=$(dirname "$0")
  case "$_d" in
    */scripts/ohos|*/scripts)
      CDPATH= cd -- "$_d" && pwd
      return 0
      ;;
  esac
  # source 场景: $0 不可靠, 尝试从 BASH_SOURCE 获取
  if [ -n "${BASH_SOURCE[0]:-}" ]; then
    _bd=$(dirname "${BASH_SOURCE[0]}")
    case "$_bd" in
      */scripts/ohos|*/scripts)
        CDPATH= cd -- "$_bd" && pwd
        return 0
        ;;
    esac
  fi
  return 1
}

_ohos_discover_llvm_bin() {
  if [ -n "${OHOS_LLVM_BIN:-}" ] && [ -d "$OHOS_LLVM_BIN" ]; then
    echo "$OHOS_LLVM_BIN"
    return 0
  fi
  for _d in \
    "$OHOS_HNP_ROOT"/ohos-sdk.org/ohos-sdk_*/ohos/native/llvm/bin \
    "$OHOS_HNP_ROOT"/ohos-sdk*/ohos/native/llvm/bin; do
    if [ -d "$_d" ]; then
      echo "$_d"
      return 0
    fi
  done
  return 1
}

_ohos_office_claw_candidates() {
  if [ -n "${OHOS_OFFICE_CLAW_CANDIDATES:-}" ]; then
    printf '%s\n' $OHOS_OFFICE_CLAW_CANDIDATES
    return 0
  fi
  printf '%s\n' \
    "$OHOS_STORAGE_ROOT/officeClaw" \
    "$HOME/officeClaw"
}

_ohos_resolve_office_claw() {
  if [ -n "${OFFICE_CLAW:-}" ] && [ -d "$OFFICE_CLAW" ]; then
    echo "$OFFICE_CLAW"
    return 0
  fi
  if [ -n "${OHOS_REPO_ROOT:-}" ]; then
    _oc=$(CDPATH= cd -- "$OHOS_REPO_ROOT/.." 2>/dev/null && pwd || true)
    if [ -n "$_oc" ]; then
      echo "$_oc"
      return 0
    fi
  fi
  _oc=$(_ohos_office_claw_candidates | while IFS= read -r _c; do
    [ -n "$_c" ] || continue
    [ -d "$_c" ] && echo "$_c" && break
  done)
  if [ -n "$_oc" ]; then
    echo "$_oc"
    return 0
  fi
  echo "$OHOS_STORAGE_ROOT/officeClaw"
}

_ohos_resolve_real_python() {
  if [ -n "${OHOS_REAL_PYTHON:-}" ] && [ -x "$OHOS_REAL_PYTHON" ]; then
    echo "$OHOS_REAL_PYTHON"
    return 0
  fi
  for _p in \
    "${OHOS_USR_LOCAL}/bin/python3.12" \
    "$HOME/usr/local/bin/python3.12" \
    "$OHOS_HNP_PYTHON" \
    "$(command -v python3.12 2>/dev/null || true)" \
    "$(command -v python3 2>/dev/null || true)"
  do
    [ -n "$_p" ] || continue
    [ -x "$_p" ] || continue
    readlink -f "$_p" 2>/dev/null || echo "$_p"
    return 0
  done
  echo "${OHOS_USR_LOCAL}/bin/python3.12"
}

_ohos_resolve_openssl_dir() {
  if [ -n "${OPENSSL_DIR:-}" ] && [ -d "$OPENSSL_DIR" ]; then
    echo "$OPENSSL_DIR"
    return 0
  fi
  for _o in \
    "$OHOS_USR_LOCAL" \
    "$HOME/usr/local" \
    "$HOME/usr/openssl" \
    "$HOME/.cmd-pkgs/openssl" \
    "${OHOS_STORAGE_ROOT}/usr/local" \
    "/usr/local"; do
    if [ -f "$_o/lib/pkgconfig/openssl.pc" ]; then
      echo "$_o"
      return 0
    fi
  done
  for _o in \
    "$OHOS_USR_LOCAL" \
    "$HOME/usr/local" \
    "${OHOS_STORAGE_ROOT}/usr/local" \
    "/usr/local"; do
    if [ -f "$_o/lib/libssl.so.3" ] || [ -f "$_o/lib/libssl.so" ] 2>/dev/null; then
      echo "$_o"
      return 0
    fi
  done
  return 1
}

_ohos_export_ld_runtime() {
  _py=${OHOS_REAL_PYTHON:-}
  _libdir=
  _py_libdir=
  # 先定位 python 自身的 lib 目录（libpython3.12.so 所在），必须排在最前
  if [ -n "$_py" ] && [ -x "$_py" ]; then
    _py_libdir=$(CDPATH= cd -- "$(dirname "$_py")/../lib" 2>/dev/null && pwd || true)
    if [ -n "${_py_libdir:-}" ] && ! [ -d "$_py_libdir" ]; then
      _py_libdir=
    fi
    _libdir=$("$_py" -c 'import sysconfig; print(sysconfig.get_config_var("LIBDIR") or "")' 2>/dev/null || true)
  fi
  # 顺序：python lib（libpython）> OPENSSL > sysconfig LIBDIR > HNP/usr-local
  _front=
  if [ -n "${_py_libdir:-}" ]; then
    _front="$_py_libdir"
  fi
  if [ -n "${OPENSSL_DIR:-}" ] && [ -d "${OPENSSL_DIR}/lib" ]; then
    case ":${_front:-}:" in
      *":${OPENSSL_DIR}/lib:"*) ;;
      *) _front="${_front:+${_front}:}${OPENSSL_DIR}/lib" ;;
    esac
  fi
  if [ -n "$_libdir" ] && [ -d "$_libdir" ]; then
    case ":${_front:-}:" in
      *":$_libdir:"*) ;;
      *) _front="${_front:+${_front}:}${_libdir}" ;;
    esac
  fi
  _tail=
  for _d in "$HOME/usr/local/lib" "$OHOS_HNP_LIB"; do
    [ -d "$_d" ] || continue
    case ":${_front}:${LD_LIBRARY_PATH:-}:" in
      *":$_d:"*) continue ;;
    esac
    _tail="${_tail:+${_tail}:}${_d}"
  done
  if [ -n "$_front" ]; then
    LD_LIBRARY_PATH="${_front}${_tail:+:${_tail}}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
  elif [ -n "$_tail" ]; then
    LD_LIBRARY_PATH="${_tail}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
  fi
  _ohos_force_prepend_ld "${_py_libdir:-${_libdir:-}}"
}

# HNP cmd-pkgs 包目录（libxml2/libxslt 等），供 install-ohos-all-deps 探测
ohos_hnp_pkg_glob_roots() {
  if [ -n "${OHOS_HNP_PKG_ROOTS:-}" ]; then
    printf '%s\n' $OHOS_HNP_PKG_ROOTS
    return 0
  fi
  for _pattern in \
    "$OHOS_HNP_ROOT/libxml2.org/libxml2_"* \
    "$OHOS_HNP_ROOT/libxslt.org/libxslt_"*; do
    [ -d "$_pattern" ] || continue
    printf '%s\n' "$_pattern"
  done
}

ohos_find_git() {
  if [ -n "${GIT_EXECUTABLE:-}" ] && [ -f "$GIT_EXECUTABLE" ]; then
    echo "$GIT_EXECUTABLE"
    return 0
  fi
  if command -v git >/dev/null 2>&1; then
    command -v git
    return 0
  fi
  if [ -n "${OHOS_GIT_SEARCH_PATHS:-}" ]; then
    for _g in $OHOS_GIT_SEARCH_PATHS; do
      if [ -f "$_g" ]; then
        echo "$_g"
        return 0
      fi
    done
  fi
  for _g in \
    "$OHOS_HNP_BIN/git" \
    "$HOME/usr/local/bin/git" \
    "$HOME/bin/git" \
    /usr/local/bin/git \
    /usr/bin/git; do
    if [ -f "$_g" ]; then
      echo "$_g"
      return 0
    fi
  done
  return 1
}

# cryptography / greenlet 等 native import 探针用（与上面 LD 顺序一致：python lib > OPENSSL > LIBDIR）
ohos_native_ld_library_path() {
  _py=${OHOS_REAL_PYTHON:-}
  _libdir=
  _py_libdir=
  if [ -n "$_py" ] && [ -x "$_py" ]; then
    _py_libdir=$(CDPATH= cd -- "$(dirname "$_py")/../lib" 2>/dev/null && pwd || true)
    if [ -n "${_py_libdir:-}" ] && ! [ -d "$_py_libdir" ]; then
      _py_libdir=
    fi
    _libdir=$("$_py" -c 'import sysconfig; print(sysconfig.get_config_var("LIBDIR") or "")' 2>/dev/null || true)
  fi
  _ld=
  if [ -n "${_py_libdir:-}" ]; then
    _ld="$_py_libdir"
  fi
  if [ -n "${OPENSSL_DIR:-}" ] && [ -d "${OPENSSL_DIR}/lib" ]; then
    _ld="${_ld:+${_ld}:}${OPENSSL_DIR}/lib"
  fi
  if [ -n "$_libdir" ] && [ -d "$_libdir" ]; then
    case ":${_ld:-}:" in
      *":$_libdir:"*) ;;
      *) _ld="${_ld:+${_ld}:}${_libdir}" ;;
    esac
  fi
  if [ -n "$_ld" ] && [ -n "${LD_LIBRARY_PATH:-}" ]; then
    printf '%s:%s' "$_ld" "$LD_LIBRARY_PATH"
  elif [ -n "$_ld" ]; then
    printf '%s' "$_ld"
  else
    printf '%s' "${LD_LIBRARY_PATH:-}"
  fi
}

# ---------- 目录 ----------
_ohos_sd=$(_ohos_resolve_scripts_dir 2>/dev/null || true)
if [ -n "$_ohos_sd" ]; then
  OHOS_ENV_SCRIPTS_DIR="$_ohos_sd"
  export OHOS_ENV_SCRIPTS_DIR
  OHOS_REPO_ROOT=${OHOS_REPO_ROOT:-$(CDPATH= cd -- "$_ohos_sd/../.." && pwd)}
  export OHOS_REPO_ROOT
fi

# 兜底：如果 _ohos_resolve_scripts_dir 没找到（被 source 时 $0 不可靠），用 OFFICE_CLAW/jiuwenswarm
if [ -z "${OHOS_REPO_ROOT:-}" ]; then
  _oc=$(_ohos_resolve_office_claw)
  for _jr in "$_oc/jiuwenswarm" "$_oc/jiuwenswarm_enterprise_dev"; do
    if [ -d "$_jr" ]; then
      OHOS_REPO_ROOT="$_jr"
      export OHOS_REPO_ROOT
      break
    fi
  done
fi

OFFICE_CLAW=$(_ohos_resolve_office_claw)
export OFFICE_CLAW

# WHEEL_DIR: 优先级: 环境变量 > $REPO_ROOT/wheels > $OFFICE_CLAW/ohos-wheel-build/wheels
if [ -n "${OHOS_REPO_ROOT:-}" ] && [ -d "$OHOS_REPO_ROOT/wheels" ]; then
  export WHEEL_BUILD_ROOT="${WHEEL_BUILD_ROOT:-$OHOS_REPO_ROOT}"
  export WHEEL_DIR="${WHEEL_DIR:-$OHOS_REPO_ROOT/wheels}"
elif [ -n "${OFFICE_CLAW:-}" ] && [ -d "$OFFICE_CLAW/ohos-wheel-build/wheels" ]; then
  export WHEEL_BUILD_ROOT="${WHEEL_BUILD_ROOT:-$OFFICE_CLAW/ohos-wheel-build}"
  export WHEEL_DIR="${WHEEL_DIR:-$WHEEL_BUILD_ROOT/wheels}"
else
  export WHEEL_BUILD_ROOT="${WHEEL_BUILD_ROOT:-${OHOS_REPO_ROOT:-$OFFICE_CLAW/jiuwenswarm}}"
  export WHEEL_DIR="${WHEEL_DIR:-$WHEEL_BUILD_ROOT/wheels}"
fi

OHOS_REAL_PYTHON=$(_ohos_resolve_real_python)
export OHOS_REAL_PYTHON

OPENSSL_DIR=$(_ohos_resolve_openssl_dir 2>/dev/null || true)
export OPENSSL_DIR
if [ -n "${OPENSSL_DIR:-}" ] && [ -d "${OPENSSL_DIR}/lib/pkgconfig" ]; then
  case ":${PKG_CONFIG_PATH:-}:" in
    *":${OPENSSL_DIR}/lib/pkgconfig:"*) ;;
    *) PKG_CONFIG_PATH="${OPENSSL_DIR}/lib/pkgconfig${PKG_CONFIG_PATH:+:${PKG_CONFIG_PATH}}" ;;
  esac
  export PKG_CONFIG_PATH
fi

# LLVM / Rust（OHOS_LLVM_BIN 可在 local 覆盖；否则自动发现 HNP ohos-sdk）
OHOS_LLVM_BIN=$(_ohos_discover_llvm_bin 2>/dev/null || true)
export OHOS_LLVM_BIN
export OHOS_HNP_ROOT OHOS_HNP_BIN OHOS_HNP_LIB OHOS_STORAGE_ROOT OHOS_USR_LOCAL OHOS_RUST_BIN

# ---------- 工具 PATH ----------
_ohos_prepend_path "$OHOS_HNP_BIN"
[ -n "${OHOS_LLVM_BIN:-}" ] && _ohos_prepend_path "$OHOS_LLVM_BIN"
_ohos_prepend_path "$WHEEL_BUILD_ROOT/bin"
_ohos_prepend_path "$WHEEL_BUILD_ROOT/cargo/bin"
_ohos_prepend_path "${OHOS_REPO_ROOT:-}/scripts"
_ohos_prepend_path "$HOME/.cargo/bin"
_ohos_prepend_path "$OHOS_USR_LOCAL/bin"
_ohos_prepend_path "$HOME/usr/local/bin"
_ohos_prepend_path "$OHOS_RUST_BIN"
_ohos_prepend_path "$HOME/bin"
_ohos_prepend_path /usr/local/bin
_ohos_prepend_path /usr/bin
_ohos_prepend_path /bin

# ---------- 运行时 / 编译库路径 ----------
_ohos_export_ld_runtime
export LD_LIBRARY_PATH

export PATH

# ---------- git ----------
if [ -z "${GIT_EXECUTABLE:-}" ]; then
  if [ -f "$OHOS_HNP_BIN/git" ]; then
    GIT_EXECUTABLE="$OHOS_HNP_BIN/git"
    export GIT_EXECUTABLE
  fi
fi

# ---------- pip / 安装默认值 ----------
export PIP_NO_BUILD_ISOLATION="${PIP_NO_BUILD_ISOLATION:-1}"

# relay-claw sidecar 常用（未设置时才默认）
export OFFICE_CLAW_RELAYCLAW_PYTHON="${OFFICE_CLAW_RELAYCLAW_PYTHON:-${OHOS_REPO_ROOT:-$OFFICE_CLAW/jiuwenswarm}/.venv/bin/python3.12}"

# ---------- 可选本地覆盖 ----------
if [ -n "${OHOS_ENV_SCRIPTS_DIR:-}" ] && [ -f "$OHOS_ENV_SCRIPTS_DIR/ohos-env.local.sh" ]; then
  # shellcheck disable=SC1091
  . "$OHOS_ENV_SCRIPTS_DIR/ohos-env.local.sh"
fi

patch_venv_activate_ohos() {
  _venv=${1:-${VENV_DIR:-}}
  _act="${_venv}/bin/activate"
  [ -f "$_act" ] || return 0
  _marker="# ohos-env-snippet"
  grep -q "$_marker" "$_act" 2>/dev/null && return 0
  cat >>"$_act" <<'EOF'

# ohos-env-snippet — source scripts/ohos/ohos-env.sh（PATH / OHOS_REAL_PYTHON / WHEEL_DIR / LD_LIBRARY_PATH）
if [ -n "${VIRTUAL_ENV:-}" ] && [ -f "${VIRTUAL_ENV}/../scripts/ohos/ohos-env.sh" ]; then
  OHOS_ENV_SCRIPTS_DIR="$(CDPATH= cd -- "${VIRTUAL_ENV}/../scripts/ohos" && pwd)"
  # shellcheck disable=SC1091
  . "${OHOS_ENV_SCRIPTS_DIR}/ohos-env.sh"
fi
EOF
}
