---
id: B_20261128_0953_symphony-init-logs
title: 全新初始化在 wheel 所在目录生成 logs
type: B
author: lin-xiaoyu
author_email: xiaoyu.lin.work@outlook.com
modules:
  - jiuwenswarm/common
files:
  - jiuwenswarm/common/config.py
  - jiuwenswarm/common/symphony_config.py
  - tests/unit_tests/test_config.py
designs: []
design_note: 修复配置迁移的导入时机，沿用现有的迁移和校验契约。
---

# 全新初始化在 wheel 所在目录生成 logs

## 现象与根因

Issue #7049 中，从 wheel 所在目录运行 `jiuwenswarm-init`，初始化返回成功并在当前目录生成 `logs/`。
2026 年 11 月 20 日合入的 `5600f8673`（!10649）把旧配置转换交给 `symphony_config`。
默认配置自带 `symphony` 段，启用模板迁移时也会先导入该模块。
本地审计钩子捕获到首次建目录的调用链。迁移入口导入 Core Symphony，Core 在注册
连接池时调用日志记录器，`LogManager.initialize()` 随即使用默认路径 `./logs/`。

## 修复决策

模板迁移先检查 YAML 中的旧版 Symphony 顶层键与 `orchestration` 下的旧规划键。
判定与转换共用同一组键。发现旧键时加载 Core，转换后按现有规则校验并备份原始 YAML。
全新配置和已经迁移的配置可以跳过这次运行时导入。

## 验证与适用范围

- 在 macOS 和 Python 3.12 上，使用 `db3827080` 与其修复版本分别构建 wheel。
  离线安装两个 wheel 后，当前目录均无 `logs/`。运行各自已安装的
  `jiuwenswarm-init` 后，修复前生成了 `logs/`，修复后的全新和重复初始化均未生成。
- 使用修复版升级仅含旧规划键的配置，确认 `mode`、`top_k`、`max_depth` 迁入
  `orchestration.plan`，备份与原始 YAML 一致。实际迁移调用 Core 校验时仍会生成
  当前目录的 `logs/`。迁移完成后从干净目录再次初始化，未生成该目录或额外备份。
- 新回归用例在修复前因存在 `logs/` 失败。配置与 Symphony 配置相关的 228 项单测在修复后通过。
  验证使用当前工作区的本地构建，发布分支和其他平台的结果需以对应构建为准。
