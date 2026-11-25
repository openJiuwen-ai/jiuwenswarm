---
id: B_20261125_1022_workspace-copystat-eperm
title: 共享 PVC 场景下工作区增量初始化容错 copystat EPERM
type: B
author: Xiangju Zhu
author_email: 68637701+zxj0302@users.noreply.github.com
modules:
  - jiuwenswarm/common
files:
  - jiuwenswarm/common/utils.py
  - tests/unit_tests/common/test_workspace_copystat_resilience.py
designs: []
design_note: 针对 jiuwenswarm/common/utils.py 工作区初始化元数据同步的最小可靠性修复，不修改对外 API 或默认目录布局，无独立设计文档。
---

# 共享 PVC 场景下工作区增量初始化容错 copystat EPERM

关联 issue：GitHub #12779。

## 现象与根因

在共享 PVC（如 OpenShift RWX PVC + emptyDir infra）部署中，若 `<root>/agent/workspace` 或 `<root>/agent/workspace/memory` 已存在且属主 UID 与当前 Pod 运行 UID 不一致，Pod 重建启动时因 `<root>/config/config.yaml` 缺失触发 `prepare_runtime_workspace()` → `prepare_workspace()`：

1. `prepare_workspace` 内部的 `_copy_dir()` 在目标目录已存在时调用 `shutil.copytree(..., dirs_exist_ok=True, copy_function=copy_if_missing)`；
2. Python 标准库 `shutil.copytree` 在每个目录复制完成后会无条件调用 `shutil.copystat(src, dst)` 执行 `chmod` / `utime`，在运行 UID 不是目标目录属主且无 `CAP_FOWNER` 时抛出 `PermissionError(EPERM)`，汇总为 `shutil.Error` 导致 AgentServer worker 启动崩溃；
3. 同文件单文件复制（`shutil.copy2`）与 `_install_default_builtin_skills` 的 `overwrite=True` 覆盖分支也存在同类元数据同步失败风险。

## 修复决策

1. **严格区分写入失败与元数据同步失败**：在 `jiuwenswarm/common/utils.py` 新增 `_copystat_best_effort`、`_copy_file_resilient` 与 `_copy_dir_tree_resilient`；目录创建（`mkdir`）与文件内容写入（`shutil.copyfile`）保持严格抛错，仅对 `shutil.copystat` 的 `PermissionError` 及 `EPERM` / `EACCES` / `ENOTSUP` / `EOPNOTSUPP` 降级为 `WARNING` 日志并继续；非权限/文件系统限制类 `OSError`（如 `EIO`）继续原样抛出。
2. **覆盖工作区初始化与内置目录合并剩余路径**：在保留上游 `_merge_tree_keep_dir_metadata` 处理 `prepare_workspace._copy_dir` 的基础上，将 `prepare_workspace` 内的单文件 `shutil.copy2`（`config.yaml`、`.env`、多语言模板文件、`skills_state.json`）及 `_install_default_builtin_skills` 的 `overwrite=True` 分支接入 `_copy_file_resilient` / `_copy_dir_tree_resilient`；首次安装的临时目录 staging 仍沿用 `shutil.copytree` 以保持既有测试与原子重命名行为兼容。
3. **部署侧注意事项**：共享 PVC 场景仍建议保持运行 UID 稳定并与卷上目录属主一致（或由平台初始化属主），应用层容错仅保证跨 UID 元数据同步失败不再阻断启动。

## 验证与边界

- 新增 `tests/unit_tests/common/test_workspace_copystat_resilience.py`（7 项用例，含参数化 `EPERM` / `EACCES` / `ENOTSUP`），覆盖目录树递归复制容错、真实写失败/非权限 `EIO` 必须透传、`prepare_workspace` 模拟共享 PVC + emptyDir 重建、`prepare_runtime_workspace` 端到端入口与 `_install_default_builtin_skills(overwrite=True)`。
- 本地验证：`test_workspace_copystat_resilience.py` + `test_builtin_catalog.py` + `test_mcp_builtins_e2e.py` + `test_ensure_mcp_builtins.py` + `test_dependency_provenance.py` 共 55 passed，以及 `test_utils.py` 工作区初始化相关用例全部通过。
