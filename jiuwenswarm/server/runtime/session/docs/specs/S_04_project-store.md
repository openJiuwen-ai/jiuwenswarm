# S_04 · 项目注册表（projects.json）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_04 / project-store |
| 关联模块 | `jiuwenswarm/server/runtime/session/project_store.py`（1,121 行，纯模块级函数，无注册类） |
| 最近一次修订日期 | 2026-10-22 |
| 关联 feature | 暂无 |

## 范围 / 边界

### 本规约管什么

- 项目注册表 `~/.jiuwenswarm/agent/projects.json`（**复数文件名**，格式
  `{"version": 1, "projects": [...]}`）的完整读写契约：跨进程文件锁、
  原子写、读容错、进程内缓存。
- 项目的 CRUD 与冲突协议（新建 / 恢复 / 隐藏 / 重命名 / 置顶重编号）。
- `(work_mode, project_dir)` / `(work_mode, name)` 的 mode-aware 查询与
  跨模式项目隔离。
- `session.create` 的 project_id / project_dir 绑定解析
  （`resolve_session_project_binding`）。
- cron 侧的 project 归属解析（`resolve_cron_*` 族）与 job patch 重解析
  （`resolve_cron_job_patch`，Web RPC / AgentTool 双链路共用）。
- TUI 前置归属解析（`find_or_create_code_project_for_dir` 族）。

### 本规约不管什么

| 子主题 | 归属 |
|---|---|
| work_mode 参数归一化与通道默认（本模块消费其结果） | [[S_01]]（work_mode.py） |
| `Project.git` 快照字段的内容维护（store 只存储不维护） | [[S_05]] |
| cron job 本体（调度 / 存储）；本模块只提供归属解析 helper | cron 子系统 |
| default / default_code 项目 id 常量与判定 | `common` 层 work_mode 常量 |

### 关键背景：两个「项目」命名空间

- **默认项目不入库**：`default` / `default_code` 是保留 id，不写
  projects.json（`get_project_by_id` docstring :291——默认项目不会命中）。
- **同一 project_dir 在 code / work 两模式下是两个独立项目**
  （不同 project_id）；查询、冲突检测、默认工作目录均按 mode 隔离。

## 不变量

- **N1 存储形态与 ID 生成**：`projects.json` 为 `{"version": 1,
  "projects": [记录 dict 列表]}`；`project_id` 格式 `proj_` + 8 位 hex
  （`secrets.token_hex(4)`，32 位熵，:174-176），**在文件锁内查重 + 重生成**
  （:709-718，万无一失策略）。
- **N2 跨进程文件锁与数据文件分离**：锁文件为 `<data_path>.lock`
  （:91-94）——数据文件的原子替换不会破坏锁。POSIX 用
  `fcntl.flock(LOCK_EX | LOCK_NB)` + 50ms 间隔重试，10s 超时抛 OSError
  并保留 `BlockingIOError` 调用栈（:71-81）；Windows 用 `msvcrt.locking
  LK_LOCK`（自带 ~10s 重试，:57-59）；Windows 锁文件需至少 1 字节才能锁
  （:98-100）。
- **N3 原子写 + 断电耐久**：`_write_disk_locked` 写 `.tmp` → flush →
  `fsync`（文件）→ `os.replace` → `_fsync_dir`（父目录目录项持久化，
  :216-226）。Windows 无法对目录 fsync，跳过（:202-203）。
- **N4 读容错——损坏即空表**：文件缺失 / JSON 损坏 / 结构类型不符一律
  返回空列表（:180-192），**不抛错**。权衡：单次写入即整体覆盖空表
  （接受），换取损坏文件不阻塞启动。
- **N5 `_mutate` 是唯一变更入口**：所有写操作走「文件锁内**重读磁盘** →
  应用变更 fn → 原子写回 → 刷新缓存」（:232-245）。锁内重读保证拿到其他
  进程（Gateway / AgentServer）的最新写入，**防止基于陈旧缓存变更丢失
  更新**。新增写操作不得绕过此入口直接改 `_CACHE` 或直接写文件。
- **N6 缓存与惰性迁移**：`_CACHE`（list | None）+ `_CACHE_LOCK`；
  `cache_bust=True` 强制读盘（跨进程同步场景）。`_load_cache` 首读时为缺 /
  非法 work_mode 的老项目**推断并写回磁盘**（:258-279，保证磁盘 schema
  统一；写回失败仅 warning 不阻塞读取）；`git` 字段缺省 `{}` 属纯常量默认，
  由 `from_dict` 兜底，**不写回**（:261）。
- **N7 mode-aware 隔离与路径规范化**：路径匹配一律
  `os.path.normcase(os.path.normpath())`（容忍尾部分隔符 / 大小写差异，
  :317-322）；work_mode 匹配经 `_wm` 统一兜底（旧记录缺字段不漏匹配，
  :334-340）。`get_project_by_dir`（不带 mode）已 **deprecated**——跨模式
  隔离下会命中其他模式的项目，仅保留给 legacy / 诊断路径（:305-309）。
  注意：store 内部对 work_mode **非法值兜底为 "work"**，与公开入口的严格
  校验（[[S_01]] N8）是两层不同纪律（:325-331 docstring）。
- **N8 session 绑定四规则**（`resolve_session_project_binding`，:404-462）：
  project_dir 非空必须绝对路径；两者皆空 / default id → 默认项目；仅传
  project_dir 无真实 project_id → **BAD_REQUEST 拒绝**（会话不得有路径却
  无归属）；真实 id 必须存在且可见（否则 NOT_FOUND）；同传 id + dir 时按
  规范化路径校验一致性，不一致 BAD_REQUEST。默认 id 判定含 `default_code`
  （:430-432）。
- **N9 create_or_restore 原子三分支（TOCTOU 关闭）**：单次 `_mutate` 内
  完成（:822-905）——命中同 mode 隐藏项目 → 恢复（`hidden=False` + 更新
  name）；命中同 mode 可见项目 → `ProjectDirConflict`；无匹配 → 新建。
  名称唯一性与**同 mode 其他项目（含隐藏项目）**比较，隐藏项目名称同样
  保留（防隐藏期间被复用、恢复后重名）。空 project_dir 允许多个（靠
  id + name + mode 区分，会话按 project_id 归属）。`create_project`
  （不去重版）仅供内部 / 测试，生产路径必须走本函数。
- **N10 隐藏的联动语义**：`hide_project` 软删除（hidden 翻转）并**自动取消
  置顶**（`pinned=False, pin_order=0`，:1015-1019）——隐藏项目不得出现在
  置顶区。`rename_project` / `restore_project` 均在锁内做同 mode 名称冲突
  检测；项目不存在 / 状态未变返回 None（调用方通常已预检）。
- **N11 项目名可作目录名校验**：`validate_project_dir_name`（:731-756）拒绝
  文件系统非法字符 `<>:"/\|?*`（全平台一致）、Windows 保留设备名
  （CON/PRN/AUX/NUL/COM1-9/LPT1-9）、全点 / 全空格、超 200 字符；**不做任何
  转换**（用户选「拒绝创建」策略）；中文 / 空格 / Unicode 允许。默认工作区
  按 mode 分目录：`jiuwenclaw_workspace/{code|work}/{name}`（:759-784），
  避免同名 code / work 项目默认路径互撞；本函数只返回路径，mkdir 由调用方负责。
- **N12 TUI 前置归属**：TUI 请求只携带 project_dir / cwd，而 N8 规则 3 拒绝
  「仅 dir 无 id」——`find_or_create_code_project_for_dir`（:1052-1111）
  先固定 `work_mode="code"` 解析或**创建** code 项目，再把真实 project_id
  注入 session.create。同名不同目录冲突（ProjectNameConflict）用路径
  sha1 前 6 位后缀**重试一次**。git 联动：新建 → `ensure_on_project_create`
  （auto init）；恢复 → 仅轻量 probe 刷新快照**不自动 init**（用户可能有意
  删除 .git，:1095-1105）。
- **N13 cron patch 重解析双链路一致**：`resolve_cron_job_patch`
  （:596-681）被 `CronController.update_job`（Web RPC）与
  `cron_tools.py update_job`（AgentTool）**共用**——修改规则必须两链路同步
  验证。规则：**拒绝单独 patch work_mode**（无法确定项目归属，显式拒绝
  而非静默忽略）；patch 含 project_dir 时 **project_id 必须由解析产生**
  （pop 掉直接传入值），并从 Project 记录注入 work_mode。

## 接口契约

### 查询族（全部返回 Project 或 None，mode-aware 者按 mode 隔离）

| 接口 | 契约要点 | 代表调用方 |
|---|---|---|
| `get_project_by_id`（:288） | 默认项目不入库不会命中；`cache_bust` 跨进程 | 绑定解析 / cron 解析内部 |
| `get_project_by_dir_and_mode`（:343） | `(mode, 规范化路径)` 匹配，不限 hidden | TUI 归属 / 冲突检测 |
| `get_project_by_name_and_mode`（:378） | `(mode, name)` 匹配，名称冲突检测 | create / rename |
| `get_project_by_dir`（:298） | **deprecated**，见 N7 | legacy / 诊断 |
| `list_projects`（:465） | `include_hidden=False` 默认排除软删除 | project.list / 统计 |
| `get_project_dir_by_id`（:684） | 含隐藏查；无命中返回 `""`（默认项目） | cron 调度构造执行请求 |

### 绑定 / 归属解析族

| 接口 | 契约要点 | 代表调用方 |
|---|---|---|
| `resolve_session_project_binding`（:404） | N8 四规则；返回 `(pid, dir, error, code)` | work_mode.py 归一化链（[[S_01]]） |
| `find_or_create_code_project_for_dir`（:1052） | N12；有副作用（可创建项目 + git 联动） | bootstrap.py（TUI 入口） |
| `find_or_create_code_project_for_tui_params`（:1114） | 取 `project_dir` / `cwd` 候选 | TUI 参数入口 |
| `resolve_cron_project_id`（:477） | 空 dir→`""`；相对路径→ValueError；命中**同 mode 可见**才返回 | cron 侧 |
| `resolve_cron_project_binding`（:518） | 返回 frozen `CronProjectBinding`（pid/work_mode/error/code/hidden）；default id 直接映射；真实 id 注入存储的 work_mode | cron 控制器 / cron 工具 |
| `resolve_cron_job_patch`（:613 契约） | N13；原地修改 patch 并返回 | controller.py:329 链 / cron_tools.py |

### CRUD 族（全部走 `_mutate`，异常语义见 N9-N10）

| 接口 | 契约要点 | 代表调用方 |
|---|---|---|
| `create_or_restore_project`（:822） | 返回 `(Project, restored: bool)` | app_web_handlers.py（project.create） |
| `save_project`（:908） | upsert（命中替换 / 未命中追加），刷 updated_at | git 快照回写链（[[S_05]]） |
| `rename_project`（:926） | 锁内名称冲突检测；不存在返回 None | app_web_handlers.py |
| `hide_project`（:999） | 软删除 + 自动取消置顶 | app_web_handlers.py |
| `restore_project`（:963） | 锁内名称冲突检测；已可见返回 None | app_web_handlers.py |
| `reindex_project_pin_orders`（:1026） | 置顶项目紧凑重编号 1..N | 置顶变更后 |
| `validate_project_dir_name` / `resolve_default_project_dir` | N11 | 创建入口 |
| `invalidate_cache`（:1045） | 测试 / 特殊场景清缓存 | 测试 |

## 数据结构

### Project（dataclass，:110-149）

| 字段 | 语义 |
|---|---|
| `project_id` | `proj_` + 8 hex（N1） |
| `name` | 展示名；可作目录名（N11 校验）；同 mode 内唯一（含隐藏） |
| `project_dir` | 绝对路径；可为空（空路径项目）；同 mode 内唯一 |
| `work_mode` | `code` / `work`；旧数据缺失经 `infer_legacy_project_work_mode` 兜底 |
| `pinned` / `pin_order` | 项目级置顶（与 [[S_03]] 会话级 pin 是平行协议） |
| `hidden` | 软删除标记 |
| `created_at` / `updated_at` | UTC 时间戳 |
| `git` | **快照字段**：enabled / repo_root / initialized_by_jiuwenswarm / detected_at / branch / status / error / is_dirty——由 ProjectGitService 写入，store 只存储不维护（:124-126） |

### 存储

- `projects.json`：`{"version": 1, "projects": [...]}`（`get_agent_root_dir()`
  下的全局单文件）。
- `projects.json.lock`：跨进程锁文件（N2）。
- `_CACHE`：进程内 list 缓存；仅 `_mutate` / `_load_cache` 维护。

## 执行路径

- **P1 变更**：CRUD → `_mutate` → 文件锁 → 重读磁盘 → fn 变更 → tmp+fsync+
  replace+fsync_dir → 刷 `_CACHE`。
- **P2 读取**：`_load_cache` → 缓存命中直接返回；miss / bust → 文件锁内读
  → 惰性迁移（work_mode 写回，N6）→ 刷缓存 → 返回。
- **P3 session.create 绑定**：handlers → work_mode 归一化（[[S_01]]）→
  `resolve_session_project_binding`（N8）。
- **P4 project.create**：app_web_handlers → `validate_project_dir_name` →
  （未指定 dir 时 `resolve_default_project_dir` + mkdir）→
  `create_or_restore_project`（N9）→ 新建时 git 联动（[[S_05]]）。
- **P5 cron 更新**：CronController.update_job / cron_tools.update_job →
  `resolve_cron_job_patch`（N13）→ 落库。

## 修改检查单

修改本模块前自查：

1. 新增字段：确认 `from_dict` 兜底 + 是否需要惰性迁移写回（纯常量默认不
   写回，推断值才写回，N6 对比）。
2. 新增写操作：必须走 `_mutate`（N5）；不得直接改 `_CACHE` 或绕锁写文件。
3. 触碰查询：新代码一律 mode-aware（dir_and_mode / name_and_mode），
   不使用 deprecated `get_project_by_dir`。
4. 触碰冲突协议：同 mode 比较含隐藏项目（名称）；恢复 / 重命名的冲突
   检测不可移出锁（TOCTOU）。
5. 触碰 `resolve_cron_job_patch`：双链路（controller.py / cron_tools.py）
   回归测试必须同时过。
6. 触碰默认项目判定：核对 `default` / `default_code` 两常量与
   `is_default_project_id` 全部入口一致。
7. 触碰 git 字段：store 只存储不维护（N；git 快照语义归 [[S_05]]）。
8. 触碰锁实现：Windows（msvcrt / 1 字节）与 POSIX（flock / 10s 超时）双端
   行为核对。

## 与其它 spec 的关系

- [[S_01]]：`resolve_session_work_mode_params` 链内调用本模块
  `resolve_session_project_binding` 完成绑定校验；TUI 前置归属
  （N12）是其 code 模式默认的补全环节。
- [[S_03]]：会话 metadata 持久化 project_id / project_dir（first-write-wins）；
  项目隐藏后 metadata 残留属预期（会话归默认项目统计）。
- [[S_05]]：`Project.git` 快照字段的唯一写方是 ProjectGitService
  （经 `save_project` 落库）；`find_or_create_code_project_for_dir` 的
  新建 / 恢复 git 联动是其两个触发点。
- [[S_06]]：diff 展示按项目（mode 隔离）取 git 状态。
