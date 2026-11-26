---
id: B_20261126_1855_design-compat-test-symlink-degradation
title: design 数据根兼容测试在无软链平台断言未随产品降级
type: B
author: gunner_beat
author_email: gunner_heart@163.com
designs: []
design_note: profile 分区迁移与兼容软链的契约由 jiuwenswarm/common/paths.py 模块 docstring 承载（"平台禁止软链（如 Windows 未开开发者模式）→ 静默跳过"为既有设计降级）；本次仅修正测试断言未覆盖该降级分支的问题，无产品行为变化。
modules:
  - tests/unit_tests/common
files:
  - tests/unit_tests/common/test_design_data_root_compatibility.py
---

# design 数据根兼容测试的平台降级断言

## 现象与影响

`test_startup_keeps_saved_design_asset_paths_readable`（#6375 回归，随 B_20261125_1146 design 共享路径缺陷修复引入）在 Windows 未开开发者模式的机器上必挂：

```
subprocess AssertionError（子进程第 10 行）
assert get_agent_root_dir().resolve() == (root / 'agent').resolve()
```

挂的原因不是迁移缺陷：冷 import 时 `migrate_profile_data_root_layout` 正常执行，agent 子树剪切到 `profile/agent`（子进程单独验证 0.7s，路径正确）。挂的是断言前提——它假设数据根老位置 `agent/` 总会留下指向 `profile/agent` 的兼容软链，而 `jiuwenswarm/common/paths.py` 的 `_ensure_compat_symlinks` 明确设计"平台禁止软链（如 Windows 未开开发者模式）→ 静默跳过"：Windows 无 `SeCreateSymbolicLinkPrivilege` 时软链不建，老路径不存在，resolve 相等断言必失败。CI Linux 环境可建软链故不挂，本地 Windows 开发环境（未开开发者模式）跑该用例即红。

## 根因与修复决策

测试把"兼容软链存在"当成了普适契约，漏测产品已明确声明的设计降级分支。修复按产品实际契约分层断言，不 skip 用例（迁移后数据完整性在所有平台都要被验证）：

- 主进程在 `tmp_path` 试建软链探测本机能力（`_can_create_symlinks`）；
- 有软链能力（Linux/已开开发者模式的 Windows）：维持原断言——`get_agent_root_dir()` 与老路径 resolve 相等、老 URI（绝对+相对）经软链可读且内容不变；
- 无软链能力（Windows 未开开发者模式）：断言降级为"迁移后数据完好"——record 与 assets 落在 `profile/agent/designer/` 新布局下、内容逐字节不变；主进程末尾的 record 回读同步改读迁移后的位置。

子进程通过 `EXPECT_COMPAT_LINK` 环境变量感知模式，避免按 `sys.platform` 粗判（权限与平台非一一对应，能力探测才是准绳）。

检视第一轮补强两处：探测函数的清理永不抛错——Windows 上 unlink 目录软链可能失败，回退 `rmdir` 后仍失败则吞掉，保证探测契约恒为"返回布尔而非把用例变 error"；另加探测误判守卫——探测报告无软链能力而 `agent/` 兼容链接实际存在时直接断言失败，防止强断言因误判被静默跳过、守护 #6375 的力度被削弱。

## 回归证据与适用范围

- 红基线：修复前在本地 Windows（未开开发者模式）跑该用例，子进程断言失败（`1 failed in 39.87s`）；
- 修复后同一环境该用例绿；
- 有软链路径的断言逻辑与原版逐行一致（仅包进条件分支），Linux CI 行为不变。

适用范围：`tests/unit_tests/common/test_design_data_root_compatibility.py` 单文件；不涉及产品代码与其他测试。
