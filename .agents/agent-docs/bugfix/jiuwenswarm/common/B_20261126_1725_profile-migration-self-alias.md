---
id: B_20261126_1725_profile-migration-self-alias
title: 防止 profile 布局迁移经兼容路径删除自身持久数据
type: B
author: MengHan18
author_email: hanmeng22@huawei.com
modules:
  - jiuwenswarm/common
files:
  - jiuwenswarm/common/paths.py
  - tests/unit_tests/common/test_profile_migration_aliases.py
designs: []
design_note: 修复重启恢复回归暴露的数据迁移幂等缺陷，不改变既有 profile 布局。
---

# 现象与定位

在官方 develop `92a4ef2b0d44a3d1fa2a040e7d3132fb3ef07d84` 上适配 Worker 自动恢复时，Windows 真实进程测试完成步骤一，替换 Worker 后却始终没有步骤二。新进程已就绪，不能把它当作任务已恢复。

隔离测试临时跟踪 `Path.unlink` 得到调用链：Worker main 导入 common.utils → migrate_profile_data_root_layout → 迁移 agent 到 profile/agent → _move_if_absent → _migrate_absorb → _migrate_absorb_file → old.unlink。被删文件包括 effects.jsonl 与 lifecycle/resources/session_original.json；跟踪代码已移除，不进入生产实现。

旧、新路径可以经 Windows junction 或其他别名指向同一份数据。原逻辑只排除 is_symlink，随后把同一文件判作“内容相同的副本”，删除源路径便同时删除正本。普通文件以自身路径为目标也存在同样问题。该迁移逻辑在未修改的上游基线已存在，不是自动恢复服务新建的删除动作。

# 修复与边界

文件归并、目录归并进入前先比较解析后的路径，相同则直接返回，不递归、不去重、不剪除。不同目录仍沿用既有行为：相同内容去重、不同内容保留正本并将旧内容另存为 .migrated。不关闭迁移、不改变目录布局、不跳过恢复检查点校验。

# 验证

新增四项回归：文件以自身为目标的两种归并入口、真实 Windows junction 下重复迁移保留检查点和 lifecycle 记录、不同目录仍能去重并保留冲突备份。Windows / Python 3.11.17 下四项通过。另用独立模块加载器分别加载官方基线和候选的 paths.py，避免 pytest 路径注入影响对照：官方的同路径文件与 junction 检查点两项均丢失，候选两项均保留。实际 Worker 换代恢复结果记录在 F_20261123_1801_team-worker-automatic-recovery。

本修复防止之后的自归并删除，不能恢复已经被删除的数据，也不能据此解释历史实验的所有崩溃或心跳失联。
