#!/bin/sh
# 楦胯挋璁惧渚濊禆閫愬寘瀹夎 + import 楠岃瘉锛坖iuwenclaw AgentServer 鑷寘鍚級
#
# manifest 鐢?ohos-gen-manifest.sh 鎸?profile 鍔ㄦ€佺敓鎴愶紙鏃犻渶浠撳簱鍐?*.tsv锛?
#   agentserver-minimal 鈥?requirements-harmony.txt
#   agentcore-minimal   鈥?harmonyos/pyproject.toml 鈭?requirements-harmony
#
# 鎺ㄨ崘鍏ュ彛: sh scripts/install-ohos-agentserver.sh
#
# 鍗曠嫭璺戞煇闃舵:
#   USE_VENV=1 MANIFEST_PROFILE=agentserver-minimal sh scripts/ohos/install-ohos-all-deps.sh
#
# 杈撳嚭:
#   ~/ohos/deps-verify/install-report-YYYYMMDD-HHMMSS.log
#   ~/ohos/deps-verify/install-summary-YYYYMMDD-HHMMSS.tsv

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
export OHOS_ENV_SCRIPTS_DIR="$SCRIPT_DIR"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/ohos-env.sh"
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
export OHOS_REPO_ROOT="$REPO_ROOT"
VENV_DIR=${VENV_DIR:-$REPO_ROOT/.venv}
CREATE_VENV=${CREATE_VENV:-1}
RECREATE_VENV=${RECREATE_VENV:-0}
MANIFEST_PROFILE=${MANIFEST_PROFILE:-agentserver-minimal}
MANIFEST=${MANIFEST:-}
MANIFEST_GEN=${OHOS_MANIFEST_GEN:-$SCRIPT_DIR/ohos-gen-manifest.sh}
MANIFEST_CACHE=${OHOS_MANIFEST_CACHE:-$REPO_ROOT/.cache/ohos-manifests}
REPORT_DIR=${REPORT_DIR:-$HOME/ohos/deps-verify}
AUTO=${AUTO:-1}
CONTINUE_ON_FAIL=${CONTINUE_ON_FAIL:-1}
REUSE_INSTALLED=${REUSE_INSTALLED:-1}
WHEEL_DIR=${WHEEL_DIR:-}
PIP_NO_BUILD_ISOLATION=${PIP_NO_BUILD_ISOLATION:-1}
SKIP_OHOS_ENV=${SKIP_OHOS_ENV:-0}
SKIP_WHEEL_PRELOAD=${SKIP_WHEEL_PRELOAD:-0}
INSTALL_SCRIPT_ID=install-ohos-all-deps/2026-05-30-manifest-gen

resolve_manifest_file() {
  if [ -n "$MANIFEST" ] && [ -f "$MANIFEST" ]; then
    case $MANIFEST in
      /*) ;;
      *) MANIFEST=$SCRIPT_DIR/$MANIFEST ;;
    esac
    return 0
  fi
  [ -f "$MANIFEST_GEN" ] || {
    echo "ERROR: manifest generator not found: $MANIFEST_GEN" >&2
    exit 1
  }
  mkdir -p "$MANIFEST_CACHE"
  MANIFEST="$MANIFEST_CACHE/${MANIFEST_PROFILE}.tsv"
  OFFICE_CLAW="${OFFICE_CLAW:-}" \
  AGENT_CORE_PATH="${AGENT_CORE_PATH:-}" \
  OPENJIUWEN_SRC_DIR="${OPENJIUWEN_SRC_DIR:-}" \
  HARMONYOS_PYPROJECT="${HARMONYOS_PYPROJECT:-}" \
  REQUIREMENTS_HARMONY="${REQUIREMENTS_HARMONY:-$REPO_ROOT/requirements-harmony.txt}" \
  PYTHON="${PYTHON:-}" \
  OHOS_REAL_PYTHON="${OHOS_REAL_PYTHON:-}" \
  sh "$MANIFEST_GEN" "$MANIFEST_PROFILE" "$MANIFEST" || exit 1
}

python_under_venv() {
  case "$1" in
    "$VENV_DIR"/*|*/.venv/bin/python|*/.venv/bin/python3*) return 0 ;;
  esac
  return 1
}

# 鍕?readlink -f venv/bin/python锛歄hOS 涓婁細瑙ｆ瀽鍒?OHOS_REAL_PYTHON锛宲ip 瑁呭埌绯荤粺 site-packages
canonicalize_python() {
  _p=$1
  if python_under_venv "$_p"; then
    echo "$_p"
  else
    readlink -f "$_p" 2>/dev/null || echo "$_p"
  fi
}

if [ -n "${PYTHON:-}" ]; then
  :
elif command -v python3.12 >/dev/null 2>&1; then
  PYTHON=$(command -v python3.12)
elif command -v python3 >/dev/null 2>&1; then
  PYTHON=$(command -v python3)
else
  echo "ERROR: set PYTHON=/path/to/python3.12" >&2
  exit 1
fi

# 瀹氫綅 WHEEL_BUILD_ROOT锛堜紭鍏?$REPO_ROOT锛屽洖閫€ ohos-wheel-build锛?
if [ -z "${OFFICE_CLAW:-}" ]; then
  _oc=$(CDPATH= cd -- "$SCRIPT_DIR/../.." 2>/dev/null && pwd || true)
  if [ -n "$_oc" ]; then
    OFFICE_CLAW=$_oc
  fi
fi
if [ -z "${WHEEL_BUILD_ROOT:-}" ]; then
  if [ -d "$REPO_ROOT/wheels" ]; then
    WHEEL_BUILD_ROOT=$REPO_ROOT
  elif [ -n "${OFFICE_CLAW:-}" ] && [ -d "$OFFICE_CLAW/ohos-wheel-build/wheels" ]; then
    WHEEL_BUILD_ROOT=$OFFICE_CLAW/ohos-wheel-build
  elif [ -n "${WHEEL_DIR:-}" ] && [ -d "$WHEEL_DIR" ]; then
    _wb=$(CDPATH= cd -- "$WHEEL_DIR/.." && pwd)
    WHEEL_BUILD_ROOT=$_wb
  else
    WHEEL_BUILD_ROOT=$REPO_ROOT
  fi
fi
export WHEEL_BUILD_ROOT

mkdir -p "$REPORT_DIR"
TS=$(date +%Y%m%d-%H%M%S)
LOG=$REPORT_DIR/install-report-$TS.log
SUMMARY=$REPORT_DIR/install-summary-$TS.tsv

if [ ! -f "$MANIFEST" ]; then
  resolve_manifest_file
fi
if [ ! -f "$MANIFEST" ]; then
  echo "ERROR: manifest not found: $MANIFEST (profile=$MANIFEST_PROFILE)" >&2
  exit 1
fi

log() {
  printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$1" | tee -a "$LOG"
}

log_tool() {
  _cmd=$1
  if command -v "$_cmd" >/dev/null 2>&1; then
    _ver=$("$_cmd" --version 2>&1 || true)
    log "  $_cmd: $(command -v "$_cmd") ($_ver)"
  else
    log "  $_cmd: MISSING"
  fi
}

log_py_module() {
  _mod=$1
  _line=$("$PYTHON" -c "
import importlib
try:
    importlib.import_module('$_mod')
    print('OK')
except Exception as e:
    print(f'MISSING: {e}')
" 2>/dev/null || echo "MISSING: python error")
  log "py-$_mod: $_line"
}

verify_pep517_backends() {
  log "======== PEP517 backends (Python import check) ========"
  log_py_module maturin
  log_py_module mesonpy
  log_py_module setuptools_rust
  log "======================================================"
}

bootstrap_pep517() {
  export_pip_build_env

  log "bootstrap PEP517: pip setuptools wheel cffi ..."
  pip_install -U pip setuptools wheel cffi >>"$LOG" 2>&1 \
    || log "WARN: pip install base build deps failed (see log)"

  # setuptools-rust 蹇呴』鍏堜簬 maturin锛坢aturin sdist 鐨?setup 渚濊禆瀹冿級
  log "bootstrap PEP517: setuptools-rust meson-python ..."
  pip_install --no-build-isolation -U setuptools-rust meson-python >>"$LOG" 2>&1 \
    || log "WARN: pip install setuptools-rust meson-python failed (see log)"

  log "bootstrap PEP517: maturin (ohos 璺宠繃 pip 缂?sdist锛岀洿鎺?py shim + cargo CLI) ..."
  log "  RUSTC=${RUSTC:-unset} CARGO=${CARGO:-unset} MATURIN=${MATURIN:-unset} AR=${AR:-unset}"
  if ! install_maturin_py_shim >>"$LOG" 2>&1; then
    log "WARN: maturin py shim failed (see log)"
  fi

  ensure_build_tool_shims

  log "bootstrap: try numpy ohos wheel only (no sdist/meson) ..."
  export_pip_build_env
  _numpy_ok=
  for _numpy_spec in 'numpy==2.4.5' 'numpy>=2.4.5,<2.5' 'numpy>=1.26,!=2.4.0'; do
    if pip_install --only-binary=:all: "$_numpy_spec" >>"$LOG" 2>&1; then
      log "bootstrap numpy OK: $_numpy_spec"
      _numpy_ok=1
      break
    fi
  done
  if [ -z "$_numpy_ok" ]; then
    log "WARN: no numpy ohos wheel on index 鈥?put numpy-*-ohos_aarch64.whl in WHEEL_DIR or build offline"
  fi

  verify_pep517_backends
  patch_pep517_site_packages
  verify_pep517_subprocess
}

install_maturin_py_shim() {
  # pip 缂?maturin 鎵╁睍鍦?HiShell 瀛愯繘绋嬮噷甯告壘涓嶅埌 rustc锛沜argo install 鍙湁 CLI銆?
  # 浠?sdist 鍙В鍘?maturin/*.py锛宨mport maturin 浼?delegate 鍒?PATH 涓婄殑 maturin 浜岃繘鍒躲€?
  if ! command -v maturin >/dev/null 2>&1; then
    log "maturin py shim: maturin CLI not in PATH"
    return 1
  fi
  log "maturin py shim: extract pure-Python package (CLI=$(command -v maturin))"
  "$PYTHON" >>"$LOG" 2>&1 <<'PY'
import glob, importlib.util, os, shutil, site, subprocess, sys, tarfile, tempfile

if importlib.util.find_spec("maturin"):
    print("maturin py shim: already importable")
    sys.exit(0)

cli = shutil.which("maturin")
if not cli:
    print("maturin py shim: no maturin CLI")
    sys.exit(1)

tmpdir = tempfile.mkdtemp(prefix="maturin-shim-")
subprocess.check_call(
    [sys.executable, "-m", "pip", "download", "maturin==1.13.3", "--no-binary", ":all:", "-d", tmpdir]
)
tars = glob.glob(os.path.join(tmpdir, "maturin-*.tar.gz"))
if not tars:
    print("maturin py shim: sdist not downloaded")
    sys.exit(1)

dest_root = site.getsitepackages()[0]
pkg_dir = os.path.join(dest_root, "maturin")
os.makedirs(pkg_dir, exist_ok=True)

with tarfile.open(tars[0], "r:gz") as tf:
    prefix = tf.getmembers()[0].name.split("/")[0]
    for m in tf.getmembers():
        if not m.isfile():
            continue
        rel = m.name[len(prefix) + 1 :]
        if rel.startswith("maturin/") and rel.endswith(".py"):
            out = os.path.join(dest_root, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with tf.extractfile(m) as src, open(out, "wb") as dst:
                dst.write(src.read())

importlib.import_module("maturin")
print(f"maturin py shim: OK -> {pkg_dir} (CLI={cli})")
PY
}

_realpath() {
  readlink -f "$1" 2>/dev/null || echo "$1"
}

resolve_real_tool() {
  _name=$1
  _found=
  case "$_name" in
    rustc)
      for _c in \
        "${RUSTC:-}" \
        "${OHOS_RUST_BIN:-}/rustc" \
        "$HOME/usr/rust-1.95.0-aarch64-unknown-linux-ohos/bin/rustc"; do
        if [ -n "$_c" ] && [ -x "$_c" ]; then
          _found=$(_realpath "$_c")
          break
        fi
      done
      ;;
    cargo)
      for _c in \
        "${CARGO:-}" \
        "${OHOS_RUST_BIN:-}/cargo" \
        "$HOME/usr/rust-1.95.0-aarch64-unknown-linux-ohos/bin/cargo"; do
        if [ -n "$_c" ] && [ -x "$_c" ]; then
          _found=$(_realpath "$_c")
          break
        fi
      done
      ;;
    maturin)
      for _c in \
        "${MATURIN:-}" \
        "${WHEEL_BUILD_ROOT:-}/cargo/bin/maturin" \
        "$HOME/.cargo/bin/maturin"; do
        if [ -n "$_c" ] && [ -x "$_c" ]; then
          _found=$(_realpath "$_c")
          break
        fi
      done
      ;;
  esac
  if [ -n "$_found" ]; then
    echo "$_found"
    return 0
  fi
  return 1
}

ensure_build_tool_shims() {
  # 鍙啓鐩綍鏀惧伐鍏?symlink锛堥』鎸囧悜鐪熷疄浜岃繘鍒讹紝閬垮厤鑷紩鐢ㄥ潖閾撅級
  _shimdir="${WHEEL_BUILD_ROOT:-}/bin"
  if [ -n "$_shimdir" ]; then
    mkdir -p "$_shimdir" 2>/dev/null || true
    case ":${PATH}:" in
      *":$_shimdir:"*) ;;
      *) PATH="$_shimdir:$PATH"; export PATH ;;
    esac
  fi

  _pybin=$(dirname "$PYTHON")
  case ":${PATH}:" in
    *":$_pybin:"*) ;;
    *) PATH="$_pybin:$PATH"; export PATH ;;
  esac

  for _tool in rustc cargo maturin; do
    _src=$(resolve_real_tool "$_tool") || continue
    for _dst in "$_shimdir/$_tool" "$_pybin/$_tool"; do
      [ -n "$_dst" ] || continue
      _dst_dir=$(dirname "$_dst")
      mkdir -p "$_dst_dir" 2>/dev/null || continue
      _cur=$(_realpath "$_dst" 2>/dev/null || true)
      if [ -n "$_cur" ] && [ "$_cur" = "$_src" ]; then
        continue
      fi
      rm -f "$_dst" 2>/dev/null || true
      if ln -sf "$_src" "$_dst" 2>/dev/null; then
        log "tool shim: $_dst -> $_src"
      elif cp -f "$_src" "$_dst" 2>/dev/null && chmod +x "$_dst" 2>/dev/null; then
        log "tool copy: $_dst <- $_src"
      else
        log "WARN: cannot shim $_tool to $_dst"
      fi
    done
  done

  _rustc=$(resolve_real_tool rustc) || _rustc=
  _cargo=$(resolve_real_tool cargo) || _cargo=
  _maturin=$(resolve_real_tool maturin) || _maturin=
  [ -n "$_rustc" ] && export RUSTC="$_rustc"
  [ -n "$_cargo" ] && export CARGO="$_cargo"
  [ -n "$_maturin" ] && export MATURIN="$_maturin"
}

write_ohos_toolchain_env() {
  _root="${WHEEL_BUILD_ROOT:-}"
  [ -n "$_root" ] || return 0
  _envfile="$_root/sitepatch/toolchain.env"
  mkdir -p "$(dirname "$_envfile")" 2>/dev/null || return 0
  {
    echo "# generated by install-ohos-all-deps.sh $(date '+%Y-%m-%dT%H:%M:%S')"
    echo "WHEEL_BUILD_ROOT=$_root"
    echo "RUSTC=${RUSTC:-}"
    echo "CARGO=${CARGO:-}"
    echo "MATURIN=${MATURIN:-}"
    echo "CC=${CC:-}"
    echo "CXX=${CXX:-}"
    echo "AR=${AR:-}"
    echo "RANLIB=${RANLIB:-}"
    echo "OPENSSL_DIR=${OPENSSL_DIR:-}"
    echo "PKG_CONFIG_PATH=${PKG_CONFIG_PATH:-}"
    echo "LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}"
    echo "CFLAGS=${CFLAGS:-}"
    echo "CPPFLAGS=${CPPFLAGS:-}"
    echo "LDFLAGS=${LDFLAGS:-}"
    echo "PATH=${PATH:-}"
    echo "OHOS_HNP_ROOT=${OHOS_HNP_ROOT:-}"
    echo "OHOS_HNP_BIN=${OHOS_HNP_BIN:-}"
    echo "OHOS_LLVM_BIN=${OHOS_LLVM_BIN:-}"
    echo "OHOS_RUST_BIN=${OHOS_RUST_BIN:-}"
    echo "OHOS_USR_LOCAL=${OHOS_USR_LOCAL:-}"
    echo "OHOS_STORAGE_ROOT=${OHOS_STORAGE_ROOT:-}"
  } >"$_envfile"
  export OHOS_TOOLCHAIN_ENV="$_envfile"
}

install_ohos_python_site_hook() {
  _root="${WHEEL_BUILD_ROOT:-}"
  _src="$_root/sitepatch/ohos_build_env.py"
  _sc="$_root/sitepatch/sitecustomize.py"
  [ -f "$_src" ] || {
    log "WARN: missing $_src 鈥?skip python site hook"
    return 0
  }
  write_ohos_toolchain_env
  log "install python site hook (sitecustomize + ohos_build_env -> site-packages)"
  WHEEL_BUILD_ROOT="${WHEEL_BUILD_ROOT:-}" "$PYTHON" >>"$LOG" 2>&1 <<'PY'
import os, shutil, site

root = os.environ.get("WHEEL_BUILD_ROOT", "")
sp = site.getsitepackages()[0]

for name in ("ohos_build_env.py", "sitecustomize.py"):
    src = os.path.join(root, "sitepatch", name)
    if not os.path.isfile(src):
        print(f"site hook: missing {src}")
        raise SystemExit(1)
    dst = os.path.join(sp, name)
    shutil.copy2(src, dst)
    print(f"site hook copied: {dst}")

pth = os.path.join(sp, "ohos-build-env.pth")
with open(pth, "w", encoding="utf-8") as f:
    f.write("import ohos_build_env\n")
print(f"site hook pth: {pth}")

import ohos_build_env  # noqa: F401
print(f"python RUSTC={os.environ.get('RUSTC', 'unset')}")
print(f"python MATURIN={os.environ.get('MATURIN', 'unset')}")
PY
}

patch_pep517_site_packages() {
  export_pip_build_env
  log "patch site-packages PEP517 hooks (maturin __init__ + setuptools_rust rustc_info)"
  WHEEL_BUILD_ROOT="${WHEEL_BUILD_ROOT:-}" \
  OHOS_MATURIN_BIN="${MATURIN:-}" \
  OHOS_RUSTC_BIN="${RUSTC:-}" \
  "$PYTHON" >>"$LOG" 2>&1 <<'PY'
import os, re, site

maturin_bin = os.environ.get("OHOS_MATURIN_BIN", "") or os.environ.get("MATURIN", "")
rustc_bin = os.environ.get("OHOS_RUSTC_BIN", "") or os.environ.get("RUSTC", "")
sp = site.getsitepackages()[0]

if not rustc_bin or not os.path.isfile(rustc_bin):
    print(f"patch: rustc binary missing: {rustc_bin!r}")
    raise SystemExit(1)

def patch_maturin_init(path, maturin_bin):
    text = open(path, encoding="utf-8").read()
    if "OHOS_MATURIN_BIN_v2" in text:
        return False
    while text.startswith("# OHOS_MATURIN_BIN"):
        text = text.split("\n", 1)[1]
    text = re.sub(
        r'(\b(?:base_command|command)\s*=\s*\[\s*\n?\s*)"maturin"',
        rf'\1"{maturin_bin}"',
        text,
    )
    text = text.replace('["maturin",', f'["{maturin_bin}",')
    text = text.replace("['maturin',", f"['{maturin_bin}',")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# OHOS_MATURIN_BIN_v2={maturin_bin}\n" + text)
    return True

maturin_init = os.path.join(sp, "maturin", "__init__.py")
if maturin_bin and os.path.isfile(maturin_bin) and os.path.isfile(maturin_init):
    if patch_maturin_init(maturin_init, maturin_bin):
        print(f"patched maturin/__init__.py -> {maturin_bin}")
    else:
        print("maturin/__init__.py already patched (v2)")
    body = open(maturin_init, encoding="utf-8").read()
    leftover = len(re.findall(r'=\s*\[\s*(?:\n\s*)?"maturin"', body))
    print(f"maturin bare CLI refs left in command lists: {leftover}")
elif not os.path.isfile(maturin_init):
    print(f"WARN: {maturin_init} not found")
else:
    print(f"WARN: maturin binary missing: {maturin_bin!r}")

rustc_info = os.path.join(sp, "setuptools_rust", "rustc_info.py")
if os.path.isfile(rustc_info):
    text = open(rustc_info, encoding="utf-8").read()
    if "OHOS_RUSTC_BIN" not in text:
        block = f'''

# OHOS_RUSTC_BIN={rustc_bin}
import os as _ohos_os
import subprocess as _ohos_sp

def _ohos_rustc_path() -> str:
    p = _ohos_os.environ.get("RUSTC")
    if p and _ohos_os.path.isfile(p):
        return p
    return {rustc_bin!r}

def _ohos_merge_env(env):
    if isinstance(env, Env):
        env = env.env
    base = _ohos_os.environ.copy()
    if env is None:
        return base
    if isinstance(env, dict):
        base.update(env)
    base["RUSTC"] = _ohos_rustc_path()
    rust_dir = _ohos_os.path.dirname(_ohos_rustc_path())
    path = base.get("PATH", "")
    if rust_dir and rust_dir not in path.split(_ohos_os.pathsep):
        base["PATH"] = rust_dir + _ohos_os.pathsep + path
    return base

@lru_cache()
def _rust_version(env: Env) -> str:
    return _ohos_sp.check_output(
        [_ohos_rustc_path(), "-V"], env=_ohos_merge_env(env), text=True
    )

@lru_cache()
def _rust_version_verbose(env: Env) -> str:
    return _ohos_sp.check_output(
        [_ohos_rustc_path(), "-Vv"], env=_ohos_merge_env(env), text=True
    )

@lru_cache()
def get_rust_target_info(target_triple: Optional[str], env: Env) -> List[str]:
    cmd = [_ohos_rustc_path(), "--print", "cfg"]
    if target_triple:
        cmd.extend(["--target", target_triple.split(".")[0]])
    output = _ohos_sp.check_output(cmd, env=_ohos_merge_env(env), text=True)
    return output.splitlines()

@lru_cache()
def get_rust_target_list(env: Env) -> List[str]:
    output = _ohos_sp.check_output(
        [_ohos_rustc_path(), "--print", "target-list"],
        env=_ohos_merge_env(env),
        text=True,
    )
    return output.splitlines()
'''
        with open(rustc_info, "a", encoding="utf-8") as f:
            f.write(block)
        print(f"patched setuptools_rust/rustc_info.py -> {rustc_bin}")
    else:
        print("setuptools_rust/rustc_info.py already patched")
else:
    print(f"WARN: {rustc_info} not found")
PY
}

verify_pep517_subprocess() {
  log "PEP517 _in_process simulation (inherit OhOS runtime, hide MATURIN/RUSTC/CARGO):"
  WHEEL_BUILD_ROOT="${WHEEL_BUILD_ROOT:-}" "$PYTHON" >>"$LOG" 2>&1 <<'PY'
import os, subprocess, sys

# OhOS/HNP Python 渚濊禆鐖惰繘绋嬩腑鐨勮繍琛屾椂鐜銆?
# 涓嶈兘鏋勯€犱竴涓繎涔庣┖鐧界殑 env锛屽惁鍒欏姩鎬佸姞杞藉櫒鍙兘鎵句笉鍒?
# libpython3.12.so.1.0 / libintl.so.8銆?
# 杩欓噷鍙Щ闄ゆ湰娴嬭瘯鍒绘剰瑕侀殣钘忕殑 Rust/Maturin 鍙橀噺锛屽叾浣欑幆澧冨畬鏁寸户鎵裤€?
env = os.environ.copy()
for key in ("RUSTC", "CARGO", "MATURIN"):
    env.pop(key, None)
code = r"""
import os, importlib
print("  in_process RUSTC=%s" % os.environ.get("RUSTC", "unset"))
print("  in_process MATURIN=%s" % os.environ.get("MATURIN", "unset"))
import maturin
src = open(maturin.__file__, encoding="utf-8").read()
print("  maturin patched=%s" % ("OHOS_MATURIN_BIN_v2" in src))
import setuptools_rust.rustc_info as ri
print("  rustc_info patched=%s" % ("OHOS_RUSTC_BIN" in open(ri.__file__, encoding="utf-8").read()))
from semantic_version import Version
v = ri.get_rust_version(None)
print("  get_rust_version=%s" % v)
"""
r = subprocess.run([sys.executable, "-c", code], env=env, text=True, capture_output=True)
print(r.stdout, end="")
if r.stderr:
    print(r.stderr, end="")
if r.returncode != 0:
    raise SystemExit(r.returncode)
PY
}

verify_python_toolchain() {
  log "python subprocess toolchain:"
  "$PYTHON" >>"$LOG" 2>&1 <<'PY'
import os, shutil, subprocess, sys

print(f"  RUSTC={os.environ.get('RUSTC', 'unset')}")
print(f"  CARGO={os.environ.get('CARGO', 'unset')}")
print(f"  MATURIN={os.environ.get('MATURIN', 'unset')}")
rustc = os.environ.get("RUSTC") or shutil.which("rustc")
if rustc:
    try:
        out = subprocess.check_output([rustc, "--version"], text=True, stderr=subprocess.STDOUT).strip()
        print(f"  rustc --version: {out}")
    except Exception as e:
        print(f"  rustc --version: FAIL {e}")
else:
    print("  rustc: NOT FOUND in python subprocess")
PY
}

_prepend_path_var() {
  _var=$1
  _dir=$2
  [ -n "$_dir" ] && [ -d "$_dir" ] || return 0
  case "$_var" in
    LD_LIBRARY_PATH)
      case ":${LD_LIBRARY_PATH:-}:" in
        *":$_dir:"*) ;;
        *)
          LD_LIBRARY_PATH="${_dir}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
          export LD_LIBRARY_PATH
          ;;
      esac
      ;;
    PKG_CONFIG_PATH)
      case ":${PKG_CONFIG_PATH:-}:" in
        *":$_dir:"*) ;;
        *)
          PKG_CONFIG_PATH="${_dir}${PKG_CONFIG_PATH:+:${PKG_CONFIG_PATH}}"
          export PKG_CONFIG_PATH
          ;;
      esac
      ;;
    PATH)
      case ":${PATH}:" in
        *":$_dir:"*) ;;
        *)
          PATH="${_dir}:${PATH}"
          export PATH
          ;;
      esac
      ;;
  esac
}

_force_prepend_ld() {
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

_dedupe_path_var() {
  _var=$1
  _cur=
  case "$_var" in
    LD_LIBRARY_PATH) _cur=${LD_LIBRARY_PATH:-} ;;
    PKG_CONFIG_PATH) _cur=${PKG_CONFIG_PATH:-} ;;
    PATH) _cur=${PATH:-} ;;
    *) return 0 ;;
  esac
  [ -n "$_cur" ] || return 0
  _out=
  _old_ifs=$IFS
  IFS=:
  # shellcheck disable=SC2086
  set -- $_cur
  IFS=$_old_ifs
  for _p in "$@"; do
    [ -n "$_p" ] || continue
    case ":${_out}:" in
      *":$_p:"*) ;;
      *) _out="${_out:+${_out}:}${_p}" ;;
    esac
  done
  case "$_var" in
    LD_LIBRARY_PATH) LD_LIBRARY_PATH=$_out; export LD_LIBRARY_PATH ;;
    PKG_CONFIG_PATH) PKG_CONFIG_PATH=$_out; export PKG_CONFIG_PATH ;;
    PATH) PATH=$_out; export PATH ;;
  esac
}

detect_openssl_prefix() {
  if [ -n "${OPENSSL_DIR:-}" ] && [ -f "${OPENSSL_DIR}/lib/pkgconfig/openssl.pc" ]; then
    _realpath "${OPENSSL_DIR}" 2>/dev/null || echo "${OPENSSL_DIR}"
    return 0
  fi
  for _prefix in \
    "$OHOS_USR_LOCAL" \
    "$HOME/usr/local" \
    "$HOME/usr/openssl" \
    "$HOME/.cmd-pkgs/openssl"; do
    if [ -f "$_prefix/lib/pkgconfig/openssl.pc" ]; then
      _realpath "$_prefix" 2>/dev/null || echo "$_prefix"
      return 0
    fi
  done
  return 1
}

_discover_pkgconfig_dir() {
  _pc=$1
  shift
  for _prefix in "$@"; do
    [ -n "$_prefix" ] || continue
    if [ -f "$_prefix/lib/pkgconfig/$_pc" ]; then
      _realpath "$_prefix/lib/pkgconfig" 2>/dev/null || echo "$_prefix/lib/pkgconfig"
      return 0
    fi
  done
  _found=$(find "$OHOS_USR_LOCAL" "$HOME/usr/local" "$HOME/usr" "$HOME/.cmd-pkgs" \
    $(ohos_hnp_pkg_glob_roots 2>/dev/null | tr '\n' ' ') \
    -name "$_pc" 2>/dev/null | head -1)
  if [ -n "$_found" ]; then
    dirname "$_found"
    return 0
  fi
  return 1
}

ensure_native_lib_env() {
  # Python libdir锛坧ydantic_core/tiktoken 绛夛級+ cmd-pkgs OpenSSL/libffi锛坈ryptography/cffi锛?
  _py_for_libdir=${OHOS_REAL_PYTHON:-$PYTHON}
  _pylibdir=$(CDPATH= cd -- "$(dirname "$_py_for_libdir")/../lib" 2>/dev/null && pwd || true)
  if [ -z "$_pylibdir" ] || [ ! -d "$_pylibdir" ]; then
    _pylibdir=$("$_py_for_libdir" -c 'import sysconfig; print(sysconfig.get_config_var("LIBDIR") or "")' 2>/dev/null || true)
  fi
  _prepend_path_var LD_LIBRARY_PATH "$_pylibdir"

  # The path may already contain the HNP Python libdir behind HarmonyBrew.
  # Move it to the front so Python loads its matching libintl ABI.
  _force_prepend_ld "$_pylibdir"

  _openssl_prefix=$(detect_openssl_prefix) || _openssl_prefix=
  if [ -n "$_openssl_prefix" ]; then
    export OPENSSL_DIR="$_openssl_prefix"
    _prepend_path_var PKG_CONFIG_PATH "$_openssl_prefix/lib/pkgconfig"
    _prepend_path_var LD_LIBRARY_PATH "$_openssl_prefix/lib"
  fi

  for _libdir in \
    "${OPENSSL_DIR:-}/lib" \
    "$HOME/usr/local/lib" \
    "$HOME/usr/lib"; do
    if [ -f "$_libdir/libffi.so" ] || [ -f "$_libdir/libffi.so.8" ]; then
      _prepend_path_var LD_LIBRARY_PATH "$_libdir"
      # cffi 缂栬瘧闇€瑕?ffi.h 澶存枃浠?
      _ffi_parent=$(dirname "$_libdir")/include
      _ffi_incs=""
      [ -f "$_ffi_parent/ffi.h" ] && _ffi_incs="-I$_ffi_parent"
      [ -f "$_ffi_parent/ffi/ffi.h" ] && _ffi_incs="$_ffi_incs -I$_ffi_parent/ffi"
      if [ -n "$_ffi_incs" ]; then
        case " ${CFLAGS:-} " in
          *"ffi"*) ;;
          *) export CFLAGS="${CFLAGS:+$CFLAGS }$_ffi_incs"
             export CPPFLAGS="${CPPFLAGS:+$CPPFLAGS }$_ffi_incs" ;;
        esac
        case " ${LDFLAGS:-} " in
          *"-L$_libdir"*) ;;
          *) export LDFLAGS="${LDFLAGS:+$LDFLAGS }-L$_libdir" ;;
        esac
      fi
      break
    fi
  done

  # libxml2/libxslt锛歝md-pkgs usr/local 鎴?HNP cmd-pkgs 鍖呯洰褰?
  for _root in \
    $(ohos_hnp_pkg_glob_roots 2>/dev/null) \
    "$OHOS_USR_LOCAL" \
    "$HOME/usr/local" \
    "$HOME/usr/libxml2" \
    "$HOME/usr/libxslt" \
    "$HOME/.harmonybrew/opt/libxml2" \
    "$HOME/.harmonybrew/opt/libxslt" \
    "${OFFICE_CLAW:-}/ohos-wheel-build/deps/prefix"; do
    [ -d "$_root" ] || continue
    [ -d "$_root/lib/pkgconfig" ] && _prepend_path_var PKG_CONFIG_PATH "$_root/lib/pkgconfig"
    [ -d "$_root/lib" ] && _prepend_path_var LD_LIBRARY_PATH "$_root/lib"
    [ -d "$_root/bin" ] && _prepend_path_var PATH "$_root/bin"
  done
  for _pc in libxml-2.0.pc libxslt.pc; do
    _pcdir=$(_discover_pkgconfig_dir "$_pc" \
      "$HOME/usr/local" \
      "$HOME/usr/libxml2" \
      "$HOME/usr/libxslt" \
      "$HOME/.harmonybrew/opt/libxml2" \
      "$HOME/.harmonybrew/opt/libxslt" \
      "${OFFICE_CLAW:-}/ohos-wheel-build/deps/prefix") || _pcdir=
    if [ -n "$_pcdir" ]; then
      _prepend_path_var PKG_CONFIG_PATH "$_pcdir"
      _libdir=$(dirname "$_pcdir")
      _libdir=$(dirname "$_libdir")
      _prepend_path_var LD_LIBRARY_PATH "$_libdir/lib"
    fi
  done

  _dedupe_path_var LD_LIBRARY_PATH
  _dedupe_path_var PKG_CONFIG_PATH

  # cryptography 杩愯鏃堕』鍔犺浇 cmd-pkgs OpenSSL锛?=3.2锛夛紝涓嶈兘琚?HNP 鏃?libssl 鎶㈠厛
  if [ -n "${OPENSSL_DIR:-}" ] && [ -d "${OPENSSL_DIR}/lib" ]; then
    _prepend_path_var LD_LIBRARY_PATH "${OPENSSL_DIR}/lib"
    _dedupe_path_var LD_LIBRARY_PATH
  fi

  # All native discovery above prepends its own paths. Restore HNP Python's
  # runtime directory to position zero before any venv Python/pip invocation.
  _force_prepend_ld "$_pylibdir"
}

verify_native_libs() {
  log "native libs (cmd-pkgs usr/local auto-detect):"
  if [ -n "${OPENSSL_DIR:-}" ]; then
    _ossl_ver=
    if command -v pkg-config >/dev/null 2>&1; then
      _ossl_ver=$(PKG_CONFIG_PATH="${PKG_CONFIG_PATH:-}" pkg-config --modversion openssl 2>/dev/null || true)
    fi
    log "  OPENSSL_DIR=$OPENSSL_DIR (openssl=${_ossl_ver:-unknown})"
    case "${_ossl_ver:-}" in
      3.0.*|3.1.*|2.*|1.*)
        log "  WARN: openssl ${_ossl_ver} < 3.2 鈥?cryptography 48 闇€瑕?OSSL_get_max_threads锛涜 cmd-pkgs 瀹夎 openssl 3.5.6"
        ;;
    esac
  else
    log "  OPENSSL_DIR: MISSING - install: curl ... | sh -s -- openssl 3.5.6"
  fi
  _ffi=
  for _libdir in "${OPENSSL_DIR:-}/lib" "$HOME/usr/local/lib" "$HOME/usr/lib"; do
    if [ -f "$_libdir/libffi.so" ] || [ -f "$_libdir/libffi.so.8" ]; then
      _ffi="$_libdir/libffi.so*"
      break
    fi
  done
  if [ -n "$_ffi" ]; then
    log "  libffi: OK ($_ffi)"
  else
    log "  libffi: MISSING - install: curl ... | sh -s -- libffi 3.4.6"
  fi
  log "  PKG_CONFIG_PATH=${PKG_CONFIG_PATH:-unset}"
  log "  LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-unset}"
  # OhOS/HNP 涓嬩笉瑕佺敤澶栭儴 env 鍛戒护鍖呰９ Python锛岀洿鎺ョ户鎵垮綋鍓?shell 杩愯鏃剁幆澧冦€?
  _cffi=$("$PYTHON" -c "import _cffi_backend; print('OK')" 2>/dev/null || echo "FAIL")
  log "  cffi _cffi_backend import: $_cffi"
}

verify_system_deps() {
  log "system deps (optional cmd-pkgs; missing = some packages will fail):"
  for _tool in pg_config pkg-config; do
    if command -v "$_tool" >/dev/null 2>&1; then
      log "  $_tool: $(command -v "$_tool")"
    else
      log "  $_tool: MISSING"
    fi
  done
  for _pc in libxml-2.0 libxslt openssl; do
    if PKG_CONFIG_PATH="${PKG_CONFIG_PATH:-}" pkg-config --exists "$_pc" 2>/dev/null; then
      log "  pkg-config $_pc: $(PKG_CONFIG_PATH="${PKG_CONFIG_PATH:-}" pkg-config --modversion "$_pc" 2>/dev/null)"
    else
      _hint=
      case "$_pc" in
        libxml-2.0) _pcfile=libxml-2.0.pc ;;
        libxslt) _pcfile=libxslt.pc ;;
        *) _pcfile= ;;
      esac
      if [ -n "$_pcfile" ]; then
        _found=$(find "$OHOS_USR_LOCAL" "$HOME/usr/local" "$HOME/usr" "$HOME/.cmd-pkgs" \
          $(ohos_hnp_pkg_glob_roots 2>/dev/null | tr '\n' ' ') \
          -name "$_pcfile" 2>/dev/null | head -1)
        [ -n "$_found" ] && _hint=" (found $_found; PKG_CONFIG_PATH=$(dirname "$_found"))"
      fi
      case "$_pc" in
        libxml-2.0) log "  pkg-config libxml-2.0: MISSING - HNP ${OHOS_HNP_ROOT}/libxml2.org/ or cmd-pkgs libxml2${_hint}" ;;
        libxslt) log "  pkg-config libxslt: MISSING - HNP ${OHOS_HNP_ROOT}/libxslt.org/ or cmd-pkgs libxslt${_hint}" ;;
        openssl) log "  pkg-config openssl: MISSING" ;;
      esac
    fi
  done
  if [ -f "$HOME/usr/local/lib/libpdfium.so" ] || pkg-config --exists pdfium 2>/dev/null; then
    log "  pdfium: OK"
  else
    log "  pdfium: MISSING - pdfplumber/pypdfium2 need cmd-pkgs pdfium or prebuilt wheel"
  fi
}

bootstrap_native_wheels() {
  # 閾?libpython 鐨勯缂?wheel锛沺hase 0 preload 鎴?WHEEL_DIR 鏈?wheel 鏃惰烦杩?pip 缂栬瘧
  export_pip_build_env
  _find_wheel="${OHOS_FIND_WHEEL_SCRIPT:-$SCRIPT_DIR/find-ohos-wheel.sh}"
  if [ -n "${WHEEL_DIR:-}" ] && [ -d "$WHEEL_DIR" ] && [ -f "$_find_wheel" ]; then
    if sh "$_find_wheel" cryptography >>"$LOG" 2>&1; then
      log "bootstrap: skip pip cryptography (ohos wheel in WHEEL_DIR)"
      return 0
    fi
  fi
  if [ -n "${OPENSSL_DIR:-}" ]; then
    log "bootstrap: cryptography (OpenSSL detected, pip compile fallback) ..."
    if pip_install "cryptography>=48.0.0,<49" >>"$LOG" 2>&1; then
      log "bootstrap cryptography OK"
    else
      log "WARN: bootstrap cryptography failed (see log)"
    fi
  else
    log "bootstrap skip cryptography (OPENSSL_DIR unset)"
  fi
}

export_pip_build_env() {
  # pip/meson/rust 瀛愯繘绋嬪父鎷夸笉鍒?HiShell PATH锛屾樉寮忚ˉ鍏?
  for _bindir in \
    "$OHOS_HNP_BIN" \
    "${OHOS_LLVM_BIN:-}" \
    "${WHEEL_BUILD_ROOT:+$WHEEL_BUILD_ROOT/cargo/bin}" \
    "${OHOS_RUST_BIN:-}" \
    "$HOME/.cargo/bin"; do
    if [ -d "$_bindir" ]; then
      case ":${PATH}:" in
        *":$_bindir:"*) ;;
        *) PATH="$_bindir:$PATH" ;;
      esac
    fi
  done
  export PATH

  if command -v clang >/dev/null 2>&1; then
    export CC=$(command -v clang)
    export CXX=$(command -v clang++ 2>/dev/null || command -v clang)
    _llvm_bin=$(dirname "$CC")
    for _ar in "$_llvm_bin/llvm-ar" "$_llvm_bin/llvm-ar-15" "$_llvm_bin/ar"; do
      if [ -x "$_ar" ]; then
        export AR="$_ar"
        break
      fi
    done
    for _ranlib in "$_llvm_bin/llvm-ranlib" "$_llvm_bin/llvm-ranlib-15"; do
      if [ -x "$_ranlib" ]; then
        export RANLIB="$_ranlib"
        break
      fi
    done
  fi
  if [ -n "${OHOS_LLVM_BIN:-}" ] && [ -x "${OHOS_LLVM_BIN}/clang" ]; then
    export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_OHOS_LINKER="${OHOS_LLVM_BIN}/clang"
  fi
  if command -v rustc >/dev/null 2>&1; then
    export RUSTC=$(_realpath "$(command -v rustc)" 2>/dev/null || command -v rustc)
    export CARGO=$(_realpath "$(command -v cargo)" 2>/dev/null || command -v cargo)
    _rust_bin=$(dirname "$RUSTC")
    case ":${PATH}:" in
      *":$_rust_bin:"*) ;;
      *) PATH="$_rust_bin:$PATH" ;;
    esac
    export PATH
  fi
  ensure_build_tool_shims
  ensure_native_lib_env
}

detect_wheel_platform_tag() {
  # 鏈満 pip 鎺ュ彈鐨?platform 鍚庣紑锛屽 ohos_aarch64锛堥潪 harmonyos_aarch64锛?
  "$PYTHON" -c "
import re, subprocess, sys
text = subprocess.check_output(
    [sys.executable, '-m', 'pip', 'debug', '--verbose'],
    stderr=subprocess.STDOUT, text=True, errors='replace',
)
tags = re.findall(r'cp\d+-cp\d+-(\S+)', text)
for prefer in ('ohos_aarch64', 'harmonyos_aarch64'):
    if prefer in tags:
        print(prefer)
        break
else:
    for t in tags:
        if 'ohos' in t or 'harmony' in t:
            print(t)
            break
    else:
        print(tags[0] if tags else 'ohos_aarch64')
" 2>/dev/null || echo "ohos_aarch64"
}

resolve_ohos_base_python() {
  if [ -n "${OHOS_REAL_PYTHON:-}" ]; then
    readlink -f "$OHOS_REAL_PYTHON" 2>/dev/null || echo "$OHOS_REAL_PYTHON"
    return 0
  fi
  echo "$PYTHON"
}

ensure_install_venv() {
  [ "${USE_VENV:-0}" = "1" ] || return 0

  if python_under_venv "$PYTHON"; then
    log "USE_VENV: install target already in venv: $PYTHON"
    export OHOS_REAL_PYTHON="${OHOS_REAL_PYTHON:-$(resolve_ohos_base_python)}"
    export INSTALL_VENV_PYTHON="$PYTHON"
    patch_venv_activate_ohos "$VENV_DIR"
    return 0
  fi

  _base=$(resolve_ohos_base_python)
  _base=$(readlink -f "$_base" 2>/dev/null || echo "$_base")
  export OHOS_REAL_PYTHON="$_base"

  if [ "$CREATE_VENV" != "1" ]; then
    log "ERROR: USE_VENV=1 but PYTHON is not under $VENV_DIR; set PYTHON=$VENV_DIR/bin/python or CREATE_VENV=1"
    exit 1
  fi

  if [ "$RECREATE_VENV" = "1" ] && [ -d "$VENV_DIR" ]; then
    log "RECREATE_VENV=1: remove $VENV_DIR"
    rm -rf "$VENV_DIR"
  fi

  if [ ! -x "$VENV_DIR/bin/python" ]; then
    log "create venv: $VENV_DIR (base $OHOS_REAL_PYTHON)"
    "$OHOS_REAL_PYTHON" -m venv "$VENV_DIR" || exit 1
  else
    log "reuse venv: $VENV_DIR"
  fi

  export PYTHON="$VENV_DIR/bin/python"
  export INSTALL_VENV_PYTHON="$PYTHON"
  patch_venv_activate_ohos "$VENV_DIR"
  log "USE_VENV=1 install target PYTHON=$PYTHON (libpython base OHOS_REAL_PYTHON=$OHOS_REAL_PYTHON)"
}

setup_build_env() {
  export OHOS_REAL_PYTHON="${OHOS_REAL_PYTHON:-$(resolve_ohos_base_python)}"
  _saved_python=
  if [ "${USE_VENV:-0}" = "1" ]; then
    _saved_python="${INSTALL_VENV_PYTHON:-$VENV_DIR/bin/python}"
  fi
  if [ "$SKIP_OHOS_ENV" != "1" ] && [ -n "${WHEEL_BUILD_ROOT:-}" ] && [ -f "$WHEEL_BUILD_ROOT/env.sh" ]; then
    log "source $WHEEL_BUILD_ROOT/env.sh"
    # shellcheck disable=SC1091
    . "$WHEEL_BUILD_ROOT/env.sh"
  else
    log "env.sh not loaded (WHEEL_BUILD_ROOT=${WHEEL_BUILD_ROOT:-unset})"
  fi
  # env.sh 浼?activate ohos-wheel-build/.venv锛屽嬁璁╁畠瑕嗙洊 jiuwenswarm/.venv 鐨?PYTHON
  if [ -n "$_saved_python" ]; then
    export PYTHON="$_saved_python"
    log "USE_VENV=1 install target PYTHON=$PYTHON (libpython base OHOS_REAL_PYTHON=$OHOS_REAL_PYTHON)"
  else
    export PYTHON="$(canonicalize_python "${PYTHON:-$OHOS_REAL_PYTHON}")"
  fi
  export_pip_build_env
  install_ohos_python_site_hook
  verify_python_toolchain
  verify_native_libs
  verify_system_deps

  if [ -z "${CC:-}" ] && command -v clang >/dev/null 2>&1; then
    export CC=$(command -v clang)
    export CXX=$(command -v clang++ 2>/dev/null || command -v clang)
    log "CC=$CC CXX=$CXX"
  fi

  export PIP_NO_BUILD_ISOLATION
  log "toolchain:"
  log_tool clang
  log_tool rustc
  log_tool cargo
  log_tool maturin
  log "CC=${CC:-unset} CXX=${CXX:-unset}"
  log "RUSTFLAGS=${RUSTFLAGS:-unset}"
  log "PIP_NO_BUILD_ISOLATION=$PIP_NO_BUILD_ISOLATION"

  if [ "$PIP_NO_BUILD_ISOLATION" = "1" ]; then
    bootstrap_pep517
    bootstrap_native_wheels
  else
    log "SKIP bootstrap PEP517 (PIP_NO_BUILD_ISOLATION=$PIP_NO_BUILD_ISOLATION)"
    verify_pep517_backends
    patch_pep517_site_packages
    verify_pep517_subprocess
  fi
}

preload_local_wheels() {
  [ -n "$WHEEL_DIR" ] && [ -d "$WHEEL_DIR" ] || return 0
  _plat=$(detect_wheel_platform_tag)
  log "preload wheels from $WHEEL_DIR (platform tag: $_plat)"

  for _base in cryptography pydantic_core rpds_py numpy greenlet tiktoken jiter lxml lupa; do
    _found=
    for _w in "$WHEEL_DIR"/${_base}-*-"${_plat}".whl "$WHEEL_DIR"/${_base}-*-ohos_aarch64.whl; do
      if [ -f "$_w" ]; then
        log "preload wheel: $_w"
        pip_install --force-reinstall "$_w" >>"$LOG" 2>&1 || true
        _found=1
        break
      fi
    done
    if [ -z "$_found" ]; then
      log "preload skip $_base (no *-${_plat}.whl in WHEEL_DIR)"
    fi
  done
  pip_install "pydantic>=2.11" >>"$LOG" 2>&1 || true
}

normalize_native_extension_permissions() {
  _site_packages=$("$PYTHON" -c 'import site; print(site.getsitepackages()[0])' 2>/dev/null || true)
  if [ -z "$_site_packages" ] || [ ! -d "$_site_packages" ]; then
    log "WARN: skip native permission normalization (site-packages unavailable)"
    return 0
  fi

  # HNP deployment paths can preserve wheels with non-executable shared
  # objects. Keep the correction scoped to this virtual environment.
  if find "$_site_packages" -type f -name '*.so*' ! -perm -111 -exec chmod 755 {} \; 2>/dev/null \
    && find "$_site_packages" -type d ! -perm -111 -exec chmod 755 {} \; 2>/dev/null; then
    log "native extension permissions normalized: $_site_packages"
  else
    log "WARN: unable to normalize native extension permissions: $_site_packages"
  fi
}

pip_install() {
  export_pip_build_env
  write_ohos_toolchain_env
  _nbi=
  if [ "$PIP_NO_BUILD_ISOLATION" = "1" ]; then
    _nbi=--no-build-isolation
  fi
  _find_links=
  if [ -n "${WHEEL_DIR:-}" ] && [ -d "$WHEEL_DIR" ]; then
    _find_links="--find-links $WHEEL_DIR --prefer-binary"
  fi
  # 榛樿璧版竻鍗庨暅鍍忥紝瑙勯伩 pypi.org DNS/鎱㈤€熶笅杞介棶棰橈紱鐜鍙橀噺鍙鐩?
  : "${PIP_INDEX_URL:=https://pypi.tuna.tsinghua.edu.cn/simple}"
  : "${PIP_TRUSTED_HOST:=pypi.tuna.tsinghua.edu.cn}"
  _index_args="--index-url $PIP_INDEX_URL --trusted-host $PIP_TRUSTED_HOST"

  # OhOS/HNP Python 渚濊禆鐖?shell 涓畬鏁寸殑杩愯鏃剁幆澧冦€?
  # 涓嶄娇鐢ㄥ閮?`env ... $PYTHON` 鍖呰锛岄伩鍏?HiShell/HNP 涓嬪姩鎬佸姞杞界幆澧冨彂鐢熷彉鍖栵紝
  # 瀵艰嚧 libpython3.12.so.1.0 / libintl.so.8 鏃犳硶瑙ｆ瀽銆?
  export RUSTC="${RUSTC:-}"
  export CARGO="${CARGO:-}"
  export MATURIN="${MATURIN:-}"
  export CC="${CC:-}"
  export CXX="${CXX:-}"
  export AR="${AR:-}"
  export RANLIB="${RANLIB:-}"
  export RUSTFLAGS="${RUSTFLAGS:-}"
  export OPENSSL_DIR="${OPENSSL_DIR:-}"
  export PKG_CONFIG_PATH="${PKG_CONFIG_PATH:-}"
  export WHEEL_BUILD_ROOT="${WHEEL_BUILD_ROOT:-}"
  export OHOS_TOOLCHAIN_ENV="${OHOS_TOOLCHAIN_ENV:-}"
  export PYTHONPATH="${PYTHONPATH:-}"
  export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}"
  export PIP_INDEX_URL PIP_TRUSTED_HOST PATH

  "$PYTHON" -m pip install --no-cache-dir $_nbi $_find_links $_index_args "$@"
  _pip_status=$?
  [ "$_pip_status" -eq 0 ] && normalize_native_extension_permissions
  return "$_pip_status"
}

try_import() {
  mod=$1
  export_pip_build_env
  _libdir=$("${OHOS_REAL_PYTHON:-$PYTHON}" -c 'import sysconfig; print(sysconfig.get_config_var("LIBDIR") or "")' 2>/dev/null || true)
  _detected_ld=
  if [ -n "${OPENSSL_DIR:-}" ] && [ -d "${OPENSSL_DIR}/lib" ]; then
    _detected_ld="${OPENSSL_DIR}/lib"
  fi
  if [ -n "$_libdir" ] && [ -d "$_libdir" ]; then
    _detected_ld="${_detected_ld:+${_detected_ld}:}${_libdir}"
  fi
  _native_ld=${LD_LIBRARY_PATH:-}
  if [ -n "$_detected_ld" ]; then
    _native_ld="${_native_ld:+${_native_ld}:}${_detected_ld}"
  fi

  # 浣跨敤 POSIX shell 鐨勪复鏃跺彉閲忚祴鍊硷紝涓嶈皟鐢ㄥ閮?env 绋嬪簭銆?
  # 杩欐牱鏃㈣兘涓哄綋鍓?import 瑕嗙洊 native lib 璺緞锛屽張涓嶄細鐮村潖 OhOS/HNP 杩愯鏃剁幆澧冦€?
  LD_LIBRARY_PATH="${_native_ld}" \
  OPENSSL_DIR="${OPENSSL_DIR:-}" \
  "$PYTHON" -c "
import importlib, sys
try:
    importlib.import_module('$mod')
    print('IMPORT_OK')
except Exception as e:
    print(f'IMPORT_FAIL:{type(e).__name__}:{e}')
"
}

extract_fail_detail() {
  # 浠?pip 鏃ュ織灏鹃儴鎻愬彇瀛愪緷璧?缂栬瘧澶辫触鍖呭悕
  tail -n 80 "$LOG" 2>/dev/null | sed -n \
    -e "s/.*Cannot import '\\([^']*\\)'.*/SUBDEP:\1 (pep517)/p" \
    -e 's/.*Could not build wheels for \([^, ]*\).*/SUBDEP:\1/p' \
    -e 's/.*Failed building wheel for \([^ ]*\).*/SUBDEP:\1/p' \
    -e 's/.*ERROR: Failed building wheel for \([^ ]*\).*/SUBDEP:\1/p' \
    -e 's/.*No matching distribution found for \([^ ]*\).*/SUBDEP:\1/p' \
    -e 's/.*libxml2 and libxslt development packages are installed.*/SUBDEP:libxml2-dev/p' \
    -e 's/.*pg_config executable not found.*/SUBDEP:pg_config/p' \
    -e 's/.*Could not find system pdfium.*/SUBDEP:pdfium/p' \
    -e 's/.*libluajit\.a.*/SUBDEP:lupa-luajit/p' \
    -e 's/.*symbol not found.*/SUBDEP:native-link(python)/p' \
    -e "s/.*FileNotFoundError.*'maturin'.*/SUBDEP:maturin-PATH/p" \
    -e 's/.*can.t find Rust compiler.*/SUBDEP:rustc-PATH/p' \
    -e 's/.*libffi\.so.*/SUBDEP:libffi-LD_LIBRARY_PATH/p' \
    -e 's/.*openssl\.pc.*/SUBDEP:openssl-PKG_CONFIG/p' \
    -e 's/.*OPENSSL_DIR unset.*/SUBDEP:openssl-OPENSSL_DIR/p' \
    | sed 's/[][]//g' | awk -F: '!seen[$0]++{printf "%s%s", (n++?",":""), $0}' RS= ORS=
}

log "渚濊禆閫愬寘瀹夎锛圥yPI 娓呭崟锛屽惈浼犻€掍緷璧栵級"
# Repair the loader environment before venv creation/reuse. The base HNP
# Python must be able to start before any pip or PEP517 subprocess is spawned.
ensure_native_lib_env
ensure_install_venv
export PYTHON="${INSTALL_VENV_PYTHON:-$PYTHON}"
log "SCRIPT_ID=$INSTALL_SCRIPT_ID"
log "PYTHON=$PYTHON ($("$PYTHON" --version 2>&1))"
if python_under_venv "$PYTHON"; then
  _sp=$("$PYTHON" -c 'import site; print(site.getsitepackages()[0])' 2>/dev/null || true)
  log "site-packages=${_sp:-unset}"
fi
log "MANIFEST=$MANIFEST (profile=${MANIFEST_PROFILE:-custom})"
log "LOG=$LOG"
log "SUMMARY=$SUMMARY"
log "WHEEL_DIR=${WHEEL_DIR:-unset}"
log "WHEEL_BUILD_ROOT=${WHEEL_BUILD_ROOT:-unset}"

setup_build_env

printf 'project\tcategory\tpip_spec\timport_module\tinstall\timport\tfail_detail\tnote\n' >"$SUMMARY"

if [ "$SKIP_WHEEL_PRELOAD" != "1" ]; then
  _wheel_preload="${OHOS_WHEEL_PRELOADER:-$SCRIPT_DIR/ohos-wheel-preload.sh}"
  if [ -f "$_wheel_preload" ] && [ -n "${WHEEL_DIR:-}" ] && [ -d "$WHEEL_DIR" ]; then
    log "preload wheels via $_wheel_preload"
    REPORT_DIR="$REPORT_DIR" PYTHON="$PYTHON" WHEEL_DIR="$WHEEL_DIR" \
      OHOS_REAL_PYTHON="${OHOS_REAL_PYTHON:-}" REUSE_INSTALLED="$REUSE_INSTALLED" \
      FORCE_REINSTALL="${FORCE_REINSTALL:-0}" sh "$_wheel_preload" >>"$LOG" 2>&1 \
      || log "WARN: wheel preload failed (see log)"
  else
    preload_local_wheels
  fi
else
  log "SKIP wheel preload (SKIP_WHEEL_PRELOAD=1)"
fi

SEEN="|"
N=0
OK=0
FAIL=0

while IFS='	' read -r project category spec import_mod note || [ -n "${project:-}" ]; do
  # UTF-8 BOM锛圵indows 缂栬緫 manifest 鏃跺父瑙侊級
  project=${project#$(printf '\357\273\277')}
  case ${project:-} in
    ''|project) continue ;;
    \#*) continue ;;
  esac
  case $SEEN in
    *"|$spec|"*) continue ;;
  esac
  SEEN="${SEEN}${spec}|"
  N=$((N + 1))
  imp=

  log "========================================"
  log "[$N] $project | $spec"

  pre_imp=$(try_import "$import_mod" 2>>"$LOG")
  if [ "$REUSE_INSTALLED" = "1" ] && [ "$pre_imp" = "IMPORT_OK" ]; then
    ist=SKIP_INSTALLED
    imp=$pre_imp
    OK=$((OK + 1))
    fail_detail=""
    log "SKIP installed: $import_mod"
  elif pip_install "$spec" >>"$LOG" 2>&1; then
    ist=INSTALL_OK
    OK=$((OK + 1))
    fail_detail=""
  else
    ist=INSTALL_FAIL
    FAIL=$((FAIL + 1))
    fail_detail=$(extract_fail_detail)
    log "INSTALL_FAIL${fail_detail:+ subdep=$fail_detail} (see log)"
    tail -n 15 "$LOG" >>"$LOG" 2>/dev/null || true
    if [ "$CONTINUE_ON_FAIL" != "1" ]; then
      exit 1
    fi
  fi

  if [ "${imp:-}" != "IMPORT_OK" ]; then
    imp=$(try_import "$import_mod" 2>>"$LOG")
  fi
  case $imp in
    IMPORT_OK) ;;
    *) log "import $import_mod: $imp" ;;
  esac

  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$project" "$category" "$spec" "$import_mod" "$ist" "$imp" "$fail_detail" "$note" >>"$SUMMARY"

  # Native import failures are diagnostic only. Some OHOS wheels cannot be
  # loaded in every shell/storage context, but pure-Python dependencies (for
  # example python-dotenv) must still be installed for AgentServer startup.

  if [ "$AUTO" != "1" ]; then
    printf 'Enter 缁х画... '
    read -r _ || true
  fi
done <"$MANIFEST"

log "========================================"
log "瀹屾垚: total=$N install_ok=$OK install_fail=$FAIL"
echo ""
echo "姹囨€? $SUMMARY"
echo "鏃ュ織: $LOG"
echo ""
echo "瀹夎澶辫触:"
echo "  grep INSTALL_FAIL $SUMMARY"
echo "import 澶辫触:"
echo "  grep IMPORT_FAIL $SUMMARY"
