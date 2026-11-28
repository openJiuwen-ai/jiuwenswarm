---
id: B_20261128_1100_legacy-skill-graph-migration
title: 升级后导入旧版技能图谱
type: B
author: lin-xiaoyu
author_email: xiaoyu.lin.work@outlook.com
modules:
  - jiuwenswarm/common
  - jiuwenswarm/runtime/symphony
files:
  - jiuwenswarm/common/config.py
  - jiuwenswarm/common/version_source.py
  - jiuwenswarm/runtime/symphony/legacy_graph_migration.py
  - tests/unit_tests/symphony/test_runtime_contract_integration.py
designs: []
design_note: 恢复技能图谱的升级兼容，由现有 Core 发布流程维护新版图谱。
---

# 升级后导入旧版技能图谱

## 现象与根因

`de8b221aa` 和 `5600f8673` 引入统一 Runtime 后，默认图谱从工作区的 `symphony/graph` 改存 `symphony/runtime/graph`。旧版已发布的 `current.json` 和版本工件仍可由 Core 的 `GraphArtifactStore` 解析。直接复制到新目录时，Runtime 会拒绝已含工件、但尚未绑定技能集合的目录。

## 修复决策

升级导入置于工作区启动的配置同步事务中，在写入新 `config_version` 前执行。应用版本优先排序，同发布版本再比较构建日期。没有版本记录的配置须匹配旧 Symphony 配置结构。迁移同时限定旧图 `1.0`、旧来源快照 `JiuwenSwarm-symphony-graph-source-v2` 和目标 `symphony.runtime.v1`。后续存储格式迁移应声明各自的源格式和目标格式。导入失败时保留旧配置及版本戳供下次启动重试。图谱读取、状态查询和普通构建均沿用 Core 的既有入口。

迁移覆盖默认旧目录、自定义旧图目录和手动搬入 Runtime 的旧图。后者先移动到 Runtime 根目录旁的备份，重试可继续从备份导入。编排关闭时仍可保存旧图，保留原开关。单技能集合绑定由 Core 的 `RuntimeStorage` 完成，图谱由 Core 的 `GraphArtifactStore` 有条件激活，已有发布版本优先。导入保留原始节点、边和来源快照，新鲜度继续由 Core 判断。

## 回归证据

修复前用已锁定的 Core `31171fb02` 复现默认目录、自定义目录和手动复制三种场景，对应回归用例均失败。首次修复的前端布局与轮询测试 6 项通过。升级入口调整后，Symphony 和启动配置相关扩展回归 688 项通过。补充编排关闭场景后，图谱、配置与版本定向回归 256 项通过。回归覆盖首次安装、同版本启动、降级、未知版本、同版本号的新构建、未知源或目标格式、导入中断重试和新版图优先。使用临时工作区和旧格式样例验证，桌面安装包原地升级仍待联调。产品中英文文档已同步升级行为。

回合 `releases/0.2.10` 时改用该分支的 `get_agent_workspace_dir()`。以基线 `24633ead1` 和锁定的 Core `76b5a0a6` 在 macOS arm64、Python 3.12 上重跑 Symphony、图谱 Web 路由、配置、工作区初始化及版本判断回归，689 项通过。

补提 `develop` 时使用 `get_symphony_dir()` 并保留该分支的启动配置流程。以基线 `8ee15e7e4` 和锁定的 Core `61421984`、Gateway `e4eb71be` 在 macOS arm64、Python 3.12 上重跑上述范围及启动配置快照和数据布局回归，703 项通过，2 项 Windows 目录联接用例跳过。
