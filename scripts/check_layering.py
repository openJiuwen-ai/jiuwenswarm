#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""分层检查：gateway 解耦燃尽度量与违规检测（手动工具）。

规则（白名单内为已知存量，即燃尽清单；白名单外为新增违规）：

  R1  common  禁止 import server / agents / channels / gateway / extensions / symphony
  R2  server  禁止 import gateway
  R3  gateway 禁止 import server / agents / channels

例外：``gateway.common`` 是无状态纯函数/常量的唯一真源（M1 策略调整），
common / server / runtime 允许反向引用；扩展治理层
``gateway.extensions.{registry,manager,loader}`` 同理——扩展发现、注册表与
加载器收敛为单份实现，避免两侧各持一份进程内单例，其在 AgentServer 侧的
引用不视为违规。其余 ``gateway.*`` 仍属传输层，继续禁止。

使用时机（《Gateway独立成仓-解耦设计》PR-2，单人解耦精简版）：

  1. 每次同步 upstream 后——母仓是共享仓库，上游提交可能引入新耦合；
  2. 每个 A/B 系列 PR 自我验收——对照燃尽基线（--baseline）；
  3. B3 拆仓前终检——gateway 闭包应只剩 gateway/ + common/。

用法：

  python scripts/check_layering.py             # 规则检查；发现新增违规退出码 1
  python scripts/check_layering.py --baseline  # 耦合基线全景，对照内置基线显示增减
  python scripts/check_layering.py --list-deps # gateway 外部直接依赖清单

CI 接入：tests/unit_tests/test_layering.py 以 UT 形式调用本脚本并断言
退出码 0——上游同步或本仓提交引入新的跨层依赖时，UT 直接失败。

统计口径：解析 git 跟踪文件与未忽略的未跟踪 .py（含开发中的新文件），
区分模块级 import 与函数内延迟 import（延迟同样计入违规——拆仓时两种
都会断）；TYPE_CHECKING 块按模块级计（静态引用对拆仓同样致命）。
"""

from __future__ import annotations

import argparse
import ast
import logging
import shutil
import subprocess
import sys
from collections import defaultdict, deque
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "jiuwenswarm"

logger = logging.getLogger(__name__)

_ALL_LOCAL_PKGS = None  # 延迟初始化（collect_py_files 依赖 git）

# ---------------------------------------------------------------------------
# 已知存量（燃尽清单）——随 B1'/A4 消化，消除后此表同步缩减
# 基线：upstream/develop fbd8d6f41 + browser_config 下沉（2026-10-13 实测）
# ---------------------------------------------------------------------------

# 键为模块名（点号分隔）。R2 原存量 server/ipc/protocol.py -> gateway.routing.keys
# 已被上游提交消除（2026-10-13 同步验证），不再列入。
# R1 存量（含 a6b9ea2e0 于 2026-10-13 引入的 3 对）已由 B1'
# （common 注入化 + 纯模块下沉）全部消除；!8016 合入的临时白名单
# （14 对）随本 MR 缩减为空。
KNOWN_R1_COMMON_BACKREFS: dict[str, set[str]] = {}

# 允许的跨层目标：无状态纯组件以 gateway 为唯一真源，母仓反向引用不视为违规。
# extensions 治理层（registry/manager/loader）同样以 gateway 为唯一实现：
# 三者持有进程内单例与扩展注册表，双份副本会导致状态分裂。
# gateway.auth 为登录栈领域共享（sessions.json 等有状态双侧契约需真源单点，
# 母仓经 common/auth shim 受控消费）。base_crypto 为 CryptoProvider 注入点
# 接口件，实现与装配（_RegistryCryptoBridge）在 extensions 治理层，
# 母仓 config/config_panel 经 common/security shim 受控消费。
ALLOWED_CROSS_LAYER_PREFIXES: tuple[str, ...] = (
    "gateway.common",
    "gateway.extensions.registry",
    "gateway.extensions.manager",
    "gateway.extensions.loader",
    "gateway.extensions.base_crypto",
    "gateway.auth",
)

KNOWN_R2_SERVER_TO_GATEWAY: dict[str, set[str]] = {}

# gateway 对 channels 的存量（A5 散件吸收对象：web 面板的文件选择器）已消除。
# browser_config 已下沉 jiuwenswarm.common.browser_config，不再占用本白名单。
KNOWN_R3_GATEWAY_FOREIGN: dict[str, set[str]] = {
}
# 燃尽基线（--baseline 对照用；数字口径见各字段注释）
BASELINE = {
    # hard 闭包 = 从 gateway 出发仅沿模块级 import 可达的外部（非 gateway/common）模块数。
    # 根包单文件（dotenv_early、llm_provider_compat_patch）计入其所属顶级名。
    # B2.5 后（rebase 1372eda94 实测，含 B2 合入后的 extensions 状态）：
    # 四大包（agents/server/runtime/symphony）hard 闭包全部归零。
    # extensions 30 = B2 吸收后的框架件 + AgentOS 客户端 + 上游回流。
    # gateway 自有只读 dotenv 副本后，根包 dotenv_early 不再从 gateway 可达；
    # instance_manager 延迟闭包只经母仓 dotenv_early 的 bootstrap 写入到达，一并归零。
    "closure_hard": {
        "extensions": 30, "observability": 10,
        "dotenv_early": 0, "llm_provider_compat_patch": 0,
    },
    # lazy 闭包 = 含函数内延迟 import 的可达模块数
    "closure_lazy": {
        # 2026-10-19 rebase 8e0f173b4：上游新功能 lazy 扩散 +104（非解耦退化）
        "agents": 344, "server": 240, "extensions": 54, "runtime": 43,
        "symphony": 11, "observability": 11, "instance_manager": 0, "channels": 6,
        "dotenv_early": 0, "llm_provider_compat_patch": 0,
    },
    # common 反向引用（R1 存量）：B1' 已全部消除（注入化 + 纯模块下沉）
    "common_backref_modules": 0,
    "common_backref_imports": 0,
    # gateway 直接 import 的外部（非 gateway/common）模块数。
    # 本次去掉 dotenv_early，23 → 22。与实测的其余差额是既有燃尽，本次不重刷。
    "gateway_direct_foreign": 22,
}


# ---------------------------------------------------------------------------
# import 图构建
# ---------------------------------------------------------------------------

def _all_local_top_modules() -> tuple[str, ...]:
    """本仓全部顶级模块名（含根包单文件），供 R1 白名单式检查。"""
    tops: set[str] = set()
    for rel in collect_py_files():
        parts = rel.split("/")
        if parts[0] == "gateway":
            tops.add("gateway")  # M0-c 顶层包：R1 语义下 common 不得 import gateway
        elif len(parts) >= 2 and parts[0] == PKG:
            tops.add(parts[1][:-3] if parts[1].endswith(".py") else parts[1])
    tops.discard("")
    tops.discard("common")  # R1 语义：common 只能 import common
    return tuple(sorted(tops))


def _git(*args: str) -> str:
    git_path = shutil.which("git") or "git"
    return subprocess.run(
        [git_path, "-C", str(REPO_ROOT), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def collect_py_files() -> list[str]:
    # M0-c 后 gateway 为顶层包（与 jiuwenswarm 并列）；两者都纳入依赖图
    tracked = _git("ls-files", f"{PKG}/", "gateway/").splitlines()
    untracked = _git("ls-files", "--others", "--exclude-standard", f"{PKG}/", "gateway/").splitlines()
    return sorted(f for f in set(tracked) | set(untracked) if f.endswith(".py"))


def path_to_module(rel: str) -> str:
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


class ImportGraph:
    def __init__(self) -> None:
        self.files = collect_py_files()
        self.fileset = set(self.files)
        self.modules = {path_to_module(f): f for f in self.files}
        # hard = 模块级 import；lazy = 函数内延迟 import
        self.hard: dict[str, set[str]] = defaultdict(set)
        self.lazy: dict[str, set[str]] = defaultdict(set)
        self._build()

    def _resolve(self, mod: str, name: str) -> str | None:
        candidates = [name]
        parts = mod.split(".")
        for i in range(len(parts), 0, -1):
            candidates.append(".".join(parts[:i] + [name]))
        for cand in candidates:
            if cand in self.modules:
                return cand
            p = cand.replace(".", "/")
            for f in (f"{p}.py", f"{p}/__init__.py"):
                if f in self.fileset:
                    return path_to_module(f)
        return None

    def _parent_chain(self, mod: str) -> set[str]:
        parts = mod.split(".")
        return {".".join(parts[:i]) for i in range(1, len(parts))} & set(self.modules)

    def _build(self) -> None:
        for mod, rel in self.modules.items():
            self._parse_module(mod, rel)

        full: dict[str, set[str]] = defaultdict(set)
        for m, s in self.hard.items():
            full[m] |= s
        for m, s in self.lazy.items():
            full[m] |= s
        for m in self.modules:
            full[m] |= self._parent_chain(m)
            self.hard[m] |= self._parent_chain(m)
        self.full = full

    def _parse_module(self, mod: str, rel: str) -> None:
        src = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            logger.warning("[warn] 语法错误，跳过 %s: %s", rel, exc)
            return

        def walk(node: ast.AST, in_func: bool) -> None:
            for ch in ast.iter_child_nodes(node):
                targets: list[str | None] = []
                if isinstance(ch, ast.Import):
                    targets = [self._resolve(mod, a.name) for a in ch.names]
                elif isinstance(ch, ast.ImportFrom):
                    if ch.level == 0:
                        base = ch.module or ""
                        if base.startswith(PKG):
                            for a in ch.names:
                                targets.append(
                                    self._resolve(mod, f"{base}.{a.name}" if a.name != "*" else base)
                                    or self._resolve(mod, base)
                                )
                        else:
                            targets = [self._resolve(mod, base)]
                    else:
                        pp = mod.split(".")
                        if not self.modules[mod].endswith("__init__.py"):
                            pp = pp[:-1]
                        for _ in range(ch.level - 1):
                            pp = pp[:-1]
                        base = (".".join(pp) + "." + ch.module) if ch.module else ".".join(pp)
                        for a in ch.names:
                            targets.append(
                                self._resolve(mod, f"{base}.{a.name}" if a.name != "*" else base)
                                or self._resolve(mod, base)
                            )
                else:
                    walk(
                        ch,
                        in_func or isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)),
                    )
                    continue
                bucket = self.lazy if in_func else self.hard
                for t in targets:
                    if t and t != mod:
                        bucket[mod].add(t)

        walk(tree, False)

    def closure(self, roots: set[str], edges: dict[str, set[str]]) -> set[str]:
        seen: set[str] = set()
        dq = deque(roots)
        while dq:
            m = dq.popleft()
            if m in seen:
                continue
            seen.add(m)
            dq.extend(e for e in edges.get(m, ()) if e not in seen and e in self.modules)
        return seen

    def imports_with_kind(self, mod: str, target: str) -> str:
        kinds = []
        if target in self.hard.get(mod, set()):
            kinds.append("模块级")
        if target in self.lazy.get(mod, set()):
            kinds.append("延迟")
        return "+".join(kinds)


# ---------------------------------------------------------------------------
# 规则检查
# ---------------------------------------------------------------------------

def _check_rule(
    graph: ImportGraph,
    title: str,
    source_prefixes: tuple[str, ...],
    forbidden_pkgs: tuple[str, ...],
    known: dict[str, set[str]],
) -> int:
    logger.info("\n[R] %s", title)
    violations: dict[str, set[str]] = defaultdict(set)
    for mod in graph.modules:
        if not mod.startswith(source_prefixes):
            continue
        for tgt in graph.hard.get(mod, set()) | graph.lazy.get(mod, set()):
            # 无状态纯组件（gateway.common）允许被母仓反向引用。
            if any(
                tgt == allowed or tgt.startswith(allowed + ".")
                for allowed in ALLOWED_CROSS_LAYER_PREFIXES
            ):
                continue
            # M0-c 后 gateway 为顶层包：forbidden 匹配需同时覆盖
            # ``jiuwenswarm.<pkg>`` 与裸顶层 ``<pkg>`` 两种模块名形态。
            if any(
                tgt.startswith(f"{PKG}.{p}.") or tgt == f"{PKG}.{p}"
                or tgt.startswith(f"{p}.") or tgt == p
                for p in forbidden_pkgs
            ):
                violations[mod].add(tgt)

    known_flat = {(s, t) for s, ts in known.items() for t in ts}
    current_flat = {(s, t) for s, ts in violations.items() for t in ts}
    new_pairs = current_flat - known_flat
    gone_pairs = known_flat - current_flat

    if not current_flat:
        logger.info("    无违规")
        return 0
    for src in sorted(violations):
        rel = graph.modules[src]
        for tgt in sorted(violations[src]):
            tag = "存量" if (src, tgt) in known_flat else "新增!"
            logger.info(
                "    [%s] %s -> %s (%s)",
                tag, rel, tgt.replace(PKG + ".", ""), graph.imports_with_kind(src, tgt),
            )
    if gone_pairs:
        for src, tgt in sorted(gone_pairs):
            logger.info(
                "    [已消除] %s -> %s  ← 燃尽清单可缩减",
                src, tgt.replace(PKG + ".", ""),
            )
    return 1 if new_pairs else 0


def run_rules(graph: ImportGraph) -> int:
    global _ALL_LOCAL_PKGS
    if _ALL_LOCAL_PKGS is None:
        _ALL_LOCAL_PKGS = _all_local_top_modules()
    exit_code = 0
    exit_code |= _check_rule(
        graph, "R1 common 禁止 import 任何非 common 的本仓模块（零本仓引用不变量）",
        (f"{PKG}.common.",), _ALL_LOCAL_PKGS,
        KNOWN_R1_COMMON_BACKREFS,
    )
    exit_code |= _check_rule(
        graph, "R2 server 禁止 import gateway",
        (f"{PKG}.server.",), ("gateway",),
        KNOWN_R2_SERVER_TO_GATEWAY,
    )
    exit_code |= _check_rule(
        graph, "R3 gateway 禁止 import server/agents/channels/runtime/symphony",
        ("gateway.",), ("server", "agents", "channels", "runtime", "symphony"),
        KNOWN_R3_GATEWAY_FOREIGN,
    )
    return exit_code


# ---------------------------------------------------------------------------
# 基线与依赖清单
# ---------------------------------------------------------------------------

def _fmt_delta(cur: int, base: int) -> str:
    diff = cur - base
    if diff == 0:
        return f"{cur} (=基线)"
    sign = "+" if diff > 0 else ""
    return f"{cur} ({sign}{diff} vs 基线 {base})"


def run_baseline(graph: ImportGraph) -> None:
    gw_roots = {m for m in graph.modules if m.startswith("gateway.")}

    logger.info("== 闭包（从 gateway 出发可达的外部模块，非 gateway/common）==")
    for label, edges, base_key in (
        ("模块级", graph.hard, "closure_hard"),
        ("含延迟", graph.full, "closure_lazy"),
    ):
        reach = graph.closure(gw_roots, edges)
        by_pkg: dict[str, int] = defaultdict(int)
        for m in reach:
            parts = m.split(".")
            if len(parts) < 2 or m.startswith(("gateway", f"{PKG}.common")):
                continue
            by_pkg[parts[1]] += 1
        total = sum(by_pkg.values())
        base_total = sum(BASELINE[base_key].values())
        parts = ", ".join(
            f"{p}:{by_pkg.get(p, 0)}({'+' if by_pkg.get(p,0)-b>0 else ''}{by_pkg.get(p, 0)-b})"
            for p, b in sorted(BASELINE[base_key].items(), key=lambda x: -x[1])
        )
        extra = {p: n for p, n in by_pkg.items() if p not in BASELINE[base_key]}
        if extra:
            parts += " | 新增包: " + ", ".join(f"{p}:{n}" for p, n in sorted(extra.items()))
        logger.info("  %s: %s  [%s]", label, _fmt_delta(total, base_total), parts)

    backref_modules, backref_imports = 0, 0
    foreign_prefixes = tuple(
        f"{PKG}.{p}."
        for p in ("server", "agents", "channels", "gateway", "extensions", "symphony")
    )
    for mod, known in KNOWN_R1_COMMON_BACKREFS.items():
        targets: set[str] = set()
        for t in graph.hard.get(mod, set()) | graph.lazy.get(mod, set()):
            if t in known or t.startswith(foreign_prefixes):
                targets.add(t)
        if targets:
            backref_modules += 1
            backref_imports += len(targets)
    logger.info("\n== common 反向引用（R1 存量）==")
    logger.info(
        "  模块数: %s，import 目标对: %s",
        _fmt_delta(backref_modules, BASELINE["common_backref_modules"]),
        _fmt_delta(backref_imports, BASELINE["common_backref_imports"]),
    )

    direct = set()
    for mod in gw_roots:
        for tgt in graph.hard.get(mod, set()) | graph.lazy.get(mod, set()):
            if len(tgt.split(".")) < 2 or tgt.startswith(("gateway", f"{PKG}.common")):
                continue
            direct.add(tgt)
    logger.info("\n== gateway 直接 import 的外部模块（非 gateway/common）==")
    logger.info(
        "  共 %s 个（--list-deps 看明细）",
        _fmt_delta(len(direct), BASELINE["gateway_direct_foreign"]),
    )


def run_list_deps(graph: ImportGraph) -> None:
    deps: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for mod in graph.modules:
        if not mod.startswith("gateway."):
            continue
        for tgt in graph.hard.get(mod, set()) | graph.lazy.get(mod, set()):
            if len(tgt.split(".")) < 2 or tgt.startswith(("gateway", f"{PKG}.common")):
                continue
            deps[tgt][graph.modules[mod]].add("hard" if tgt in graph.hard.get(mod, set()) else "lazy")
    logger.info("gateway 直接依赖的外部模块（非 gateway/common）：共 %s 个\n", len(deps))
    by_pkg: dict[str, list[str]] = defaultdict(list)
    for t in deps:
        by_pkg[t.split(".")[1]].append(t)
    for pkg in sorted(by_pkg, key=lambda p: -len(by_pkg[p])):
        logger.info("### %s（%s 个）", pkg, len(by_pkg[pkg]))
        for t in sorted(by_pkg[pkg]):
            users = deps[t]
            kinds: set[str] = set()
            for v in users.values():
                kinds |= v
            kind = "/".join(sorted(kinds))
            logger.info(
                "  [%-9s] %-52s %s 个文件引用",
                kind, t.replace(PKG + ".", ""), len(users),
            )
        logger.info("")


def main() -> int:
    parser = argparse.ArgumentParser(description="gateway 解耦分层检查（手动工具）")
    parser.add_argument("--baseline", action="store_true", help="输出耦合基线全景（对照内置基线）")
    parser.add_argument("--list-deps", action="store_true", help="列出 gateway 外部直接依赖明细")
    args = parser.parse_args()

    # 结果走 stdout、无日志前缀：保持 CLI 输出与 UT 断言（读 stdout）不变
    logging.basicConfig(stream=sys.stdout, format="%(message)s", level=logging.INFO)

    global _ALL_LOCAL_PKGS
    _ALL_LOCAL_PKGS = _all_local_top_modules()
    graph = ImportGraph()
    if args.baseline:
        run_baseline(graph)
        return 0
    if args.list_deps:
        run_list_deps(graph)
        return 0
    return run_rules(graph)


if __name__ == "__main__":
    sys.exit(main())
