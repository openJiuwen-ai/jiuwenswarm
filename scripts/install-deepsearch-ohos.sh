#!/bin/sh
# ============================================================================
# install-deepsearch-ohos.sh
# DeepResearch (openjiuwen_deepsearch) 鸿蒙(HarmonyOS)一键安装脚本
#
# 用法（在 jiuwenswarm 仓库根目录执行）:
#     cd ~/officeClaw/jiuwenswarm
#     sh scripts/install-deepsearch-ohos.sh
#
# 设计说明:
#   1. 优先使用仓库 wheels/ 下的离线包 —— 把整个 jiuwenswarm 目录复制到新
#      机器即可完全离线安装（注意 wheels/ 与 scripts/ 不在 git 里，必须随目录复制）。
#   2. 离线 wheel 里的 .so 若因代码签名（本机 binary-sign-tool 自签）不被新机器
#      接受，自动用新机器的签名工具重签（scripts/ohos/ohos-musl-wheel-convert.py）。
#   3. 仍失败时从清华镜像下载 musl wheel 现场转换（需要网络）。
#   4. 幂等可重跑：已装好且功能正常的组件自动跳过。
#   5. matplotlib/seaborn 故意不装（仅图表沙箱使用）；Milvus 向量检索用桩替代
#      （真 pymilvus 依赖 grpcio 原生库，鸿蒙上无法运行）。
# ============================================================================
set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT" || exit 1

VENV_PY="$REPO_ROOT/.venv/bin/python"
WHEELS="$REPO_ROOT/wheels"
DEPS_DIR="$WHEELS/deepsearch-deps"
MUSL_DIR="$WHEELS/musl-sources"
CONVERTER="$REPO_ROOT/scripts/ohos/ohos-musl-wheel-convert.py"
SIGN_TOOL="/data/service/hnp/bin/binary-sign-tool"
MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
TMP="$REPO_ROOT/.cache/deepsearch-install-tmp"
DEEPSEARCH_COMMIT="49c964ec7e1cd7497346775a186b18051db72ce9"

# 尽力加载鸿蒙公共环境（PATH/git/LD_LIBRARY_PATH 等，失败不影响）
if [ -f "$SCRIPT_DIR/ohos/ohos-env.sh" ]; then
    # shellcheck disable=SC1091
    . "$SCRIPT_DIR/ohos/ohos-env.sh" >/dev/null 2>&1 || true
fi

log()  { printf '[deepsearch-install] %s\n' "$*"; }
warn() { printf '[deepsearch-install][警告] %s\n' "$*"; }
fail() { printf '[deepsearch-install][失败] %s\n' "$*"; exit 1; }
# 探测统一空 LD_LIBRARY_PATH：与 AgentServer sidecar 生产环境一致（wheel 自带
# RPATH 自包含），且免疫用户 shell 里可能污染的库路径
py_ok() { LD_LIBRARY_PATH= "$VENV_PY" -c "$1" >/dev/null 2>&1; }
# pip 刚解包的 .so 在鸿蒙 hmdfs 上存在"沉降窗口"：冷启动/冷缓存时大文件
# （如 numpy 的 4.3MB .so）需数十秒才完全可见，期间 dlopen 报 Permission
# denied（内核读到不完整字节→签名校验失败）。实测 6 秒窗口不够，误判为
# 安装失败（安装其实已成功，稍后 import 即通过）。故用 2/5/10/20 退避共
# ~37 秒；成功路径第一次就返回，零额外开销。
py_ok_retry() {
    _i=0
    while [ "$_i" -lt 5 ]; do
        py_ok "$1" && return 0
        _i=$((_i + 1))
        [ "$_i" -lt 5 ] && sleep "$(echo "2 5 10 20" | cut -d' ' -f$_i)"
    done
    return 1
}
# 真实 wheel 以 zip 魔数 "PK" 开头。仓库里 >10MiB 的 wheel（两个 pandas）走
# Git LFS（gitcode 单文件限 10 MiB）；未装 git-lfs 的机器克隆下来是 ~130 字节的
# 指针文本文件，必须识别并跳过，让后续在线兜底路径接管。
is_real_wheel() { [ -f "$1" ] && [ "$(head -c 2 "$1" 2>/dev/null)" = "PK" ]; }

# hmdfs 沉降治理：pip 刚写入的 .so 存在分钟级"沉降窗口"（冷启动时更久），
# 期间 dlopen 报 Permission denied。全量 cat 强制数据落盘/入缓存可缩短窗口。
# 关键在于入口探测失败时先暖读+重试而不是立即重装 —— force-reinstall 会重写
# .so、把沉降时钟归零，形成"越重跑越坏"的恶性循环（本机实测：连续重跑 2 次
# 均失败，停止重跑等 2 分钟后 import 直接通过）。
SP="$REPO_ROOT/.venv/lib/python3.12/site-packages"
warm_sos() {
    [ -d "$SP" ] || return 0
    find "$SP" \( -name '*.so' -o -name '*.so.*' \) -type f -exec cat {} + \
        >/dev/null 2>&1
    return 0
}

mkdir -p "$TMP"

# ============================================================================
log "步骤 0/6: 环境预检"
# ============================================================================
[ -x "$VENV_PY" ] || fail "未找到 $VENV_PY —— 请先执行 scripts/install-ohos-agentserver.sh 完成基础环境"
py_ok 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)' \
    || fail "需要 Python 3.12（wheel 按 cp312 构建），当前不是 3.12"
py_ok 'import platform, sys; sys.exit(0 if platform.machine() == "aarch64" else 1)' \
    || fail "仅支持 aarch64 架构"
if [ -x "$SIGN_TOOL" ]; then
    log "  签名工具: $SIGN_TOOL"
else
    warn "未找到 $SIGN_TOOL —— 若离线 wheel 需要重签将无法兜底"
fi
[ -f "$CONVERTER" ] || warn "未找到 scripts/ohos/ohos-musl-wheel-convert.py —— wheel 重签/转换兜底不可用"

# ---------- 执行环境探测（hmdfs 任务级限制金丝雀）----------
# 实测：系统终端等上下文无法 mmap-exec hmdfs 上的任何 .so（dlopen 一律
# Permission denied，与签名/权限位/文件内容无关——pydantic_core 同样被拒）；
# 而 App(AgentServer) 环境不受限（DeepResearch 在 App 内实测可用）。
# 受限终端里：安装照常（pip 写文件没问题），import 功能验证必然失败且无意义
# —— 跳过验证、用 pip 元数据判定，装完指引用 App 实测。
CANARY_OK=1
if [ -d "$SP" ]; then
    if ! py_ok "
import ctypes, glob, sys
sos = glob.glob('$SP/**/*.so', recursive=True)[:5]
if sos:
    denied = 0
    for s in sos:
        try:
            ctypes.CDLL(s)
            sys.exit(0)
        except OSError as e:
            denied += ('Permission denied' in str(e))
    sys.exit(1 if denied == len(sos) else 0)
"; then
        CANARY_OK=""
        warn "  !! 当前终端无法加载 hmdfs 上的 .so（任务级执行限制）"
        warn "     这不代表安装失败 —— App(AgentServer) 环境不受此限制"
        warn "     本脚本将继续安装、跳过功能验证，完成后请在 App 内实测"
    fi
fi
log "  预检通过 (repo=$REPO_ROOT)"

# ============================================================================
log "步骤 1/6: 安装纯 Python 依赖"
# ============================================================================
# 版本与本机验证过的组合一致；json-repair==0.58.0 同时满足
# jiuwenclaw(>=0.58.0) 与 deepsearch(==0.58.0)
PKGS="jinja2==3.1.6 json-repair==0.58.0 networkx==3.4.2 pyvis==0.3.2 \
aiolimiter==1.1.0 tldextract==5.3.2 requests-file==3.0.1 \
python-dateutil==2.9.0.post0 pytz==2026.3.post1 openpyxl==3.1.5 \
et-xmlfile==2.0.0 jsonpickle==4.1.2 ipython==9.17.1 traitlets==5.16.1 \
prompt_toolkit==3.0.53 wcwidth==0.8.2"

if ls "$DEPS_DIR"/*.whl >/dev/null 2>&1; then
    if "$VENV_PY" -m pip install --no-index --find-links="$DEPS_DIR" --no-deps \
        $PKGS >/dev/null 2>&1; then
        log "  离线安装完成（wheels/deepsearch-deps）"
    else
        warn "离线安装不完整，改用镜像在线安装"
        "$VENV_PY" -m pip install --no-deps -i "$MIRROR" $PKGS >/dev/null 2>&1 \
            || warn "在线安装有失败项，稍后自动补齐循环会重试"
    fi
else
    log "  未找到离线包目录 wheels/deepsearch-deps，使用镜像在线安装"
    "$VENV_PY" -m pip install --no-deps -i "$MIRROR" $PKGS >/dev/null 2>&1 \
        || warn "在线安装有失败项，稍后自动补齐循环会重试"
fi

# ============================================================================
log "步骤 2/6: 安装 openjiuwen_deepsearch 本体"
# ============================================================================
if py_ok "import openjiuwen_deepsearch"; then
    log "  已安装，跳过"
else
    _done=""
    _w=$(ls "$WHEELS"/openjiuwen_deepsearch-*.whl 2>/dev/null | head -1)
    if [ -n "$_w" ]; then
        if is_real_wheel "$_w"; then
            "$VENV_PY" -m pip install --no-deps "$_w" >/dev/null 2>&1 && _done="wheel"
        else
            warn "  离线 wheel 是 Git LFS 指针（本机未装 git-lfs），跳过: $(basename "$_w")"
        fi
    fi
    if [ -z "$_done" ] && [ -d "$REPO_ROOT/.cache/deepsearch-src/deepsearch" ]; then
        "$VENV_PY" -m pip install --no-deps "$REPO_ROOT/.cache/deepsearch-src/deepsearch" \
            >/dev/null 2>&1 && _done="本地源码"
    fi
    if [ -z "$_done" ]; then
        log "  尝试从 gitcode 克隆源码（enterprise_dev 分支）..."
        if command -v git >/dev/null 2>&1; then
            rm -rf "$REPO_ROOT/.cache/deepsearch-src"
            git clone -b enterprise_dev https://gitcode.com/openJiuwen/deepsearch.git \
                "$REPO_ROOT/.cache/deepsearch-src" >/dev/null 2>&1
            if [ -d "$REPO_ROOT/.cache/deepsearch-src/deepsearch" ]; then
                git -C "$REPO_ROOT/.cache/deepsearch-src" checkout "$DEEPSEARCH_COMMIT" \
                    >/dev/null 2>&1 \
                    || warn "无法锁定 commit $DEEPSEARCH_COMMIT，使用分支最新版"
                "$VENV_PY" -m pip install --no-deps \
                    "$REPO_ROOT/.cache/deepsearch-src/deepsearch" >/dev/null 2>&1 && _done="git 克隆"
            fi
        fi
    fi
    [ -n "$_done" ] || fail "openjiuwen_deepsearch 安装失败（无离线 wheel、无本地源码、git 克隆失败）"
    log "  安装成功（来源: $_done）"
fi

# ============================================================================
log "步骤 3/6: 安装原生依赖（numpy / pypdfium2 / Pillow / pandas）"
# ============================================================================
# install_native <显示名> <功能测试代码> <harmonyos wheel 通配> <musl 兜底: "pkg==ver cp312|py3|none">
install_native() {
    _label=$1 _test=$2 _glob=$3 _musl=$4
    if [ -n "$CANARY_OK" ]; then
        if py_ok "$_test"; then
            log "  $_label 已可用，跳过"
            return 0
        fi
        # 上次运行可能已装好、只是 .so 还在沉降窗口：暖读 + 重试探测，通过则
        # 不重装（重装会重写文件、重置沉降时钟）
        if warm_sos && py_ok_retry "$_test"; then
            log "  $_label 已安装（文件沉降后通过），跳过"
            return 0
        fi
    else
        # 受限终端：import 必然 Permission denied，用 pip 元数据判定是否已装
        if "$VENV_PY" -m pip show "$_label" >/dev/null 2>&1; then
            log "  $_label 已安装（pip 元数据；终端受限跳过功能验证，请在 App 内实测）"
            return 0
        fi
    fi
    # --- 路径 1: 仓库内离线 harmonyos wheel ---
    for _w in "$WHEELS"/$_glob; do
        [ -f "$_w" ] || continue
        if ! is_real_wheel "$_w"; then
            warn "  $_label: $(basename "$_w") 是 Git LFS 指针（本机未装 git-lfs），跳过离线 wheel"
            continue
        fi
        log "  $_label: 安装离线 wheel ($(basename "$_w"))"
        if "$VENV_PY" -m pip install --no-deps --force-reinstall "$_w" >/dev/null 2>&1; then
            if [ -z "$CANARY_OK" ]; then
                log "  $_label 安装完成（离线 wheel；终端受限，功能验证请在 App 内进行）"
                return 0
            fi
            warm_sos
            if py_ok_retry "$_test"; then
                log "  $_label 安装成功（离线 wheel）"
                return 0
            fi
            warn "  $_label 离线 wheel 未通过功能测试（可能是本机代码签名不同）"
        else
            warn "  $_label 离线 wheel pip 安装失败"
        fi
        break
    done
    # --- 路径 2: 用本机签名工具重签同一 wheel ---
    if [ -f "$CONVERTER" ] && [ -x "$SIGN_TOOL" ]; then
        for _w in "$WHEELS"/$_glob; do
            [ -f "$_w" ] || continue
            is_real_wheel "$_w" || continue
            log "  $_label: 尝试用本机签名工具重签..."
            _out="$TMP/$(basename "$_w")"
            if "$VENV_PY" "$CONVERTER" "$_w" -o "$_out" >/dev/null 2>&1 \
               && "$VENV_PY" -m pip install --no-deps --force-reinstall "$_out" >/dev/null 2>&1; then
                if [ -z "$CANARY_OK" ]; then
                    log "  $_label 安装完成（本机重签；终端受限，功能验证请在 App 内进行）"
                    return 0
                fi
                warm_sos
                if py_ok_retry "$_test"; then
                    log "  $_label 安装成功（本机重签）"
                    return 0
                fi
            fi
            warn "  $_label 重签路径未成功"
            break
        done
        # --- 路径 3: 从 musl wheel 现场转换（本机签名） ---
        _mspec=${_musl%% *}            # pkg==ver
        _mkind=${_musl##* }            # cp312 | py3 | none
        if [ "$_mkind" != "none" ] && [ -n "$_mspec" ]; then
            _msrc=""
            _mpkg=${_mspec%%==*}
            for _m in "$MUSL_DIR"/$_mpkg-*.whl; do
                is_real_wheel "$_m" && _msrc=$_m && break
            done
            if [ -z "$_msrc" ]; then
                log "  $_label: musl 源 wheel 不在本地，尝试镜像下载..."
                if [ "$_mkind" = "cp312" ]; then
                    "$VENV_PY" -m pip download --no-deps --only-binary :all: \
                        --platform musllinux_1_2_aarch64 --python-version 3.12 \
                        --implementation cp --abi cp312 -d "$TMP" -i "$MIRROR" \
                        "$_mspec" >/dev/null 2>&1
                else
                    "$VENV_PY" -m pip download --no-deps --only-binary :all: \
                        --platform musllinux_1_1_aarch64 --python-version 3.12 \
                        -d "$TMP" -i "$MIRROR" "$_mspec" >/dev/null 2>&1
                fi
                for _m in "$TMP"/$_mpkg-*.whl; do
                    [ -f "$_m" ] && _msrc=$_m && break
                done
            fi
            if [ -n "$_msrc" ]; then
                log "  $_label: 从 musl wheel 转换（含同余修复 + libpython 依赖 + 本机签名）..."
                _conv_in="$TMP/$(basename "$_msrc")"
                [ "$_conv_in" = "$_msrc" ] || cp "$_msrc" "$_conv_in"
                # 转换器输出落在源文件同目录，名字自动带 harmonyos_aarch64 标签
                if "$VENV_PY" "$CONVERTER" "$_conv_in" >/dev/null 2>&1; then
                    for _o in "$TMP"/$_mpkg-*harmonyos_aarch64.whl; do
                        [ -f "$_o" ] || continue
                        if "$VENV_PY" -m pip install --no-deps --force-reinstall "$_o" >/dev/null 2>&1; then
                            if [ -z "$CANARY_OK" ]; then
                                log "  $_label 安装完成（musl 现场转换；终端受限，功能验证请在 App 内进行）"
                                return 0
                            fi
                            warm_sos
                            if py_ok_retry "$_test"; then
                                log "  $_label 安装成功（musl 现场转换）"
                                return 0
                            fi
                        fi
                    done
                fi
                warn "  $_label musl 转换路径未成功"
            fi
        fi
    fi
    if [ -z "$CANARY_OK" ]; then
        warn "  $_label pip 安装失败（终端受限，无法给出功能层面的错误信息）"
    else
        warn "  $_label 所有安装路径均失败（功能测试已各重试 5 次）；真实错误："
        LD_LIBRARY_PATH= "$VENV_PY" -c "$_test" 2>&1 | tail -5 | sed 's/^/    /'
        warn "  若错误为 Permission denied：安装大概率已成功，只是 hmdfs 沉降窗口未过 —— 等待 2~5 分钟后重跑本脚本即可（入口探测通过即跳过，不会重装重写文件）"
    fi
    return 1
}

DEGRADED=""
install_native "numpy"      "import numpy; numpy.array([1,2]).sum()" \
    "numpy-*harmonyos_aarch64.whl" "none" \
    || DEGRADED="$DEGRADED numpy"

install_native "pypdfium2"  "import pypdfium2 as p; d=p.PdfDocument.new(); d.new_page(10,10)" \
    "pypdfium2-*harmonyos_aarch64.whl" "pypdfium2==4.30.0 py3" \
    || DEGRADED="$DEGRADED pypdfium2"

install_native "Pillow"     "from PIL import Image; Image.new('RGB',(8,8))" \
    "Pillow-*harmonyos_aarch64.whl" "none" \
    || DEGRADED="$DEGRADED Pillow"

install_native "pandas"     "import pandas as pd; assert pd.Series(range(10)).rolling(3).sum().iloc[2]==3.0" \
    "pandas-*harmonyos_aarch64.whl" "pandas==2.3.1 cp312" \
    || DEGRADED="$DEGRADED pandas"

# ============================================================================
log "步骤 4/6: 配置 pymilvus 平台桩"
# ============================================================================
if py_ok "from pymilvus import RRFRanker, MilvusClient, AnnSearchRequest"; then
    log "  pymilvus 已可用，跳过"
else
    "$VENV_PY" - <<'PYSTUB' || fail "pymilvus 桩写入失败"
import sysconfig
from pathlib import Path

sp = Path(sysconfig.get_paths()["purelib"])
base = sp / "pymilvus"
(base / "client").mkdir(parents=True, exist_ok=True)
(base / "milvus_client").mkdir(parents=True, exist_ok=True)

(base / "__init__.py").write_text('''# pymilvus platform stub (HarmonyOS)
# Real pymilvus requires grpcio (native), which cannot run on HarmonyOS.
# This stub satisfies module-level imports used by openjiuwen /
# openjiuwen_deepsearch; Milvus-backed stores raise on actual use.
__version__ = "0.0.0+ohos-stub"


def _unavailable(*args, **kwargs):
    raise NotImplementedError(
        "pymilvus is stubbed on this platform (HarmonyOS): "
        "Milvus-backed vector store / retrieval is unavailable."
    )


class MilvusException(RuntimeError):
    """Stub exception mirroring pymilvus.MilvusException."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args)


class _Unavailable:
    def __init__(self, *args, **kwargs):
        _unavailable()


class MilvusClient(_Unavailable):
    pass


class AsyncMilvusClient(_Unavailable):
    pass


class AnnSearchRequest(_Unavailable):
    pass


class RRFRanker(_Unavailable):
    pass


class WeightedRanker(_Unavailable):
    pass


class Collection(_Unavailable):
    pass


class CollectionSchema(_Unavailable):
    pass


class Function(_Unavailable):
    pass


class FunctionType:  # enum-like placeholder
    UNKNOWN = 0
    BM25 = 1
    TEXT_EMBEDDING = 2


class DataType:  # enum-like placeholder
    NONE = 0
    BOOL = 1
    INT8 = 2
    INT16 = 3
    INT32 = 4
    INT64 = 5
    FLOAT = 10
    DOUBLE = 11
    STRING = 20
    VARCHAR = 21
    ARRAY = 22
    JSON = 23
    FLOAT_VECTOR = 101
    SPARSE_FLOAT_VECTOR = 104


class connections:
    @staticmethod
    def connect(*args, **kwargs):
        _unavailable()


class utility:
    @staticmethod
    def has_collection(*args, **kwargs):
        _unavailable()
''', encoding="utf-8")

(base / "client" / "__init__.py").write_text(
    "from pymilvus import MilvusException  # noqa: F401\n", encoding="utf-8")
(base / "client" / "types.py").write_text('''# pymilvus platform stub (HarmonyOS)
from enum import Enum


class LoadState(str, Enum):
    NotLoad = "NotLoad"
    Loading = "Loading"
    Loaded = "Loaded"
''', encoding="utf-8")
(base / "client" / "search_result.py").write_text('''# pymilvus platform stub (HarmonyOS)
class SearchResult:  # noqa: D101
    def __init__(self, *args, **kwargs):
        raise NotImplementedError("pymilvus is stubbed on this platform (HarmonyOS).")
''', encoding="utf-8")
(base / "milvus_client" / "__init__.py").write_text(
    "from pymilvus.milvus_client.index import IndexParams  # noqa: F401\n", encoding="utf-8")
(base / "milvus_client" / "index.py").write_text('''# pymilvus platform stub (HarmonyOS)
class IndexParams:  # noqa: D101
    def __init__(self, *args, **kwargs):
        raise NotImplementedError("pymilvus is stubbed on this platform (HarmonyOS).")
''', encoding="utf-8")

di = sp / "pymilvus-0.0.0+ohos_stub.dist-info"
di.mkdir(parents=True, exist_ok=True)
(di / "METADATA").write_text(
    "Metadata-Version: 2.1\nName: pymilvus\nVersion: 0.0.0+ohos_stub\n"
    "Summary: HarmonyOS platform stub for pymilvus\n", encoding="utf-8")
(di / "WHEEL").write_text(
    "Wheel-Version: 1.0\nGenerator: install-deepsearch-ohos\n"
    "Root-Is-Purelib: true\nTag: py3-none-any\n", encoding="utf-8")
(di / "top_level.txt").write_text("pymilvus\n", encoding="utf-8")
(di / "RECORD").write_text("pymilvus,,\n", encoding="utf-8")
print("stub written")
PYSTUB
    py_ok "from pymilvus import RRFRanker, MilvusClient, AnnSearchRequest" \
        || fail "pymilvus 桩写入后仍无法导入"
    log "  已写入平台桩（Milvus 向量检索在鸿蒙上不可用，其余功能不受影响）"
fi

# ============================================================================
log "步骤 5/6: 配置 deepresearch skill 目录兜底链接"
# ============================================================================
_skill_probe_ok() {
    [ -n "$CANARY_OK" ] || return 1   # 受限终端 import 必然失败，直接走目录检查
    py_ok "from jiuwenclaw.agentserver.tools.deepresearch.tools import _resolve_skill_root as r; import sys; sys.exit(0 if r() else 1)"
}
if _skill_probe_ok; then
    log "  skill 目录已可解析，跳过"
else
    _skills="$REPO_ROOT/../relay-claw/office-claw-skills"
    if [ -d "$_skills/deepresearch/scripts" ]; then
        ln -sfn "$(CDPATH= cd -- "$_skills" && pwd)" "$REPO_ROOT/office-claw-skills"
        log "  已创建软链 office-claw-skills -> ../relay-claw/office-claw-skills"
    else
        warn "未找到 ../relay-claw/office-claw-skills —— Agent 经 JIUWENCLAW_SHARED_SKILLS_DIRS 注入时不受影响；CLI 直跑时 skill 脚本不可解析"
    fi
fi

# ============================================================================
log "步骤 6/6: 自动补齐缺失模块"
# ============================================================================
if [ -z "$CANARY_OK" ]; then
    warn "  跳过自动补齐（终端受限，import 探测必然失败）—— 缺失模块会在 App 内调用时报明确错误"
else
    cat > "$TMP/probe_mod.py" <<'PYPROBE'
try:
    import openjiuwen_deepsearch.framework.openjiuwen.agent.workflow  # noqa
    print("OK")
except ModuleNotFoundError as e:
    print(e.name)
except Exception:
    print("NONMOD")
PYPROBE

    _i=0
    warm_sos
    while [ "$_i" -lt 12 ]; do
        _missing=$(LD_LIBRARY_PATH= "$VENV_PY" "$TMP/probe_mod.py" 2>/dev/null | tail -1)
        [ "$_missing" = "OK" ] && break
        [ -n "$_missing" ] || break
        [ "$_missing" = "NONMOD" ] && break
        log "  补装缺失模块: $_missing"
        "$VENV_PY" -m pip install --no-deps -i "$MIRROR" "$_missing" >/dev/null 2>&1 \
            || warn "  无法安装 $_missing（无网络或无兼容 wheel）"
        sleep 2   # hmdfs 延迟可见：给刚解包的文件稳定时间，避免复测误判导致重复安装
        _i=$((_i + 1))
    done
    if [ "$_i" -ge 12 ]; then
        warn "自动补齐循环达到上限，可能仍有缺失模块"
    fi
fi

# ============================================================================
log "最终验证（空 LD_LIBRARY_PATH，模拟最严苛的运行环境）"
# ============================================================================
cat > "$TMP/verify_final.py" <<'PYVERIFY'
import importlib
import logging
import sys

logging.disable(logging.INFO)

FAIL = []
mods = [
    "openjiuwen_deepsearch.algorithm.report_style.service",
    "openjiuwen_deepsearch.framework.openjiuwen.agent.agent_factory",
    "openjiuwen_deepsearch.framework.openjiuwen.agent.workflow",
    "jiuwenclaw.agentserver.tools.deepresearch_task_manager",
    "jiuwenclaw.agentserver.tools.deepresearch.tools",
    "jiuwenclaw.agentserver.tools.deepresearch_tools",
    "jiuwenclaw.agentserver.tools.deepresearch.rewrite_tools",
    "jiuwenclaw.agentserver.tools.deepresearch_plugin.styled_html_export",
]
for m in mods:
    try:
        importlib.import_module(m)
        print(f"  OK   {m}")
    except Exception as e:
        FAIL.append(m)
        print(f"  FAIL {m}: {type(e).__name__}: {e}")

try:
    import pandas as pd
    assert pd.Series(range(10)).rolling(3).sum().iloc[2] == 3.0
    print("  OK   pandas 功能（rolling C++ 路径）")
except Exception as e:
    FAIL.append("pandas")
    print(f"  FAIL pandas 功能: {e}")

try:
    import pypdfium2 as pdfium
    d = pdfium.PdfDocument.new()
    d.new_page(100, 100)
    print("  OK   pypdfium2 功能（新建 PDF）")
except Exception as e:
    FAIL.append("pypdfium2")
    print(f"  FAIL pypdfium2 功能: {e}")

try:
    from jiuwenclaw.agentserver.tools.deepresearch_tools import get_deepresearch_tools
    tools = get_deepresearch_tools()
    names = []
    for t in tools:
        card = getattr(t, "_card", None)
        names.append(getattr(card, "name", "") or type(t).__name__)
    print(f"  DeepResearch 工具注册: {len(tools)} 个 -> {', '.join(names)}")
    if not tools:
        FAIL.append("deepresearch-tools")
except Exception as e:
    FAIL.append("deepresearch-tools")
    print(f"  FAIL 工具注册: {e}")

try:
    from jiuwenclaw.agentserver.tools.deepresearch.tools import _resolve_skill_root
    root = _resolve_skill_root()
    if root:
        print(f"  OK   skill 目录: {root}")
    else:
        print("  警告 skill 目录未解析（Agent 走 JIUWENCLAW_SHARED_SKILLS_DIRS 时不受影响）")
except Exception as e:
    print(f"  警告 skill 目录检查失败: {e}")

if FAIL:
    print(f"验证失败: {len(FAIL)} 项 -> {', '.join(FAIL)}")
    sys.exit(1)
print("全部验证通过")
PYVERIFY

if [ -z "$CANARY_OK" ]; then
    # 受限终端：import 验证必然 Permission denied，无意义 —— 以 pip 完成情况定论
    if [ -n "$DEGRADED" ]; then
        fail "以下包 pip 安装失败:$DEGRADED —— 请检查离线 wheel 完整性与网络"
    fi
    log "最终验证跳过：当前终端无法加载 hmdfs .so（任务级限制，非安装问题）"
    log "安装内容已全部就位 —— 请完全退出并重开 OfficeClaw App，在 App 内调用一次"
    log "DeepResearch 工具完成验证（App 环境不受此终端限制，已实测可用）"
else
    _vc=0
    while :; do
        warm_sos
        if LD_LIBRARY_PATH= "$VENV_PY" "$TMP/verify_final.py" > "$TMP/verify.log" 2>&1; then
            grep -v ohos_build_env "$TMP/verify.log"
            [ -n "$DEGRADED" ] && warn "以下包安装时未通过即时验证，但最终验证已通过（hmdfs 沉降误报）:$DEGRADED"
            break
        fi
        _vc=$((_vc + 1))
        case "$_vc" in
            1) warn "最终验证未通过，5 秒后重试（等待文件沉降）..." ; sleep 5 ;;
            2) warn "仍未通过，20 秒后重试..." ; sleep 20 ;;
            3) warn "仍未通过，60 秒后最后重试..." ; sleep 60 ;;
                *)
                grep -v ohos_build_env "$TMP/verify.log"
                fail "验证未通过 —— 若错误为 Permission denied：等 5 分钟后重跑本脚本（已装内容会跳过重装）；若仍失败请在你的终端执行下面两条命令并把输出发我：
    .venv/bin/python -c 'import numpy'
    grep -m1 ' /storage' /proc/self/mountinfo"
                ;;
        esac
    done
fi

# ============================================================================
log "==================================================================="
log "DeepResearch 安装完成！"
log "==================================================================="
if [ -n "$CANARY_OK" ]; then
    log "产出: 4 个 DeepResearch 工具已注册（deepresearch_stream /"
    log "      deepresearch_prepare_rewrite / deepresearch_commit_rewrite /"
    log "      deepresearch_generate_rewrite_html）"
else
    log "产出: DeepResearch 依赖已安装（本终端无法执行 .so，未做功能验证）"
    log "验证: 在 App 内调用一次 DeepResearch 工具即完成验证"
fi
log ""
log "最后一步: 重启 AgentServer 使其加载新依赖 ——"
log "  · 命令行启动的: 先结束进程再执行  sh scripts/start-ohos-agentserver.sh 18092"
log "  · App(OfficeClaw) 启动的: 完全退出 App 后重新打开"
log "已知降级: matplotlib/seaborn 未装（仅图表沙箱受影响）; Milvus 向量检索不可用（平台桩）"
