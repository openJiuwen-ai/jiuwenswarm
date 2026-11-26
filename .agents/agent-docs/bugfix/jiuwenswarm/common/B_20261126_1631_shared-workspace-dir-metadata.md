---
id: B_20261126_1631_shared-workspace-dir-metadata
title: 共享卷上工作区准备因已存在目录元数据 EPERM 导致 AgentServer 启动失败
type: B
author: wangmingqi
author_email: wangmingqi@huawei.com
modules:
  - jiuwenswarm/common
files:
  - jiuwenswarm/common/utils.py
  - tests/unit_tests/test_utils.py
  - docker/start.sh
designs: []
design_note: 对既有工作区模板播种行为的局部缺陷修复与语义收紧，不新增架构方案。
---

# 共享卷上工作区准备因已存在目录元数据 EPERM 导致 AgentServer 启动失败修复记录

## 现象与影响

OpenShift full 形态复用 nfs-client PVC 承载 domain 分区时，Pod 反复起不来，
readinessProbe 不通。Worker 日志里 `prepare_workspace` 抛 `shutil.Error`，
报错列表同时包含 `<root>/domain/agent/workspace/memory` 与工作区根目录
`<root>/domain/agent/workspace` 本身，错误均为 `Operation not permitted`；
而模板文件内容实际已全部拷贝到位——失败项只有目录元数据。

触发条件组合：历史部署（uid 1005020000）在共享卷上创建过工作区目录，
restricted-v2 SCC 按 namespace 分配随机 uid，当前 Pod 已非这些目录的属主。

## 关联设计与当前契约

无独立设计。本次将 `prepare_workspace` 模板播种的隐式契约显式化：

- 播种是**增量合并**：只补缺失的文件与目录，已存在的用户内容（记忆、笔记、
  技能）原样保留，`overwrite=True` 的重置语义除外。
- **已存在的目标目录保留自身元数据**，播种过程完全不对其 chmod/utime——
  包括已存在的目标根目录（issue 报错中 EPERM 恰好落在根目录上）。
- 仅对本次**新建**的目录/文件套用模板元数据，且为 best-effort：失败降级为
  warning，不阻断启动（NFS root_squash 等场景 chmod/utime 一律被拒）。
- 真实内容失败（copyfile 失败、模板目录不可遍历）仍聚合抛 `shutil.Error`，
  不允许静默产出缺文件却报"工作区已就绪"的不完整工作区。

## 根因与修复决策

`shutil.copytree(dirs_exist_ok=True)` 在**每个**访问过的目录收尾都无条件执行
`copystat`（chmod + utime），即便该目录早已存在且本次未新增任何文件。chmod/utime
只允许属主执行，非属主一律 EPERM，于是整个 runtime 准备被判失败。

修复决策：不再用 copytree，`_merge_tree_keep_dir_metadata` 自行 `os.walk` 遍历，
按 fresh / pre-existing 分流——新建目录才 copystat，已存在目录完全不触碰。

备选与拒绝原因：

- **包一层 try/except 容错 copytree 的 copystat**：copytree 没有任何钩子可以
  跳过"已存在目录"的 copystat（`copy_function` 只管文件），只能 monkeypatch
  标准库或改写返回值聚合逻辑，不可维护；且 copytree 对文件元数据失败同样
  计入 `shutil.Error`，无法只放行元数据类失败。
- **部署侧 chgrp 0 / g+rwX 修复存量属主**：属部署层手段，修的是"新 uid 对
  历史目录的写权限"，与本仓"播种不该改写已存在目录元数据"是两个正交问题；
  即使属主对齐，copytree 仍会无谓地 chmod/utime 每个目录。部署侧配合见下。

配套决策（同 Issue 的部署侧行为）：

- `docker/start.sh`（gateway / agentserver 镜像 CMD）顶部设 `umask 0002`：
  domain 分区为多副本共享 PVC，restricted-v2 下进程 gid 0 运行，umask 022 会让
  新建目录 0755 / 文件 0644 只有属主可写，Pod 重建换 uid 后对前任写入的记忆、
  会话只能读不能改。umask 0002 → 0775 / 0664，同属 gid 0 的后继 Pod 始终可写。
  固定 uid 的通用 k8s 部署无副作用。写在 CMD 而非 Dockerfile（RUN umask 对
  运行期无效）。
- 遍历用 `os.walk(..., followlinks=True)` 镜像 `copytree(symlinks=False)` 的
  行为（ descend 进符号链接目录、按真实条目拷贝），并**必须**提供 `onerror`
  回调：os.walk 默认静默跳过无法 scandir 的目录，模板不可读时会产出不完整
  工作区（检视意见指出，已补聚合报错与回归用例）。

## 回归证据与适用范围

环境：本仓 develop，uv + Python 3.14；Issue #7025 现网堆栈如上。

- `uv run pytest tests/unit_tests/test_utils.py`：73 passed；含覆盖
  `test_merge_tree/prepare_workspace_rerun` 系列回归用例：
  - 已存在目录（**含目标根目录**）不被 copystat；
  - 元数据全拒绝（EPERM）时内容仍完成；
  - 真实 copyfile 失败仍聚合 `shutil.Error`；
  - 模板目录不可遍历（scandir EPERM）聚合报错——曾缺失，本次补上，且已
    反向验证：还原该修复后用例失败；
  - ignore 语义与 copytree 对齐；
  - 二次启动（删 config.yaml 回放 emptyDir 首装路径 + 已有外来属主目录）
    不失败，并以 spy 断言播种路径确实执行。
- `uv run pytest tests/unit_tests/common/test_ensure_mcp_builtins.py
  tests/unit_tests/common/test_mcp_builtins_e2e.py`：24 passed。
- umask 继承：实测 `umask 0002` 经 bash → python 子进程继承，新建目录
  `drwxrwxr-x`、文件 `-rw-rw-r--`；`bash -n` 通过。

未验证范围：真实 OpenShift + NFS 现网重放（依赖部署侧姊妹变更，见遗留）；
Windows 路径分支未受影响（遍历与 copystat 语义平台无关）。

## 防错模式与资料同步

- 对共享卷上已存在的路径只做**内容级**操作；元数据操作（chmod/utime/copystat）
  仅允许落在自己本次新建的条目上，且失败降级不阻断。
- "标准库默认吞错"要点查：`os.walk` 默认丢弃 scandir 错误，替换 `copytree`
  这类自带错误聚合的调用时，必须显式补 `onerror`，否则失败面从"启动报错"
  退化为"运行期缺文件"。

已同步：本记录 + 索引（sync_documents.py 重新生成）。

## 遗留与后续

- 新随机 uid 对**历史属主已存在文件**的写权限，属部署侧问题：需按 OpenShift
  任意 uid 规范对共享卷补 chgrp 0 / g+rwX（同 Issue #7025 的姊妹 PR !10916
  处理部署侧，本仓只解决启动崩溃与后续新建条目的组可写）。
- `_install_default_builtin_skills` 的强制重置分支（`jiuwenswarm/common/utils.py`
  中 `shutil.copytree(source, target, dirs_exist_ok=True)`）在共享卷 + 外来属主
  目录下会踩同一 EPERM；仅 `overwrite=True`（`init -f`）触发，常规启动不经过，
  本次未改，留待需要时同样切到 `_merge_tree_keep_dir_metadata` 或部署侧保证属主。
- `_copy_dir` 仅有 `prepare_workspace` 内两个调用点，无其他调用方依赖其原
  copytree 语义（`git grep` 核对）；rsi/skill_manager 等处另有独立 copytree
  用法，语义各自维护，不受本次影响。
