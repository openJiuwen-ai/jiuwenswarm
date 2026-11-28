---
id: B_20261128_1043_agent-gateway-team-quota-lease-pin
title: 团队配额孤儿租约：gateway pin 对齐到兜底回收版本
type: B
author: hanxinyang
author_email: xyhan226@163.com
modules:
  - jiuwenswarm/server
  - agent-gateway/enterprise/team
files:
  - pyproject.toml
  - uv.lock
  - agent-gateway/agent_gateway/enterprise/team/abandoned_hold.py
  - agent-gateway/tests/unit_tests/enterprise/test_team_abandoned_hold.py
  - tests/unit_tests/server/test_server_regressions.py
  - tests/unit_tests/agentserver/test_team_quota.py
designs: []
design_note: 承接 Issue #141 的删除清理修复链，按依赖 pin 收尾，无独立设计。
---

# 团队配额孤儿租约：gateway pin 对齐到兜底回收版本

## 现象与影响

Issue [#141](https://gitcode.com/ZYQ5333/jiuwenclaw/issues/141)：集群模式删除团队后，管理面显示"0 个团队、已用 1"，用户额度被一条已不存在的团队长期占住。

`team_quota_lease` 的 `committed` 行 `expires_at` 为 `NULL`，不会自然过期；只要没有代码显式置为 `released`，该行就一直计入 `snapshot_user` 的占用。`team_entity` 已删除但租约仍在时，就形成"幽灵占用"。

## 关联设计与当前契约

- 同域上游修复 `B_20261126_0212_team-delete-partial-quota-release`：`handle_team_delete` 对 `result.session_ids` 逐条按会话清理，解决运行期部分删除后的残留。
- 本仓 `pyproject.toml` 以 git rev 方式 pin `agent-gateway`，`jiuwenswarm/app.py` 用 `python -m agent_gateway.app_gateway` 从当前 venv 拉起 gateway，因此 **pin 决定运行时是否具备兜底回收能力**。
- 依赖仓 `openJiuwen/agent-gateway` !364（merge node `eeb3f1bc2`）在 `agent_gateway/enterprise/team/abandoned_hold.py` 的维护循环里新增 committed 孤儿租约回收，由 `agent_gateway/application/services.py` 启动的 `run_abandoned_hold_release_loop` 周期执行。
- 无独立 D：本次只是依赖版本对齐，行为契约由上述两侧 PR 定义。

## 根因与修复决策

本仓原 pin `bb699e4362f983872772b1e369e0a00638c999fe` 正好是 !364 merge node 的**第一父提交**，即 !364 合入前的 develop 节点。于是：

- 打包/安装的 gateway 不含 `abandoned_hold.py` 中的 committed 回收分支；
- `B_20261126_0212` 依赖的"带内释放失败仍有兜底"这一层在运行时不存在；
- 部分删除路径、进程重启、带内释放异常等场景仍会留下孤儿 `committed` 租约，用户占用只能等 AgentServer 侧其他路径碰巧清理。

修复决策：

1. 把 `agent-gateway` pin 从 `bb699e4362` 升到 `eeb3f1bc2`，并同步 `uv.lock`。三处声明（`dependencies`、`harmony` extra、`[tool.uv.sources]`）与锁文件三处引用（`source`、`requires-dist` ×2）保持一致。
2. **只对齐到 !364 的 merge node**，不跟随 gateway develop 最新提交。`merge-base` 验证 `eeb3f1b` 是 merge node，第一父即旧 pin，diff 仅 `abandoned_hold.py` + 对应单测，不夹带其他改动。
3. `uv.lock` 不整份重 lock，只替换 gateway rev，`revision` 保持 `3`，避免新版 uv 的 marker 归一化噪声混进这个 6 行 PR。

回收逻辑本身的分工与边界：

- **AgentServer 带内释放**（`!10937` / `B_20261126_0212`）：删除请求返回成功后立即按会话释放，正常路径不依赖 gateway。
- **Gateway 兜底回收**（`!364`）：维护循环每 60s 执行一次 `release_abandoned_holds()`；对 `kind='team_create'` 且 `status='committed'` 的行，若对应 `team_entity` 不存在或状态不在 `creating/running/paused/stopping` 内，则置为 `released`。
- **防误回收**：`COMMITTED_ORPHAN_MIN_AGE_SEC = 300`，只处理 `updated_at` 早于 5 分钟的行，避免把并发提交中的新租约当成孤儿；`stopped`/`deleted` 实体按"不再占用配额"处理。

## 回归证据与适用范围

环境：Windows，`uv sync --locked` 后使用仓内 `.venv`。

- `uv lock --check` 通过（338 packages），`uv.lock` 的 `revision` 保持 `3`。
- `uv sync --locked` 后检查 venv 内 `agent_gateway/enterprise/team/abandoned_hold.py`，已含 `COMMITTED_ORPHAN_MIN_AGE_SEC` / `STATUS_COMMITTED`，确认安装物来自 `eeb3f1bc2`。
- `pytest tests/unit_tests/agentserver/test_team_quota.py` → 38 passed。
- `pytest tests/unit_tests/server/test_server_regressions.py -k "team_delete or purge or quota"` → 5 passed, 33 deselected（退出码非 0 仅因 Windows 临时目录 `team_route_bindings.db` 清理 `PermissionError`，用例本身全通过）。
- 依赖仓 `!364` 自带 `tests/unit_tests/enterprise/test_team_abandoned_hold.py`，覆盖孤儿 committed 回收、年轻行不回收、活跃实体不回收、`team_entity` 表缺失时不影响 reserved 回收。

未验证范围：真实 MySQL 共享团队库上的并发提交竞争、跨 Pod 的维护循环时序；这些需在集群环境复测。适用前提为 `is_enterprise()` 为真且使用共享 team store，个人版在本仓与 gateway 两侧都会直接返回。

## 防错模式与资料同步

- 依赖 pin 升级必须做 `merge-base` 验证：确认目标提交是目标 PR 的 merge node，且第一父等于旧 pin，才能保证 diff 范围可控、不夹带无关提交。
- 修"清理时机"类缺陷时，要同时核对**调用方代码**与**实际安装的依赖版本**；只改本仓代码但 pin 指向旧版本，兜底逻辑不会生效。
- 已在本文记录两侧分工与 pin 依据，与 `B_20261126_0212_team-delete-partial-quota-release` 互相引用。

## 遗留与后续

- `agent-gateway:develop` 在 `eeb3f1b` 之后还有 southbound 重构等改动，本次刻意不跟；后续整体升级 pin 时再评估。
- 真实集群下的孤儿回收效果需在部署环境验证：删除团队后观察 `team_quota_lease` 中对应行是否在 5 分钟窗口后被置为 `released`。
