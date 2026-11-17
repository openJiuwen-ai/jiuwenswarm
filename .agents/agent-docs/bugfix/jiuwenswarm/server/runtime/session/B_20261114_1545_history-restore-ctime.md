---
id: B_20261114_1545_history-restore-ctime
title: Windows 上会话历史恢复误报 HISTORY_RESTORE_CHANGED 导致老会话无法打开
type: B
author: CharlesWang2030
author_email: 93426525@qq.com
designs: []
design_note: 用户确认的恢复链路误报修复，无独立设计编号。
modules:
  - jiuwenswarm/server/runtime/session
files:
  - jiuwenswarm/server/runtime/session/history_replay.py
  - jiuwenswarm/server/runtime/session/context_snapshot.py
  - jiuwenswarm/server/runtime/session/context_migration.py
  - jiuwenswarm/agents/harness/common/session_ops_service.py
  - jiuwenswarm/extensions/video_duplex/tests/backend/test_shared_conversation_context.py
---

# 现象与原因

Windows 桌面端上打开一个发过消息的会话（或重启 worker 后首次进入旧会话）发消息，随机报错 `HISTORY_RESTORE_CHANGED: History changed during restore`，此后该会话每次进入都报同样错误，跨重启不自愈，表现为会话永久不可用；启动时批量恢复历史会话也会重复报错（实测单次启动 21 连败）。

根因：恢复链路用 `(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)` 作为历史文件指纹，并在读取后用两种查询来源交叉复核——`os.stat(path)` 按路径重新查询（走目录元数据缓存，返回真实创建时间），`os.fstat(fd)` 沿打开句柄读缓存元数据（把最后写入时间当作创建时间上报）。历史文件天然"创建后持续追加"，只要写入过一次，两源的 `st_ctime` 必然不一致（实测同一未修改文件相差约 16 秒，新文件相差约 1 毫秒），指纹比对随机失败。失败后不缓存增量 cursor，之后每次恢复都全量重扫并再次失败，形成粘滞表现。该问题与并发写入、竞态无关（复现时无任何进程写文件，校验序列 500/500 失败）；Linux/Kylin 上 stat/fstat 同源同字段，不受影响。

# 修复

从会话恢复链路的文件指纹中移除不稳定的 `st_ctime_ns`（5 元 → 4 元；dev/ino/size/mtime_ns 已覆盖文件替换与并发写检测），各产生点同步：

- `history_replay.py`：`_identity()` 去掉 ctime，空指纹哨兵同步；
- `context_snapshot.py`：同款 `_identity()`；快照加载兼容旧 5 元 / 新 4 元持久化 identity（ctime 槽位不参与校验，旧快照无需迁移，读一次即自愈）；
- `context_migration.py`：迁移边界 identity 同步；
- `session_ops_service.py`：`_history_file_identity()` 同步；
- `extensions/video_duplex/tests/backend/test_shared_conversation_context.py`：测试哨兵元组同步。

# 验证

- 单元测试：`test_history_replay.py`、`test_context_snapshots.py`、`test_history_index_paging.py`、`test_compact_history_records.py` 共 115 例，114 通过；1 例（`test_compact_history_records` 锁文件 Errno 13）经基线复跑同样失败，为本机环境性偶发，与本改动无关；
- 离线指纹探针：修复前对真实会话文件 500/500 错位，修复后 0 错位；
- 真实界面回归：重启 worker 后打开修复前必然报错的会话并发消息，恢复正常（合并后按「重启 worker → 打开旧会话 → 发消息」再回归一次）。

# 遗留与关联

同类 `st_ctime_ns` 指纹比对点（本修复不含，建议另行评估）：`server/runtime/marketplace/asset_package_builder.py:206`、`common/native_tokenizer_cache.py:43`、`agents/harness/common/rsi/harness_provider.py:681`、`runtime/cron/store.py:74`、`runtime/symphony/service.py:1893,2189`。
