---
id: B_20261128_0955_local-app-delete-safeguards
title: 本地应用删除确认、目录保护与清理反馈
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/extensions/app_market
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/extensions/app_market/hub_install.py
  - jiuwenswarm/extensions/app_market/manager.py
  - jiuwenswarm/extensions/app_market/extension.py
  - jiuwenswarm/extensions/app_market/app_market/models.py
  - jiuwenswarm/extensions/app_market/README.md
  - jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json
  - tests/unit_tests/test_app_market_hub_install.py
  - jiuwenswarm/channels/web/frontend/tests/appMarketPanel.test.mjs
designs: []
design_note: 在允许删除本地应用目录之后收紧同一删除路径的确认与保护条件，沿用 S_03 契约，无独立设计。
---

# 本地应用删除确认、目录保护与清理反馈

## 现象与根因

- 本地应用可能指向用户的项目目录。允许清理安装根外的本地应用目录时，需要在删除请求中明确目标并保护系统、数据与市场目录。
- 编辑会话和其他进程可能占用应用文件。下架与磁盘清理有不同的成功条件，用户需要知道卡片已下架、清理是否完成，以及归档在哪里。
- 只读条目容忍只在 Windows 生效，POSIX 上 `clear_readonly` 直接返回；而 S_03、README 的描述未限定平台。
- 本地应用清理不再读取 `local_release_id`，该字段变为只写；`delete_app` 的注释与 `project_retained` 仍宣称项目不受影响。
- 清理失败时后端返回硬编码英文，前端中文界面直接内插；归档目录从未展示给用户。

## 修复决策

- 本地应用 `remove_files` 必须在 `confirm_work_dir` 中回显应用目录（按规范路径比较，Windows 不区分大小写），未回显或不匹配时返回 `app_delete_confirm_required`，登记与文件都不改动。Hub 卸载继续依赖安装记录与发布标记，不需要回显。
- 本地清理和 Hub 卸载共用文件系统根、用户主目录、JiuwenSwarm 数据目录、进程当前工作目录及其上级的保护检查。本地清理也保留市场安装根及其上级和归档目录，返回 `protected_directory`。
- 下架成功后直接尝试清理目录，编辑会话忙碌不影响下架。Windows 共享或锁冲突及文件系统忙碌报错统一返回 `filesystem_busy`，登记保持移除。应用更新继续按原契约检查编辑会话状态。
- `_remove_tree` 对只读条目在 POSIX 上补充写权限后重试，且从不 chmod 符号链接本身，避免改到链接目标。
- 前端按 `cleanup_code` 展示中英文说明。占用提示包含应用已下架和未能完全清理的目录，其他文件系统错误保留原始原因。已有归档时，成功和失败提示均给出归档目录。
- `delete_app` 的 `project_retained` 按 `work_dir` 与 `project_dir` 是否一致计算，并新增 `project_files_removed`；`local_release_id` 注释改为仅保留发布来源凭据。
- 上一版 B_20261124_1429 的"根外目录缺少归属凭据时保留"决策被本次删除能力有意取代，登记与文件保护改由回显确认、保护目录、身份核验和登记重叠四条规则承担。

## 验证

- Windows 项目 `.venv` 运行 `tests/unit_tests/test_app_market_hub_install.py`，53 项通过。覆盖本地删除和 Hub 卸载先移除登记、清理失败时保留归档、占用和权限错误的区分，以及更新的忙碌会话检查。
- Windows 临时目录实测打开的 `index.html` 触发 `WinError 32`。应用登记已移除，结果为 `filesystem_busy`，锁定文件和归档的 `data/save.json` 均存在。
- 前端 `npm run test:app-market`，93 项通过。覆盖文件清理失败后卡片下架、目录占用提示和归档位置。未运行全量套件，也未在 Linux/macOS 实机验证。

当前契约见 [S_03 应用分发与安装](../../../../specs/jiuwenswarm/extensions/app_market/S_03_app-hub-distribution.md)。
