---
id: B_20261116_2059_hub-package-root
title: Hub 安装把包内子资产 manifest 误判为多个包根
type: B
author: openjiuwen_ai
author_email: openjiuwen_ai@163.com
modules:
  - jiuwenswarm/server/runtime/marketplace
files:
  - jiuwenswarm/server/runtime/marketplace/hub_asset_installer.py
  - tests/unit_tests/server/marketplace/test_hub_asset_installer.py
designs: []
design_note: 仅修正包根识别的深度判定，不改变 Hub 包格式、安装状态机与 marketplace 契约；无独立设计。
---

# Hub 安装把包内子资产 manifest 误判为多个包根

## 现象与根因

#6602：插件广场安装一个元数据缺失的「未知插件」时弹错
`Hub plugin ZIP must contain exactly one manifest.json`，但该插件实际可安装/已存在。

`hub_asset_installer._find_package_root` 原先用 `extracted.rglob("manifest.json")`
收集**所有**清单并要求恰好一个。Hub 包允许携带子资产清单（插件内的
`skills/<name>/manifest.json`、专家团内的 `agents/<name>/manifest.json`），
一个合法包因此被算成多个包根而直接拒绝。同仓导入侧 `_zip_package_root`
（`extension_package_manager.py`）只认根目录或唯一一级子目录，两处判定不一致。

该异常发生在下载解压后的临时目录校验阶段，早于暂存与提交，因此不会留下半成品包；
「已安装」来自既有安装或其他安装入口，本次报错只是把本可安装的包拒掉。

## 修复决策

- 取**最浅层**的 manifest 所在目录作为包根：根清单优先，其次唯一一级子目录；
  更深的清单属于包内子资产，不参与根判定。
- 同级出现多个候选仍视为歧义并报错，避免把多包 ZIP 误当成单包。
- `agent_group` 保留 `package_type=agent_group` 过滤后再做深度判定，嵌套的
  专家团清单不会再让外层组包判定失败。
- 不改变解压、校验、暂存与提交顺序，也不引入幂等/覆盖语义。

## 验证

先红后绿（未修复代码上 3 例失败，均复现 `exactly one manifest.json`）：

- `test_plugin_archive_root_allows_nested_skill_manifests`
- `test_plugin_archive_single_child_allows_nested_manifests`
- `test_group_archive_root_wins_over_nested_group_manifests`
- `test_archive_rejects_sibling_package_roots`（边界：同级多根仍报错）

`tests/unit_tests/server/marketplace/test_hub_asset_installer.py` 12 passed。
同目录全量 381 passed / 9 failed，失败均为 Windows 环境基线（符号链接特权
`WinError 1314`、临时目录 `WinError 5`、`//?/` 扩展路径未归一化），与本次改动无关。

## 遗留

- 若 Hub 资产本身归类错误（例如把专家团包登记为 plugin），包根识别通过后仍会在
  `package_type` 校验处失败；这属于资产元数据问题，需在 Hub 侧修正。
