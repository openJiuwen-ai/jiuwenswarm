---
id: B_20261126_1308_session-metadata-corrupted-marker
title: 损坏 metadata.json 的会话在列表中伪装成合法旧会话，团队身份字段静默丢失
type: B
author: zhangpu
author_email: zhangpu161@163.com
modules:
  - jiuwenswarm/server/runtime/session
files:
  - jiuwenswarm/server/runtime/session/session_metadata.py
  - jiuwenswarm/server/runtime/session/session_info.py
  - tests/unit_tests/agentserver/test_session_metadata_corrupted.py
designs: []
design_note: 防御性呈现契约修复，复用 _read_metadata 既有读路径补显式失败状态并在合成层打标、投影层透传，无独立设计文档。
---

# 损坏 metadata.json 的会话在列表中伪装成合法旧会话，团队身份字段静默丢失

## 现象与影响

`metadata.json` 读取失败（进程被 kill 留下半写文件、磁盘满留空文件、JSON 损坏、
内容非 dict）与文件不存在在 `_read_metadata` 中共用 `{}` 返回值。
`get_all_sessions_metadata` 把两种情况都按「旧会话无 metadata」合成默认值：
`team_name` / `dispatch_mode` / `work_mode` 等身份字段静默丢失，团队会话打开后
按单 agent 渲染、团队看板与轨迹入口消失，用户看到的损坏会话与正常旧会话
完全无法区分。

## 关联设计与当前契约

- `_read_metadata` 的 `{}` 返回契约是 read-modify-write 调用方的既有依赖，
  本次修复不改变该契约（`_read_metadata` 保持原签名与返回，作为
  `_read_metadata_with_status` 的薄包装存在）。
- 单会话读取链路 `session.get_metadata` 对损坏会话经 `if not meta` 返回
  NOT_FOUND（诚实拒绝）；本修复使列表链路的呈现语义与之对齐。
- 消费投影：Web/网关 `session.list` 的唯一出口是
  `session_info.to_session_info` 白名单投影，标记必须在该处透传才能到达
  前端消费方。

## 根因与修复决策

根因：`_read_metadata` 对「文件缺失」「读取失败」「合法空对象」三种语义
不同的情形共用 `{}` 返回，列表合成层无从区分；修复若改 `_read_metadata`
契约将波及全部 read-modify-write 调用方，风险不成比例。

修复决策（三层配合）：

1. **读取侧显式失败状态**：新增 `_read_metadata_with_status` 返回
   `(metadata, read_failed)`，`read_failed=True` 仅限「文件存在但不可读」
   （半写窗口、磁盘满、解析失败、非 dict、空文件）；文件不存在与合法空
   对象 `{}` 均不算失败。以此否决了初版实现用
   `(session_dir / "metadata.json").exists()` 反推损坏的方案——该方案会把
   合法空 JSON 与一次性 OSError（Windows 跨进程原子替换的 sharing
   violation 等，`_FILE_LOCK` 仅进程内）误标为损坏，且存在读后写窗口的
   TOCTOU 误标。
2. **合成层打标**：列表合成默认值后按 `read_failed` 打
   `metadata_corrupted: True` 并输出含 session_id 的告警日志；标记仅在
   内存结果上，`enable_writeback=False` 防止标记被推断写盘持久化。
3. **投影层透传**：`to_session_info` 增加
   `"metadata_corrupted": bool(meta.get("metadata_corrupted", False))`，
   与 `execution_blocked` / `stop_pending` 既有模式一致；否则标记在
   Web 主链路被白名单投影静默丢弃，修复仅在后端日志生效。

范围边界：另一批量入口 `_collect_all_sessions_metadata`（项目统计/置顶
聚合消费）本次未同步打标，属后续 PR 范围；TUI/Web 前端消费逻辑亦未在本
PR 落地（标记层先行，呈现由后续 PR 配套）。

## 回归证据与适用范围

2026-11-26 验证：`tests/unit_tests/agentserver/test_session_metadata_corrupted.py`
参数化覆盖四种文件形态——截断 JSON / 空文件 / 非 dict JSON（三者必须打标）
与合法空对象 `{}`（不得打标），并断言 `to_session_info` 投影后标记仍然
可达、文件缺失场景不打标。执行方式：仓库 venv 的 openjiuwen SDK 陈旧
（缺 ToolExecution），按惯例以 uv.lock 锁定 rev 安装到独立目录后以
`PYTHONPATH=<pin>:<worktree>` 复跑。

适用范围：`get_all_sessions_metadata` 列表链路（Web/网关/项目查询共用
投影）；不改变 `_read_metadata` 返回契约与单会话读取链路行为。
