# 会话生命周期与自升级

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_14 / session-lifecycle-and-upgrade |
| 关联模块 | jiuwenswarm/common/{todo_generation, todo_snapshot, cleanup, cron_team_completion, git_safe_directory, updater, updater_restart_helper, upgrade_executor, version, version_source}.py |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管 **会话遗留物的生成 / 恢复 / 清理**（todo、过期目录、git 提示）与**自升级链路**
（版本真源、三源获取、执行器、重启守护）。工作区文件的业务级删除安全归 [[S_07]]；
cron 调度本身在 gateway/cron（本 spec 只管收轮判定状态机）。

## 自升级链路协作

```
version.__version__（唯一版本真源）
  → updater.UpdaterService（编排：检查/状态机/驱动）
      → version_source（GitHub / GitCode / PyPI 三源；release_sort_key 总序比较）
      → upgrade_executor.create_executor()
          ├─ DesktopExecutor：只下载 .part → 原子 replace（安装交给桌面端 pywebview）
          └─ PipExecutor：pip/uv install --upgrade → 写 .restart_pending.json
                          （argv/env/pid/端口，fsync）→ 拉起 restart_helper → 本进程 SIGTERM
              → updater_restart_helper（独立守护，python -m 运行）
                  等父进程退出（60s）→ 杀 web → 等端口释放（15s）
                  → detached 重启主进程 → 删 restart 标记
```

## 不变量

- **N1 todo 代际过滤向后兼容**：`filter_todos_by_generation` 对**无 token 的旧条目一律
  保留**；token 为空则原样返回；兼容 dict 与对象两种形态。bump / get token 的逻辑在
  `todo_resume.py`（agents 侧）。
- **N2 todo 快照安全读取**：session_id 严格白名单正则（防路径穿越，`_SAFE_SESSION_ID`）；
  任何读取 / 解析失败**静默返回 `[]`**（fail-open，快照是增强非关键路径）；cancelled
  项剔除；状态新旧别名归一（running → in_progress 等）；串行遮罩
  `overlay_serial_todo_statuses` 把乱序的已完成 / 进行中行重置为 pending（UI 单线推进
  观感契约）。
- **N3 后台清理尽力而为**：保留期默认 30 天（`CLEANUP_PERIOD_DAYS`）；首次延迟 10min、
  之后每 24h 一轮；按目录 mtime 判过期；阻塞操作丢线程池且单步失败不中断循环；
  rmtree `ignore_errors=True`（可重复执行幂等）。装配点：`app_gateway` 启动。
- **N4 cron 收轮三信号路径**：只有 **leader 的非占位 `chat.final`** 才算完成信号
  （三个结束路径：workflow 完成后 final / team.completed + 有文本 / 纯 harness 收尾）；
  **leader 提前 final 后若出现 `task.created` 会清除 final 锁存继续等待**；占位文案
  （"最终报告即将生成"等，`CRON_LEADER_PLACEHOLDER_MARKERS`）不算结果；单机模式 2s
  宽限期排水；`chat.error` 直接收尾并清空任务表。
- **N5 git 提示不代执行**：`git_safe_directory` 检测 dubious ownership（returncode != 0
  且输出含特征串）只给建议命令，**不代执行 `git config`**；路径规范化
  `expanduser().resolve().as_posix()`。
- **N6 版本唯一真源**：`version.__version__` 全仓唯一（UA / 握手 / 升级判定 / ACP
  initialize 全引用）。
- **N7 pre-release 总序**：`release_sort_key` —— 基版本数值比较优先 → 同基版本 stable
  恒高于 pre-release → pre 类型序 dev < alpha < beta < rc < pre → 同类型比序号。多源
  优先抓 releases 列表（含 pre-release、跳过 draft），失败回退 `/latest`（仅稳定版）；
  版本号从 tag / name / 资产文件名多候选取最大。
- **N8 安装模式二分**：`sys.frozen` 或 `JIUWENSWARM_DESKTOP=1` → desktop，否则 pip；
  **pip 模式强制走 PyPI 源**（desktop 才用 gitcode / github）。desktop 只下载不安装。
- **N9 pip 升级前置拒绝**：editable 安装直接拒绝（提示 git pull）；仅 uv 管理的 venv
  （pyvenv.cfg 含 uv 标记，排除 conda / 系统 Python）才用 uv，否则
  `pip install --upgrade`；状态更新全程持 `threading.Lock`。
- **N10 重启守卫一次性**：`updater_restart_helper` 等父进程退出（60s 超时）再动作；
  web 侧等 gateway 端口连上（30s）；失败即 `sys.exit(1)` 不重试；restart json 是
  一次性标记（成功即删）。无 Python 导入方（`python -m` 运行），消费 `web_process.json`。
- **N11 asset 平台精确匹配**：win = exe / mac = dmg / linux = tar.gz 精确名匹配；pip
  需 .whl 资产；token 掩码显示；下载头带 `PRIVATE-TOKEN`（从 updater 延迟导入避免
  循环依赖）。

## 接口契约

| 模块 | 关键符号 | 语义要点 |
|---|---|---|
| `todo_generation.py` | `todo_item_generation_token` / `filter_todos_by_generation` | N1 |
| `todo_snapshot.py` | `load_todo_snapshot_for_frontend` / `format_todos_for_frontend` / `overlay_serial_todo_statuses` / `_SAFE_SESSION_ID` | N2 |
| `cleanup.py` | `start_background_cleanup` / `cleanup_loop` / `cleanup_old_sessions` / `cleanup_orphan_file_ops` | N3 |
| `cron_team_completion.py` | `new_cron_team_round_state` / `apply_cron_team_round_event` / `cron_team_round_should_end` / `is_cron_leader_placeholder_text` / `CRON_LEADER_PLACEHOLDER_MARKERS` | N4 |
| `git_safe_directory.py` | `is_dubious_ownership_error` / `safe_directory_value` / `safe_directory_hint` | N5 |
| `updater.py` | `UpdaterService` / `UpdateStatus` / `get_access_token` / `DEFAULT_SOURCE_CONFIG` / `_is_newer_version` | N8 / N9；`start_upgrade()` 仅 pip 模式，3s 后对本进程 SIGTERM |
| `updater_restart_helper.py` | `main()` / `--restart-json` / `--parent-pid` | N10 |
| `upgrade_executor.py` | `UpgradeExecutor`(ABC) / `DesktopExecutor` / `PipExecutor` / `create_executor` / `DOWNLOAD_CHUNK_SIZE` | N8–N11 |
| `version.py` | `__version__ = "0.2.3"` | N6 |
| `version_source.py` | `VersionSource`(ABC) / `GitHubReleasesSource` / `GitCodeReleasesSource` / `PyPIVersionSource` / `ReleaseInfo` / `ReleaseAsset` / `release_sort_key` / `is_prerelease_version` / `strip_prerelease_suffix` | N7；PyPI simple JSON API 失败解析 HTML 兜底 |

## 消费者

- todo_snapshot：`server/handlers/session.py`（历史恢复先发快照）。
- cron_team_completion：`gateway/cron/scheduler.py`、`team_helpers.py`、`team_manager.py`。
- cleanup：`app_gateway`。
- updater 全链：`app_gateway`、`app_web_handlers`（约 8 处 RPC handler）。
- version：`updater` / `upgrade_executor` / `version_source` / `e2a/acp/protocol` /
  `app_web_handlers` / `tui_connect` / `agent_ws_server` / `commands`。

## 修改检查单

- 改 todo 状态别名 / 串行遮罩 → 前端恢复观感回归（`test_todo_snapshot.py` +
  `test_history_todo_restore.py`）+ N1 兼容（旧快照无 token）。
- 改收轮状态机 → N4 三路径 + final 锁存清除 + 占位文案表全量回归
  （`test_cron_scheduler.py` + `test_cron_team_completion.py`）。
- 改版本比较 → `release_sort_key` 总序全序矩阵回归（stable vs 各 pre 类型 vs 序号）；
  禁用宽松的字符串比较。
- 改升级流程 → desktop / pip 双模式 + editable 拒绝 + uv/pip 分流 + 重启标记一次性
  回归（`test_updater_install_mode.py`）；**先在测试环境验证 restart_helper 的端口
  等待链**。
- 改清理保留期 / 节奏 → 与 [[S_07]] 工作区配额的磁盘观测联动评估。

## 与其它 spec 的关系

- [[S_02]]：version 被 ACP initialize 消费（能力自述里的版本上报）。
- [[S_07]]：cleanup 的目录清理不经 zone 删除链（边界见其范围段）。
- [[S_01]]：cleanup 装配点登记（app_gateway）。
