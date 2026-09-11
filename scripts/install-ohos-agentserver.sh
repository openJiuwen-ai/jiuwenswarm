#!/bin/sh
# 楦胯挋 Preview锛欰gentServer 绮剧畝鏍堜竴閿畨瑁咃紙鑷寘鍚級
# 楦胯挋閮ㄧ讲鐩綍鍚嶉€氬父涓?jiuwenswarm锛圵indows 寮€鍙戜粨 jiuwenswarm_enterprise_dev 鎷疯繃鍘诲悗鍙敼鍚嶏級
# 瀵归綈鏈粨寮€鍙戞祦绋嬶細
#   1. python3 -m venv .venv && source .venv/bin/activate
#   2. preload WHEEL_DIR 涓?native wheel锛坈ryptography/lupa 绛夛紝鏈€鍏堟墽琛岋級
#   3. pip install -r requirements-harmony.txt   锛坧hase-1 閫愬寘 + import 楠岃瘉锛屽惈浼犻€掍緷璧栵級
#   4. pip install openjiuwen-harmonyos --no-deps  锛坓it 鎴栨湰鍦?agent-core/harmonyos锛?
#   5. pip install agentcore-minimal 琛ヤ緷璧?     锛坔armonyos/pyproject.toml 鈭?Phase 1锛屽惈浼犻€掍緷璧栵級
#   6. pip install openjiuwen_deepsearch --no-deps锛堥缚钂欐棤 pypdfium2 wheel锛涢€氱敤渚濊禆鐢?phase-1 鎻愪緵锛?
#   7. pip install --no-deps -e .                 锛坖iuwenclaw 鏈綋锛?
#
# 鐢ㄦ硶锛堥缚钂?HiShell锛?
#   cd /storage/Users/currentUser/officeClaw/jiuwenswarm
#   export OHOS_REAL_PYTHON=/storage/Users/currentUser/usr/local/bin/python3.12
#   export WHEEL_DIR=/storage/Users/currentUser/officeClaw/jiuwenswarm/wheels
#   sed -i 's/\r$//' scripts/*.sh scripts/ohos/*.sh
#   sh scripts/install-ohos-agentserver.sh
#
# 鐜鍙橀噺:
#   OHOS_REAL_PYTHON   cmd-pkgs Python锛堢紪 wheel / libpython 鐢級
#   WHEEL_DIR          wheels/锛堥缂?native wheel锛岄粯璁?$REPO_ROOT/wheels锛?
#   AGENT_CORE_PATH    鏈湴 agent-core锛圲SE_LOCAL_OPENJIUWEN=1 鏃讹級
#   GIT_EXECUTABLE     鏄惧紡鎸囧畾 git 璺緞锛坧ip git+ 渚濊禆 clone 鏃剁敤锛?
#   OPENJIUWEN_USE_HARMONYOS_PYPROJECT=1  榛樿浠?clone 鐨?harmonyos/pyproject.toml 鍋?-e 瀹夎
#   USE_LOCAL_OPENJIUWEN=1  鏈湴 pip install --no-deps -e $AGENT_CORE_PATH
#   OPENJIUWEN_GIT_REPO / OPENJIUWEN_GIT_REF  榛樿 openJiuwen/agent-core @ enterprise-dev
#   DEEPSEARCH_PATH     鏈湴 deepsearch 浠撳簱锛堜紭鍏堜簬 Git锛涗粨搴撴牴鎴?deepsearch/ 瀛愰」鐩級
#   DEEPSEARCH_GIT_REPO / DEEPSEARCH_GIT_REF  榛樿 openJiuwen/deepsearch @ enterprise_dev
#   DEEPSEARCH_NO_DEPS=1  楦胯挋榛樿涓嶅畨瑁?DeepSearch 鐨勬闈㈢浼犻€掍緷璧栵紙璁句负 0 鍙仮澶嶅畬鏁翠緷璧栧畨瑁咃級
#   SKIP_DEEPSEARCH=0   鏄惧紡瀹夎 DeepSearch锛堥缚钂欓粯璁よ烦杩囷紝鍩虹瀵硅瘽涓嶄緷璧栧畠锛?
#   REUSE_INSTALLED=1   import 姝ｅ父鏃惰烦杩囧凡瀹夎渚濊禆鍜岃繍琛屾椂锛堥粯璁?1锛?
#   FORCE_REINSTALL=1   蹇界暐澶嶇敤妫€鏌ワ紝寮哄埗閲嶆柊瀹夎
#   CREATE_VENV=1      榛樿鍒涘缓 $REPO_ROOT/.venv
#   SKIP_PHASE0/1/2/3/4  璺宠繃瀵瑰簲闃舵锛坧hase 0 = wheels 棰勮锛?
#   CONTINUE_ON_FAIL=1 鍗曞寘澶辫触缁х画锛堥粯璁?1锛?

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
OHOS_DIR="$SCRIPT_DIR/ohos"
export OHOS_ENV_SCRIPTS_DIR="$OHOS_DIR"
# shellcheck disable=SC1091
. "$OHOS_DIR/ohos-env.sh"
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
export OHOS_REPO_ROOT="$REPO_ROOT"
OFFICE_CLAW=${OFFICE_CLAW:-$(CDPATH= cd -- "$REPO_ROOT/.." && pwd)}
WHEEL_BUILD_ROOT=${WHEEL_BUILD_ROOT:-$REPO_ROOT}
WHEEL_DIR=${WHEEL_DIR:-$WHEEL_BUILD_ROOT/wheels}
REPORT_DIR=${REPORT_DIR:-$REPO_ROOT/ohos-install-reports}
CREATE_VENV=${CREATE_VENV:-1}
VENV_DIR=${VENV_DIR:-$REPO_ROOT/.venv}
RECREATE_VENV=${RECREATE_VENV:-0}
CONTINUE_ON_FAIL=${CONTINUE_ON_FAIL:-1}
USE_LOCAL_OPENJIUWEN=${USE_LOCAL_OPENJIUWEN:-0}
# PRODUCTION=1 (榛樿): 鐢ㄩ潪 editable 瀹夎(婧愮爜澶嶅埗杩?site-packages),鐢ㄤ簬鎵撳寘鍦烘櫙
# PRODUCTION=0: 寮€鍙戞ā寮?淇濈暀 -e 浠ヤ究鏀规簮鐮佺珛鍗崇敓鏁?
PRODUCTION=${PRODUCTION:-1}
if [ "$PRODUCTION" = "1" ]; then
  EDITABLE_FLAG=""
else
  EDITABLE_FLAG="-e"
fi
AGENT_CORE_PATH=${AGENT_CORE_PATH:-$OFFICE_CLAW/agent-core}
OPENJIUWEN_GIT_REPO=${OPENJIUWEN_GIT_REPO:-https://gitcode.com/openJiuwen/agent-core.git}
OPENJIUWEN_GIT_REF=${OPENJIUWEN_GIT_REF:-enterprise-dev}
OPENJIUWEN_SPEC=${OPENJIUWEN_SPEC:-openjiuwen-harmonyos @ git+${OPENJIUWEN_GIT_REPO}@${OPENJIUWEN_GIT_REF}#subdirectory=harmonyos}
DEEPSEARCH_PATH=${DEEPSEARCH_PATH:-}
DEEPSEARCH_GIT_REPO=${DEEPSEARCH_GIT_REPO:-https://gitcode.com/openJiuwen/deepsearch.git}
DEEPSEARCH_GIT_REF=${DEEPSEARCH_GIT_REF:-enterprise_dev}
DEEPSEARCH_SRC_DIR=${DEEPSEARCH_SRC_DIR:-$REPO_ROOT/.cache/deepsearch-src}
DEEPSEARCH_NO_DEPS=${DEEPSEARCH_NO_DEPS:-1}
SKIP_DEEPSEARCH=${SKIP_DEEPSEARCH:-1}
REUSE_INSTALLED=${REUSE_INSTALLED:-1}
FORCE_REINSTALL=${FORCE_REINSTALL:-0}

DEPS_INSTALLER=${OHOS_DEPS_INSTALLER:-$OHOS_DIR/install-ohos-all-deps.sh}
WHEEL_PRELOADER=${OHOS_WHEEL_PRELOADER:-$OHOS_DIR/ohos-wheel-preload.sh}

log() {
  printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$1"
}

die() {
  log "ERROR: $*"
  exit 1
}

resolve_base_python() {
  if [ -n "${OHOS_REAL_PYTHON:-}" ]; then
    echo "$OHOS_REAL_PYTHON"
    return 0
  fi
  if [ -n "${PYTHON:-}" ]; then
    echo "$PYTHON"
    return 0
  fi
  if command -v python3.12 >/dev/null 2>&1; then
    command -v python3.12
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  return 1
}

run_manifest_phase() {
  _phase=$1
  _profile=$2
  [ -f "$DEPS_INSTALLER" ] || die "deps installer not found: $DEPS_INSTALLER"

  log "======== phase $_phase: $_profile (generated manifest) ========"
  mkdir -p "$REPORT_DIR/phase-$_phase"
  _harmonyos=
  for _p in \
    "${OPENJIUWEN_SRC_DIR:-$REPO_ROOT/.cache/openjiuwen-src}/harmonyos/pyproject.toml" \
    "${AGENT_CORE_PATH:-}/harmonyos/pyproject.toml" \
    "$OFFICE_CLAW/agent-core/harmonyos/pyproject.toml"; do
    [ -f "$_p" ] || continue
    _harmonyos="$_p"
    break
  done
  MANIFEST="" \
    MANIFEST_PROFILE="$_profile" \
    HARMONYOS_PYPROJECT="${_harmonyos:-}" \
    REPORT_DIR="$REPORT_DIR/phase-$_phase" \
    PYTHON="$PYTHON" \
    OHOS_REAL_PYTHON="$OHOS_REAL_PYTHON" \
    USE_VENV=1 \
    VENV_DIR="$VENV_DIR" \
    CREATE_VENV="${CREATE_VENV:-1}" \
    RECREATE_VENV="${RECREATE_VENV:-0}" \
    OFFICE_CLAW="$OFFICE_CLAW" \
    AGENT_CORE_PATH="$AGENT_CORE_PATH" \
    OPENJIUWEN_SRC_DIR="${OPENJIUWEN_SRC_DIR:-$REPO_ROOT/.cache/openjiuwen-src}" \
    WHEEL_BUILD_ROOT="$WHEEL_BUILD_ROOT" \
    WHEEL_DIR="$WHEEL_DIR" \
    SKIP_WHEEL_PRELOAD=1 \
    AUTO=1 \
    CONTINUE_ON_FAIL="$CONTINUE_ON_FAIL" \
    REUSE_INSTALLED="$REUSE_INSTALLED" \
    sh "$DEPS_INSTALLER"
}

runtime_imports_ok() {
  "$PYTHON" -c "$1" >/dev/null 2>&1
}

pip_in_venv() {
  ensure_ohos_tool_path
  # 涓嶇敤 env 鍖呬竴灞傦細閮ㄥ垎 OhOS 涓?env 瀛愯繘绋?PATH 涓庣埗 shell 涓嶄竴鑷达紝pip 璋?git 浼?ENOENT
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
  export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}"
  export PATH
  # 榛樿璧版竻鍗庨暅鍍忥紝瑙勯伩 pypi.org DNS 闂锛涘彲鐢辫皟鐢ㄦ柟瑕嗙洊
  : "${PIP_INDEX_URL:=https://pypi.tuna.tsinghua.edu.cn/simple}"
  : "${PIP_TRUSTED_HOST:=pypi.tuna.tsinghua.edu.cn}"
  export PIP_INDEX_URL PIP_TRUSTED_HOST
  "$PYTHON" -m pip install --no-cache-dir --no-build-isolation \
    --index-url "$PIP_INDEX_URL" --trusted-host "$PIP_TRUSTED_HOST" "$@"
}

ensure_pep517_minimal() {
  if "$PYTHON" -c "import setuptools, wheel" 2>/dev/null; then
    log "PEP517: setuptools+wheel OK"
    return 0
  fi
  log "PEP517 bootstrap: pip install -U pip setuptools wheel (SKIP_PHASE1 鎴栨湭璺?deps installer 鏃堕渶瑕?"
  "$PYTHON" -m pip install --no-cache-dir -U pip setuptools wheel \
    || die "PEP517 bootstrap failed (pip setuptools wheel)"
}

resolve_openjiuwen_install_dir() {
  _src=$1
  if [ "${OPENJIUWEN_USE_HARMONYOS_PYPROJECT:-1}" = "1" ] \
    && [ -f "$_src/harmonyos/pyproject.toml" ]; then
    echo "$_src/harmonyos"
    return 0
  fi
  echo "$_src"
}

_prepend_path_dir() {
  _dir=$1
  [ -n "$_dir" ] || return 0
  [ -d "$_dir" ] || return 0
  case ":${PATH}:" in
    *":$_dir:"*) ;;
    *) PATH="$_dir:$PATH" ;;
  esac
}

# HiShell 浜や簰寮?PATH 鍚?git锛涢潪浜や簰 sh 闇€ ohos-env.sh + 鏈嚱鏁拌ˉ venv/bin
ensure_ohos_tool_path() {
  # shellcheck disable=SC1091
  . "$OHOS_DIR/ohos-env.sh"
  if [ -n "${VENV_DIR:-}" ] && [ -d "$VENV_DIR/bin" ]; then
    _prepend_path_dir "$VENV_DIR/bin"
    export PATH
  fi
}

find_cmd_git() {
  ensure_ohos_tool_path
  ohos_find_git
}

# patch_venv_activate_ohos 瀹氫箟浜?ohos-env.sh

link_git_into_venv() {
  _git=$1
  [ -n "$_git" ] || return 0
  [ -x "$_git" ] || return 0
  [ -d "${VENV_DIR:-}/bin" ] || return 0
  # 閬垮厤鎶婁笂娆＄敓鎴愮殑 shim 褰撴垚鐪?git 鍐?wrap 涓€娆?鈫?鏃犻檺閫掑綊
  _shim="$VENV_DIR/bin/git"
  [ "$_git" = "$_shim" ] && return 0
  case "$_git" in
    "$VENV_DIR"/*) return 0 ;;
  esac
  _git_dir=$(dirname "$_git")
  # 鐢?wrapper 鑰岄潪 symlink锛歱ip 瀛愯繘绋?PATH 甯镐笉鍚?hnp锛宻him 鍐?exec 缁濆璺緞
  cat >"$_shim" <<EOF
#!/bin/sh
export PATH="$_git_dir:\${PATH:-}"
export LD_LIBRARY_PATH="${OHOS_HNP_LIB}:\${LD_LIBRARY_PATH:-}"
exec "$_git" "\$@"
EOF
  chmod +x "$_shim" 2>/dev/null || true
  _prepend_path_dir "$VENV_DIR/bin"
  export PATH
}

clone_openjiuwen_repo() {
  _git=$(find_cmd_git) || return 1
  _repo="$OPENJIUWEN_GIT_REPO"
  _ref="$OPENJIUWEN_GIT_REF"
  _dest="${OPENJIUWEN_SRC_DIR:-$REPO_ROOT/.cache/openjiuwen-src}"
  _log="${REPORT_DIR}/phase-2/git-clone.log"
  mkdir -p "$(dirname "$_dest")" "$REPORT_DIR/phase-2"
  if [ -d "$_dest/.git" ]; then
    log "refresh clone: $_dest ($_ref)"
    # checkout -B 閲嶇疆鍒?origin/$_ref锛歝heckout 鏈湴鏃у垎鏀笉浼氬洜 fetch 鍓嶈繘锛?
    # refresh 浼氬亣鎴愬姛骞朵竴鐩磋鏃?commit锛堥缚钂欐墦鍖呮満瀹炶俯锛夈€?
    "$_git" -C "$_dest" fetch --depth 1 origin "$_ref" >>"$_log" 2>&1 \
      && "$_git" -C "$_dest" checkout -B "$_ref" "origin/$_ref" >>"$_log" 2>&1 \
      && return 0
    log "WARN: git fetch failed, re-clone"
    rm -rf "$_dest"
  fi
  log "git clone -b $_ref --depth 1 $_repo -> $_dest"
  "$_git" clone --depth 1 --branch "$_ref" "$_repo" "$_dest" >>"$_log" 2>&1 || return 1
  return 0
}

resolve_deepsearch_install_dir() {
  _src=$1
  for _candidate in "$_src/deepsearch" "$_src"; do
    if [ -f "$_candidate/pyproject.toml" ] || [ -f "$_candidate/setup.py" ]; then
      echo "$_candidate"
      return 0
    fi
  done
  return 1
}

resolve_local_deepsearch_path() {
  for _candidate in \
    "${DEEPSEARCH_PATH:-}" \
    "$OFFICE_CLAW/deepsearch" \
    "$REPO_ROOT/../deepsearch"; do
    [ -n "$_candidate" ] || continue
    resolve_deepsearch_install_dir "$_candidate" >/dev/null 2>&1 || continue
    echo "$_candidate"
    return 0
  done
  return 1
}

clone_deepsearch_repo() {
  _git=$(find_cmd_git) || return 1
  _log="${REPORT_DIR}/deepsearch/git-clone.log"
  mkdir -p "$(dirname "$DEEPSEARCH_SRC_DIR")" "${REPORT_DIR}/deepsearch"
  if [ -d "$DEEPSEARCH_SRC_DIR/.git" ]; then
    log "refresh deepsearch clone: $DEEPSEARCH_SRC_DIR ($DEEPSEARCH_GIT_REF)"
    "$_git" -C "$DEEPSEARCH_SRC_DIR" fetch --depth 1 origin "$DEEPSEARCH_GIT_REF" >>"$_log" 2>&1 \
      && "$_git" -C "$DEEPSEARCH_SRC_DIR" checkout -B "$DEEPSEARCH_GIT_REF" "origin/$DEEPSEARCH_GIT_REF" >>"$_log" 2>&1 \
      && return 0
    log "WARN: deepsearch git fetch failed, re-clone"
    rm -rf "$DEEPSEARCH_SRC_DIR"
  fi
  log "git clone -b $DEEPSEARCH_GIT_REF --depth 1 $DEEPSEARCH_GIT_REPO -> $DEEPSEARCH_SRC_DIR"
  "$_git" clone --depth 1 --branch "$DEEPSEARCH_GIT_REF" \
    "$DEEPSEARCH_GIT_REPO" "$DEEPSEARCH_SRC_DIR" >>"$_log" 2>&1
}

install_deepsearch() {
  if [ "$REUSE_INSTALLED" = "1" ] && [ "$FORCE_REINSTALL" != "1" ] \
    && runtime_imports_ok "import openjiuwen_deepsearch; from openjiuwen_deepsearch.config.config import Config"; then
    log "SKIP installed: openjiuwen_deepsearch"
    return 0
  fi
  ensure_pep517_minimal
  _src=
  if _local=$(resolve_local_deepsearch_path); then
    _src=$_local
    log "浣跨敤鏈湴 openjiuwen_deepsearch: $_src"
  else
    find_cmd_git >/dev/null 2>&1 \
      || die "DeepSearch 闇€瑕?git 鎴栨湰鍦版簮鐮侊紱璇疯缃?DEEPSEARCH_PATH=/path/to/deepsearch"
    clone_deepsearch_repo \
      || die "openjiuwen_deepsearch clone failed (see $REPORT_DIR/deepsearch/git-clone.log)"
    _src=$DEEPSEARCH_SRC_DIR
  fi

  _install=$(resolve_deepsearch_install_dir "$_src") \
    || die "openjiuwen_deepsearch package metadata not found under: $_src"
  if [ "$DEEPSEARCH_NO_DEPS" = "1" ]; then
    # pypdfium2 currently has no OpenHarmony wheel and its source build pulls
    # desktop-only pdfium/Chromium sources. Keep the OHOS bundle installable;
    # shared pure-Python dependencies come from requirements-harmony.txt.
    log "pip install --no-deps $EDITABLE_FLAG $_install (OHOS-compatible runtime)"
    pip_in_venv --no-deps $EDITABLE_FLAG "$_install" \
      || die "openjiuwen_deepsearch install failed"
  else
    log "pip install $EDITABLE_FLAG $_install (including runtime dependencies)"
    pip_in_venv $EDITABLE_FLAG "$_install" \
      || die "openjiuwen_deepsearch install failed"
  fi

  if ! "$PYTHON" -c "import openjiuwen_deepsearch; from openjiuwen_deepsearch.config.config import Config" >/dev/null 2>&1; then
    die "openjiuwen_deepsearch import verification failed after installation"
  fi
  log "openjiuwen_deepsearch import: OK"
}

resolve_agent_core_path() {
  if [ -n "${AGENT_CORE_PATH:-}" ] && [ -d "$AGENT_CORE_PATH" ]; then
    echo "$AGENT_CORE_PATH"
    return 0
  fi
  for _p in \
    "$OFFICE_CLAW/agent-core" \
    "$OFFICE_CLAW/agent-core_5969" \
    "$REPO_ROOT/../agent-core" \
    "$REPO_ROOT/vendor/openjiuwen"
  do
    if [ -f "$_p/pyproject.toml" ] || [ -f "$_p/setup.py" ] || [ -f "$_p/harmonyos/pyproject.toml" ]; then
      echo "$_p"
      return 0
    fi
  done
  return 1
}

install_openjiuwen() {
  # 鏃х増 openjiuwen 涔熻兘瑁?import 鎴愬姛鈥斺€斿繀椤婚『甯﹂獙璇?agent_teams 鍏抽敭妯″潡锛?
  # 鍚﹀垯 2026-08 鐨勬棫 .venv 浼氳璇垽"宸插畨瑁?鑰岃烦杩囨洿鏂帮紙楦胯挋鎵撳寘鏈哄疄韪╋細
  # 閲嶈鍚庝粛缂?agent_teams/context.py锛屾瀯寤?Phase 2b 鏍￠獙澶辫触锛夈€?
  if [ "$REUSE_INSTALLED" = "1" ] && [ "$FORCE_REINSTALL" != "1" ] \
    && runtime_imports_ok "import openjiuwen; import openjiuwen.agent_teams.context"; then
    log "SKIP installed: openjiuwen (agent_teams OK)"
    return 0
  fi
  ensure_pep517_minimal

  # 绂荤嚎 wheel 浼樺厛锛堜笌 openjiuwen_deepsearch 鏈綋鐨勫畨瑁呯瓥鐣ヤ竴鑷达級锛歸heel 鏄?
  # 2026-09 鎵嬫満瀹炴祴锛堜笓瀹跺洟 + DeepSearch 鍏ㄩ€氾級鐨?0.1.10 绮剧‘蹇収銆俛gent-core
  # 鐨?enterprise-dev 鍒嗘敮鎸佺画閲嶆瀯锛坔armonyos/ 宸叉槸绾?pyproject 澹筹紝浠ｇ爜鎸埌
  # 浠撳簱鏍癸紝鏂囦欢浣嶇疆婕傜Щ锛夛紝git/鏈湴婧愮殑鐗堟湰涓庣洰褰曠粨鏋勪笉鍙帶锛涚绾?wheel
  # 淇濊瘉鍚勬墦鍖呮満瑁呭嚭鐨?openjiuwen 涓庡疄娴嬬増鏈€愬瓧鑺備竴鑷淬€?
  for _w in "$WHEEL_DIR"/openjiuwen_harmonyos-*.whl; do
    [ -f "$_w" ] || continue
    if [ "$(head -c 2 "$_w" 2>/dev/null)" != "PK" ]; then
      log "WARN: openjiuwen wheel 鏄?Git LFS 鎸囬拡锛堟湰鏈烘湭瑁?git-lfs锛夛紝璺宠繃绂荤嚎瀹夎: $_w"
      continue
    fi
    log "openjiuwen: 瀹夎绂荤嚎 wheel ($(basename "$_w"))"
    pip_in_venv --no-deps --force-reinstall "$_w" \
      || die "openjiuwen 绂荤嚎 wheel 瀹夎澶辫触: $_w"
    # 鏂囦欢绾ч獙璇侊紙涓嶇敤 import 楠岃瘉锛氬彈闄愮粓绔棤娉?dlopen .so锛宨mport aiohttp
    # 蹇呯劧 Permission denied锛屼細璇姤澶辫触鈥斺€旇 install-deepsearch-ohos.sh 閲戜笣闆€锛?
    [ -f "$VENV_DIR/lib/python3.12/site-packages/openjiuwen/agent_teams/context.py" ] \
      || die "openjiuwen 瑁呭悗楠岃瘉澶辫触锛歛gent_teams/context.py 涓嶅瓨鍦?
    log "openjiuwen: 绂荤嚎 wheel 瀹夎楠岃瘉 OK (agent_teams/context.py)"
    return 0
  done

  if [ "$USE_LOCAL_OPENJIUWEN" = "1" ]; then
    _local=$(resolve_agent_core_path) || die "USE_LOCAL_OPENJIUWEN=1 but AGENT_CORE_PATH not found (set AGENT_CORE_PATH=$OFFICE_CLAW/agent-core)"
    _install=$(resolve_openjiuwen_install_dir "$_local")
    log "pip install --no-deps $EDITABLE_FLAG $_install"
    pip_in_venv --no-deps $EDITABLE_FLAG "$_install" || die "openjiuwen local install failed"
    return 0
  fi

  if _git=$(find_cmd_git); then
    link_git_into_venv "$_git"
    export PATH="$(dirname "$_git")${PATH:+:$PATH}"
    log "git: $_git ($("$_git" --version 2>&1 | head -1))"
    log "PATH (git dir prepended): $(echo "$PATH" | tr ':' '\n' | head -5 | tr '\n' ':')..."
    _src="${OPENJIUWEN_SRC_DIR:-$REPO_ROOT/.cache/openjiuwen-src}"
    if clone_openjiuwen_repo; then
      _install=$(resolve_openjiuwen_install_dir "$_src")
      log "pip install --no-deps $EDITABLE_FLAG $_install  (manual git clone, bypass pip git+)"
      pip_in_venv --no-deps $EDITABLE_FLAG "$_install" || die "openjiuwen local install from clone failed"
    else
      log "WARN: git clone failed, fallback pip git+ spec"
      log "pip install --no-deps \"$OPENJIUWEN_SPEC\""
      pip_in_venv --no-deps "$OPENJIUWEN_SPEC" || die "openjiuwen git install failed"
    fi
    return 0
  fi

  if _local=$(resolve_agent_core_path); then
    log "WARN: git not in PATH; fallback to local agent-core: $_local"
    _install=$(resolve_openjiuwen_install_dir "$_local")
    log "pip install --no-deps $EDITABLE_FLAG $_install"
    pip_in_venv --no-deps $EDITABLE_FLAG "$_install" || die "openjiuwen local install failed"
    return 0
  fi

  die "git not found and no local agent-core. Options:
  1) export PATH=${OHOS_HNP_BIN}:\$PATH   (cmd-pkgs git 甯歌璺緞)
  2) export GIT_EXECUTABLE=/path/to/git
  3) copy agent-core to $OFFICE_CLAW/agent-core then USE_LOCAL_OPENJIUWEN=1
  4) Windows zip: https://gitcode.com/openJiuwen/agent-core/-/tree/enterprise-dev"
}

run_wheel_preload_phase() {
  [ -f "$WHEEL_PRELOADER" ] || die "wheel preloader not found: $WHEEL_PRELOADER"
  mkdir -p "$REPORT_DIR/phase-0"
  log "======== phase 0: preload native wheels (first) ========"
  REPORT_DIR="$REPORT_DIR/phase-0" \
    PYTHON="$PYTHON" \
    WHEEL_DIR="$WHEEL_DIR" \
    OHOS_REAL_PYTHON="$OHOS_REAL_PYTHON" \
    REUSE_INSTALLED="$REUSE_INSTALLED" \
    FORCE_REINSTALL="$FORCE_REINSTALL" \
    sh "$WHEEL_PRELOADER" || die "native wheel preload failed"
}

# ---------- 0. 瑙ｆ瀽 Python / venv ----------
BASE_PY=$(resolve_base_python) || die "set OHOS_REAL_PYTHON=/path/to/python3.12"
BASE_PY=$(readlink -f "$BASE_PY" 2>/dev/null || echo "$BASE_PY")
export OHOS_REAL_PYTHON="$BASE_PY"

if [ "$CREATE_VENV" = "1" ]; then
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    log "create venv: $VENV_DIR (base $BASE_PY)"
    "$BASE_PY" -m venv "$VENV_DIR"
  else
    log "reuse venv: $VENV_DIR"
  fi
  export PYTHON="$VENV_DIR/bin/python"
  # 鍕?readlink -f venv/bin/python锛氫細瑙ｆ瀽鍒?OHOS_REAL_PYTHON锛宲ip 瑁呭埌绯荤粺 Python
  patch_venv_activate_ohos "$VENV_DIR"
else
  export PYTHON="${PYTHON:-$BASE_PY}"
  PYTHON=$(readlink -f "$PYTHON" 2>/dev/null || echo "$PYTHON")
  export PYTHON
  log "CREATE_VENV=0 PYTHON=$PYTHON"
fi

log "install-ohos-agentserver (2026-05-30-wheels-first)"
log "REPO_ROOT=$REPO_ROOT"
log "OFFICE_CLAW=$OFFICE_CLAW"
log "WHEEL_DIR=$WHEEL_DIR"
log "PYTHON=$PYTHON ($("$PYTHON" --version 2>&1))"
log "OHOS_REAL_PYTHON=$OHOS_REAL_PYTHON"
ensure_ohos_tool_path
if _git_probe=$(find_cmd_git 2>/dev/null); then
  log "git probe: $_git_probe ($("$_git_probe" --version 2>&1 | head -1))"
else
  log "git probe: NOT FOUND (phase 2 灏嗗皾璇曟湰鍦?agent-core 鎴栨姤閿?"
fi

# ---------- 0. preload native wheels锛堟渶鍏堬紝閬垮厤鍚庣画 pip 鎷?PyPI 瑕嗙洊锛?---------
if [ "${SKIP_PHASE0:-0}" != "1" ]; then
  run_wheel_preload_phase
else
  log "SKIP phase 0 (native wheels preload)"
fi

# ---------- 1. requirements-harmony锛堥€愬寘 + 浼犻€掍緷璧栵級----------
if [ "${SKIP_PHASE1:-0}" != "1" ]; then
  run_manifest_phase 1 agentserver-minimal
else
  log "SKIP phase 1 (requirements-harmony manifest)"
fi

# ---------- 2. openjiuwen --no-deps ----------
if [ "${SKIP_PHASE2:-0}" != "1" ]; then
  log "======== phase 2: openjiuwen --no-deps ========"
  install_openjiuwen
  if ! "$PYTHON" -c "import openjiuwen; print('openjiuwen OK', openjiuwen.__file__)" 2>/dev/null; then
    log "WARN: import openjiuwen failed (may need phase 3 deps)"
  fi
else
  log "SKIP phase 2 (openjiuwen)"
fi

# ---------- 3. agentcore-minimal锛坔armonyos/pyproject.toml 鈭?requirements-harmony锛屽惈浼犻€掍緷璧栵級----------
if [ "${SKIP_PHASE3:-0}" != "1" ]; then
  run_manifest_phase 3 agentcore-minimal
else
  log "SKIP phase 3 (agentcore-minimal manifest)"
fi

# ---------- DeepSearch锛堥缚钂欏彲閫夎兘鍔涳紝榛樿璺宠繃锛?---------
if [ "$SKIP_DEEPSEARCH" != "1" ]; then
  log "======== phase deepsearch: openjiuwen_deepsearch + runtime deps ========"
  install_deepsearch
else
  log "SKIP DeepSearch (SKIP_DEEPSEARCH=1)"
fi

# ---------- 4. jiuwenswarm --no-deps -e . ----------
if [ "${SKIP_PHASE4:-0}" != "1" ]; then
  if [ "$REUSE_INSTALLED" = "1" ] && [ "$FORCE_REINSTALL" != "1" ] \
    && runtime_imports_ok "import jiuwenswarm" \
    && [ -x "$VENV_DIR/bin/jiuwenswarm-agentserver" ]; then
    log "SKIP installed: jiuwenswarm"
  else
    log "======== phase 4: jiuwenswarm --no-deps $EDITABLE_FLAG . ========"
    ensure_pep517_minimal
    pip_in_venv --no-deps $EDITABLE_FLAG "$REPO_ROOT" || die "jiuwenswarm install failed"
  fi
else
  log "SKIP phase 4 (jiuwenswarm -e .)"
fi

# ---------- 5. 鍚姩鍓嶉獙璇?----------
log "======== verify ========"
_fail=0
_venv_sp=$("$PYTHON" -c 'import site; print(site.getsitepackages()[0])' 2>/dev/null || true)
log "  PYTHON=$PYTHON"
log "  site-packages=${_venv_sp:-unknown}"
case "${_venv_sp:-}" in
  "$VENV_DIR"/*) ;;
  *)
    log "  WARN: site-packages 涓嶅湪 $VENV_DIR 涓?鈥?Phase 1/3 鍙兘瑁呭埌浜嗙郴缁?Python锛岃 RECREATE_VENV=1 閲嶈"
    _fail=$((_fail + 1))
    ;;
esac
_verify_ld=$(ohos_native_ld_library_path 2>/dev/null || true)
for _mod in dotenv openjiuwen openjiuwen_deepsearch openjiuwen_deepsearch.config.config jiuwenswarm pydantic pydantic_core sqlalchemy greenlet openai lupa.luajit21 fastmcp cryptography yaml fastapi mcp markdown markdown_it latex2mathml mathml2omml docx jiuwenswarm.common.platform jiuwenswarm.agents.harness.common.tools.deepresearch.tools; do
  # OhOS/HNP 涓嬩笉瑕佺敤澶栭儴 env 绋嬪簭鍖呰９ Python銆?
  if LD_LIBRARY_PATH="${_verify_ld:-${LD_LIBRARY_PATH:-}}" \
    "$PYTHON" -c "import ${_mod}" 2>/dev/null; then
    log "  import ${_mod}: OK"
  else
    log "  import ${_mod}: FAIL (warning; import verification does not block installation)"
  fi
done

if [ "$SKIP_DEEPSEARCH" != "1" ]; then
  if ! LD_LIBRARY_PATH="${_verify_ld:-${LD_LIBRARY_PATH:-}}" \
    "$PYTHON" -c "from openjiuwen_deepsearch.config.config import Config" >/dev/null 2>&1; then
    log "  openjiuwen_deepsearch: REQUIRED IMPORT FAILED"
    _fail=$((_fail + 1))
  fi
fi

if command -v jiuwenswarm-agentserver >/dev/null 2>&1; then
  log "  jiuwenswarm-agentserver: $(command -v jiuwenswarm-agentserver)"
else
  log "  jiuwenswarm-agentserver: MISSING (check venv bin in PATH)"
  _fail=$((_fail + 1))
fi

echo ""
echo "瀹屾垚銆傛姤鍛婄洰褰? $REPORT_DIR"
echo ""
echo "鍚姩 AgentServer:"
echo "  cd $REPO_ROOT"
echo "  sh scripts/start-ohos-agentserver.sh 18092"
echo "  # 鎴栵細node start-agentserver.mjs 18092"
echo ""

[ "$_fail" -eq 0 ] || exit 1
exit 0
