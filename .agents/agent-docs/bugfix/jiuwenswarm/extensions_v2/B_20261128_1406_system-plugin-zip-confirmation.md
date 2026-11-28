---
id: B_20261128_1406_system-plugin-zip-confirmation
title: 上传系统插件 ZIP 时缺少多插件开启提醒
type: B
author: lizhiyuan
author_email: lizhiyuan65@huawei.com
modules:
  - jiuwenswarm/extensions_v2
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/extensions_v2/management/adapter.py
  - jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/UploadFileCreateModal.tsx
  - jiuwenswarm/channels/web/frontend/src/services/extensionsV2Api.ts
  - jiuwenswarm/channels/web/frontend/tests/pluginMineActions.test.mjs
  - tests/unit_tests/extensions_v2/management/test_management_adapter.py
designs: []
design_note: 沿用插件广场已有的启用状态判断与确认弹窗，在上传入口补齐同一交互。
---

# 上传系统插件 ZIP 时缺少多插件开启提醒

关联 Issue #7311。

## 现象与根因

已有系统插件启用时，在「扩展 → 我的扩展 → 插件」中通过「创建 → 上传文件创建」选择另一个系统插件 ZIP，点击确定会直接导入，缺少插件广场安装时的「开启多个系统插件」提醒。上传入口只接收所选 ZIP 的本地绝对路径，原流程直接调用 `extensions.v2.import`，将包类型识别留给导入阶段，因此在导入前没有接入现有冲突确认。

## 修复决策

- 上传入口先以 `inspect: true` 调用 `extensions.v2.import`。后端用 Core 的 `PackageManager.open_source` 校验 ZIP 和系统插件 manifest，从 `OpenedSource.manifest.package_id` 读取标识，并在退出上下文时清理临时解包。预检只读取包信息，安装、激活、安装记录写入和界面通知都在实际导入阶段执行。复用 Core 避免维护第二套 ZIP 根目录和 manifest 识别规则。
- 识别到系统插件后，复用插件广场的 `hasOtherEnabledSystemPlugin`，检查本地是否有已安装、已启用且标识不同的系统插件。发现冲突时复用同一 `ConfirmDialog` 和文案。确认后按原路径导入，取消会保留所选 ZIP，避免先安装再询问用户。
- 普通插件 ZIP 直接进入原导入流程。预检或插件目录查询失败时，沿用广场的提示策略继续原导入，由现有导入流程报告校验或安装错误。这样临时查询故障也允许用户正常导入。

相关入口见[后端包管理协议](../../../../../jiuwenswarm/extensions_v2/docs/specs/S_02_backend-package-management.md)。

## 回归证据

| 分支基线 | 前端 `test:plugin-mine-actions` | 后端 `test_management_adapter.py` |
| --- | --- | --- |
| `origin/releases/0.2.10` `c02a8b31b` | 35 项通过 | 全部 68 项通过 |
| `origin/develop` `720475742` | 35 项通过 | 4 项导入定向测试通过 |

前端覆盖系统插件 ZIP 的提醒文案、取消不导入、确认后继续，以及普通插件 ZIP 直接导入。后端使用真实 ZIP 验证预检返回包标识，安装目录、安装记录、激活状态和通知均无变化，再用同一个 ZIP 验证原导入成功。后端验证分别使用分支固定的 Core `76b5a0a61` 和 `614219844`，两版 `OpenedSource` 均提供上下文管理器和 `manifest` 属性。桌面端手工复测尚未执行。
