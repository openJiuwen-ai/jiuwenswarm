# Windows 下 openjiuwen_runtime 在导入期创建「安装盘根\tmp」，盘根不可写时 AgentServer 启动即退出

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-23 |
| 严重程度 | 高（OfficeAce 1.8.1：装在 D 盘等数据盘且盘根禁止普通用户建目录时，智能体后端 100% 无法启动；11-03 日志已有同型崩溃） |
| 范围 | `jiuwenswarm/runtime_work_dirs.py`（新增）、`jiuwenswarm/__init__.py`、`jiuwenswarm/dotenv_early.py`、`tests/unit_tests/test_runtime_work_dirs.py`、本文档；上游根治（agent-runtime `foundation/config.py`）与桌面端提示文案（relay-claw）随各自仓库处理 |
| 测试基线 | `tests/unit_tests/test_runtime_work_dirs.py` 13 passed（新增，含 Windows 子进程端到端与 .env 覆盖校正）；`test_dotenv_decode_guard.py` + `test_desktop_port_resolve.py` + `agentserver/test_flash_env_routing.py` 与下列用例合计 183 passed；`test_app_agentserver.py` + `test_config.py` + `test_utils.py` + `test_instance_manager.py` + `test_start_services_ready_hint.py` 245 passed |
| 关联 spec | 无（不改变任何模块契约；仅在包导入期为未设置的 `DEPLOY_DIR` / `DIST_DIR` 补缺省值） |

## 现象

用户日志（1.8.1，安装根 `D:\HuaweiMoveData\...\OfficeAce`）：AgentServer 进程在
`[debug_dump] faulthandler enabled` 之后约 100ms 退出，`full.log` 中 35 次
`[AgentServer] atexit reason=PermissionError: [WinError 5] 拒绝访问。: 'D:\\tmp'`，
relay-claw 侧 49 次 `OA.AGENT.PROCESS_EXITED`，最终 `service_unavailable`，界面只显示笼统的
「后端启动失败」。

## 根因

导入链（本机用 meta_path 钩子复现，与用户 stderr 栈一致）：

```
app_agentserver.main → _run_with_telemetry (app_agentserver.py:419)
  → jiuwenswarm.common.security.link_mtls:20
  → openjiuwen_runtime.foundation.security → models → db/__init__ → mysql_handler
  → openjiuwen_runtime.foundation.config: settings = Settings()
  → resolve_and_create_paths(): deploy_path.mkdir(parents=True)
```

`Settings` 的默认值是 POSIX 路径 `DEPLOY_DIR=/tmp/deploys`、`DIST_DIR=/tmp/dist`。在 Windows
上 `Path('/tmp/deploys').is_absolute()` 为 False（没有盘符），于是被拼到
`PROJECT_ROOT = config.py.parents[3].resolve()`（即 `<python>\Lib`）上；由于 `/tmp` 以根开头，
pathlib 只保留左侧盘符，结果是 `<安装盘>:\tmp\deploys`。`mkdir(parents=True)` 先创建
`D:\tmp`，盘根 ACL 拒绝即抛 WinError 5；该异常发生在模块级单例构造中，没有任何捕获，进程直接退出。

所以问题单中的「D:\tmp 写死」并不准确：两个仓库都没有这个字面量，`D:` 来自安装盘。装在 C 盘时得到
`C:\tmp`，普通用户通常可以创建，问题被掩盖。

## 修复

- 新增零依赖模块 `jiuwenswarm/runtime_work_dirs.py`：**仅在 Windows、且 `DEPLOY_DIR` /
  `DIST_DIR` 未设置时**，按顺序选第一个能创建成功的根：`%TEMP%\openjiuwen-runtime`，再退到
  `<JIUWENSWARM_DATA_DIR | JIUWENCLAW_DATA_DIR | ~/.jiuwenswarm>\.runtime-tmp`，然后写入
  `os.environ`。所有候选都失败时，只记录中文可操作指引（`jiuwenswarm.early` logger 输出到 stderr），
  不改环境，也不抛异常。
- `jiuwenswarm/__init__.py` 在包导入时调用它。之所以不放在 `dotenv_early`，是因为
  desktop / init / skill-train 等入口不导入 `dotenv_early`；而任何 `jiuwenswarm.*` 导入都会先执行包
  `__init__`，这是唯一能保证早于 `openjiuwen_runtime` 首次导入的位置。
- 优先级保持不变：父进程显式下发的 Windows 绝对路径（如 `D:\data\deploys`）不会被覆盖；
  之后由 `--dotenv` / 用户 `.env` 以 `override=True` 载入的合法值也仍然生效，因为 runtime 直到第一次被导入时
  才读取环境变量。
- 「已设置但同样会落到盘根」的值按未设置处理：Windows 上有根无盘符（`/tmp/deploys`、`\tmp`）或
  盘符相对（`C:tmp`）的值与默认值同样出错（例如按 Linux 文档写进 `.env`、多平台共用一份 env），
  因此同样改用候选根，并输出一条 warning 写明原值和新值。纯相对路径（`deploys`）是 runtime
  约定的「相对项目根」，不在此列，保留原样。
- `load_dotenv_runtime()` 载入 `.env` 后再调用一次兜底：包导入期之后由 `.env` 覆盖进来的空值或
  `/tmp/...` 也会被校正。其它直接调用 `load_dotenv` 的零散入口不在此覆盖范围内，
  需等 runtime 上游根治。
- POSIX（Linux 容器、macOS）上不做任何事：`/tmp/...` 在这些平台本就是合法的绝对路径，
  企业部署的行为不变。

## 拒绝的方案

- **在 jiuwenswarm 里 monkeypatch `Settings` 或捕获 `PermissionError`**：异常发生在第三方模块级单例构造期，
  只能抢在导入之前修正输入，事后无法补救。
- **只在 `app_agentserver` 的 link_mtls 导入前设置**：gateway（`web_http_server`）、企业扩展和数据库层
  也会导入 runtime，单点修补容易漏掉。
- **把目录放在安装目录下**：安装目录同样可能不可写（Program Files / OneDrive 同步目录）。

## 跨仓遗留（不在本仓修复）

- agent-runtime `foundation/config.py`（根治）：默认值改为 `tempfile.gettempdir()` 下的目录；
  导入期的 `mkdir` 失败应降级为告警，而不是让进程退出。
- relay-claw `buildSidecarSpawnEnv()`：桌面端也应无条件下发三个目录；启动失败时识别
  `PermissionError` + 运行时目录，给出中文可操作提示，替换现在笼统的「后端启动失败」。

## 复现与验证

- 路径计算：本机 venv 中 `PROJECT_ROOT=D:\project\jiuwenswarm\.venv\Lib`，`/tmp/deploys` → `D:\tmp\deploys`；
  代入用户安装根同样得到 `D:\tmp\deploys`。
- 盘根拒绝：`subst` 一个盘符并对其根目录 deny `AD`，`mkdir(parents=True)` 得到
  `PermissionError [WinError 5]: 'R:\\tmp'`，与用户报错一致。注意 runtime 会对 `PROJECT_ROOT` 调用
  `resolve()`，subst 盘符会被还原为真实盘符，所以不能用 subst 直接端到端复现 Settings 的行为。
- 修复后：子进程先 `import jiuwenswarm` 再导入 runtime，`settings.DEPLOY_DIR` / `DIST_DIR` 落在
  `%TEMP%\openjiuwen-runtime\` 下，不再落到盘根（见单测 `test_runtime_settings_no_longer_resolve_to_drive_root`）。
- 用户临时规避（无需新版本）：在系统环境变量中把 `DEPLOY_DIR` / `DIST_DIR` 设为用户可写的绝对路径后重启 OfficeAce。
