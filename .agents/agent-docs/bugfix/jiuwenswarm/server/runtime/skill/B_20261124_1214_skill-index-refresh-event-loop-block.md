---
id: B_20261124_1214_skill-index-refresh-event-loop-block
title: Skill 安装与索引刷新同步 rglob 扫描阻塞 Worker 事件循环
type: B
author: Xiangju Zhu
author_email: 68637701+zxj0302@users.noreply.github.com
modules:
  - jiuwenswarm/server/runtime/skill
  - jiuwenswarm/server/runtime/agent_adapter
files:
  - jiuwenswarm/server/runtime/skill/skill_manager.py
  - jiuwenswarm/server/runtime/skill/skillpack_install.py
  - jiuwenswarm/server/runtime/agent_adapter/interface.py
  - tests/unit_tests/agentserver/test_skill_index_refresh_nonblocking.py
designs: []
design_note: 针对异步事件循环被同步磁盘扫描阻塞的运行时缺陷修复，不改变外部 RPC 协议与 agent-data.json 产物格式，无独立前置设计文档。
---

# Skill 安装与索引刷新同步 `rglob` 扫描阻塞 Worker 事件循环修复记录

## 现象与影响

关联 Issue：GitHub #12645 / GitCode #6979。

- 在 CI 并行高负载或技能数量较多的工作区下，调用 `skills.import_upload`、`skills.create_from_knowledge`、`skills.teamskillshub.install` 等异步 RPC 时，worker 事件循环心跳停摆超过 27s（`worker_loop_beat_age` 聚簇在 26.9s–27.6s），被 supervisor 判定 IPC 断连并强杀重启。
- 受影响期间所有在途请求（含同窗 cron run）统一返回 `WORKER_CLOSED (runtime worker closed)`，且由于卡死发生在安装落盘之后的索引刷新阶段，导致 RPC 报失败但技能实际已安装落盘的状态背离。

## 关联设计与当前契约

- 无独立设计文档（`designs: []`）。
- 保持 `SkillManager._refresh_agent_data_indexes()` 同步方法签名及 `agent-data.json` JSON 结构、排序规则与中英文后缀去重规则不变，兼容已有单测与同步内部调用方。
- 保持 `inventory_mutex` / `state_transaction` 锁语义：锁必须在同一执行线程内获取与释放，不跨 `await` 持锁。

## 根因与修复决策

### 根因

1. **事件循环阻塞**：`handle_skills_import_upload`、`handle_skills_import_local`（含本地路径、download_token 与远程归档分支）、`handle_skills_team_skills_hub_install`、`handle_skills_install`、`handle_skills_clawhub_download`、`handle_skills_team_skills_hub_init`、`handle_skills_pack_member_install`、非企业版 `handle_skills_list`、`SkillPackInstaller.commit` 以及 `interface.py` 的 `finalize_create_from_knowledge` 调用链，均在 asyncio 事件循环线程直接同步执行文件拷贝/解压、`inventory_mutex` 加锁及 `_refresh_agent_data_indexes()` 索引重建。
2. **隐藏目录未剪枝与非原子写**：`SkillManager._generate_agent_data_for_workspace()` 使用 `sorted(agent_root.rglob("*"))` 全量递归遍历整个 workspace，会深入 `.archive`、`.agent_history`、`.skillpack`、`.git` 等隐藏目录并对所有条目调用 `_is_file_long()` 后再丢弃，磁盘 I/O 随历史与版本归档增长无界放大；且直接 `output_path.write_text(...)` 在并发线程写入时非原子。

### 修复决策

- **剪枝扫描 + 原子写入**（`skill_manager.py`）：
  - `_generate_agent_data_for_workspace` 改用 `os.walk(..., topdown=True, followlinks=False)`，原地剪枝 `dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))`，跳过所有隐藏目录不再进入；保持 Windows 长路径（`_long_path` / `_is_file_long`）与 `agent-data.json` 输出格式完全一致。
  - 写入改用隐藏唯一临时文件（`.agent-data.<pid>.<tid>.<uuid>.tmp`，前缀 `.` 使并发扫描自动忽略）并在专用 `_AGENT_DATA_WRITE_LOCK` 保护下执行 `os.replace`，兼顾 POSIX 原子性与 Windows 并发替换安全。
- **全量卸载出事件循环与并发锁兜底**（`skill_manager.py` / `skillpack_install.py` / `interface.py`）：
  - 新增 `SkillManager._refresh_agent_data_indexes_async()`（内部调用 `asyncio.to_thread(self._refresh_agent_data_indexes)`）。
  - 将上述所有受影响的 `async` RPC 入口的同步解压/落盘/`inventory_mutex` 提交/未管理技能登记/索引刷新统一通过 `asyncio.to_thread` 下沉到工作线程执行，确保锁获取与释放在同一工作线程内闭环。
  - `_install_imported_skill_dir` 改为在工作线程内使用 `_SKILL_IMPORT_LOCK_TIMEOUT_SECONDS = 5.0` 有界等待 `inventory_mutex`（与 `skills.toggle` 保持一致），使并发导入请求能够有序排队完成；若超时，各导入入口统一捕获 `InventoryLockTimeout` 返回结构化 `SKILL_INVENTORY_BUSY`，且 `handle_skills_import_upload` 保留已上传 zip 供用户重试。
- **备选与拒绝原因**：
  - 仅做后台 fire-and-forget 异步刷新被拒绝：部分调用方与前端在安装返回后立即可读取 `agent-data.json`（及测试断言落盘顺序），改为 `await asyncio.to_thread(...)` 既保证事件循环心跳持续活跃，又维持返回即索引就绪的强一致契约。

## 回归证据与适用范围

- 候选分支：`fix/skill-index-refresh-nonblocking`（基于 `upstream/develop` `639ae62`）。
- 新增回归测试：`tests/unit_tests/agentserver/test_skill_index_refresh_nonblocking.py`（10 项测试）：
  - 验证隐藏目录（`.archive`/`.agent_history`/`.git`/`.skillpack`）原地剪枝不进入、输出 schema 与中英文去重一致、符号链接目录不跟随、悬空链接安全跳过；
  - 验证多线程并发写入与并发读取 `agent-data.json` 无异常、无 `.tmp` 泄漏；
  - 验证 `skills.import_upload`、`skills.import_local`、`skills.teamskillshub.install`、`skills.teamskillshub.init`、`skills.list`、`SkillPackInstaller.commit` 与 `skills.create_from_knowledge`（经 `JiuWenSwarm._handle_skills_request`）在模拟慢索引刷新（0.2s）期间事件循环 10ms 心跳持续推进且刷新均运行在工作线程；
  - 验证并发双 `skills.import_upload` 排队均成功无丢包，以及 `inventory_mutex` 持续占用超时时返回 `SKILL_INVENTORY_BUSY` 且保留上传 zip。
- 存量回归：运行 `tests/unit_tests/agentserver/` 下 15 个技能相关单测文件全部通过。
- 未验证范围：未在真实 200+ 技能高并发 CI 集群环境执行长稳压测；仅在 Linux Python 3.12 环境完成单元测试验证。

## 防错模式与资料同步

- **防错模式**：`SkillManager` 的所有 `async` RPC 入口不得在事件循环线程直接调用 `_refresh_agent_data_indexes()`、`inventory_mutex` 写提交或全工作区扫描，须统一经 `asyncio.to_thread`（或 `_refresh_agent_data_indexes_async`）卸载到工作线程；目录遍历若忽略隐藏目录，必须使用 `os.walk` 原地剪枝 `dirnames[:]` 而非 `Path.rglob("*")` 后过滤。
- 已同步文档索引（`sync_documents.py`）。

## 遗留与后续

- `jiuwenswarm/channels/web/app_web.py` 中的 `_generate_agent_data` 仍使用 `rglob("*")` 扫描，不属于 worker 运行时事件循环阻塞链，可按需在后续单独优化。
- Issue #12645 第 2 条预期中关于"worker 真实 OOM/崩溃重启后 RPC 层区分结果未知"属于 supervisor/gateway 通用重试语义，超出本次索引阻塞修复边界，需另案跟踪。
