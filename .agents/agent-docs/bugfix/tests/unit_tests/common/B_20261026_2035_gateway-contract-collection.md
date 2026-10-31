---
title: Gateway 拆包后协议契约测试无法收集
author: null
author_note: 历史作者待确认；缺少原 owner 和可靠创建署名
id: B_20261026_2035_gateway-contract-collection
type: B
modules: []
files: []
designs: null
design_note: 历史设计关联待核实，不等同于没有关联设计
---

# Gateway 拆包后协议契约测试无法收集

日期：2026-10-26。范围：`tests/unit_tests/common/test_req_method_contract.py`
及当前修复分支与 `develop` 的集成。

## 现象与根因

用户提供的 UT 报告包含 18129 项通过、100 项跳过及 1 项收集错误。
`test_req_method_contract.py` 在模块导入阶段引用
`jiuwenswarm.gateway.channel_manager.tui.tui_connect`，抛出
`ModuleNotFoundError: No module named 'jiuwenswarm.gateway'`。

`215779909` 引入协议契约测试；`ea68d6ed7` 将 Gateway 移到顶层 `gateway/`，
该测试的导入及源码扫描目录仍使用旧路径。测试收集因导入失败而中断；
即使只修导入，旧扫描目录也可能让字面量检查空跑。
上游 `0a34c3599` 已同时修正两个导入、扫描目录，并加入目录存在和扫描非空断言。

## 决策

将已含上述修复的 `develop` 同步到当前人工干预历史记录修复分支，沿用上游修正，
保留既有暂停恢复、去重和资源清理改动。同步的上游版本为 `67b8c033d`，自动合并无冲突。
协议枚举来自项目锁定的 `openjiuwen-protocol`，测试复用该来源，不增加第二套枚举。
本次没有新增协议语义，集中 spec 无需新增契约。

## 未采用的方案

- 不补造旧 `jiuwenswarm.gateway` 兼容包，以免将已经迁出的导入路径继续固化。
- 不跳过测试或吞掉导入错误，以免隐藏转发方法与协议枚举不一致的问题。
- 不重复实现上游已有修复，避免分支集成时出现两套路径选择逻辑。

## 验证

在当前顶层 Gateway 布局下执行原始测试模块源码，复现相同的缺包错误。
使用 worktree 独立 `.venv` 和冻结依赖：协议契约测试 3 项通过，
人工干预历史、暂停恢复、取消收集、权限和会话清理相关回归 390 项通过。
整个 `tests/unit_tests/` 成功收集 18360 项测试，没有收集错误。

未执行完整 UT 用例运行、格式化或静态检查；本地结果不代表远端 CI 已完成复跑。
