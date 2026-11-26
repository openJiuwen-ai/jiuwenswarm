---
id: B_20261119_1416_builtin-extension-discovery
title: 本地默认启动未加载 Swarm 内置扩展导致 app_market RPC 不可用
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/common
  - jiuwenswarm/channels/desktop
  - jiuwenswarm/extensions/app_market
files:
  - jiuwenswarm/app.py
  - jiuwenswarm/common/config.py
  - jiuwenswarm/common/utils.py
  - jiuwenswarm/channels/desktop/desktop_app.py
  - jiuwenswarm/channels/desktop/electron/main.cjs
  - jiuwenswarm/channels/desktop/gateway_entry.py
  - scripts/jiuwenswarm_exe_entry.py
  - tests/electron/startup_shutdown.test.cjs
  - jiuwenswarm/resources/config.yaml
  - jiuwenswarm/resources/.env.template
  - tests/unit_tests/test_builtin_extension_dirs.py
  - tests/unit_tests/test_config.py
designs: []
design_note: 本次为本地启动装配缺陷修复，依据现有应用市场扩展契约与 Gateway 扩展加载实现，无独立设计。
---

# 本地默认启动未加载 Swarm 内置扩展导致 app_market RPC 不可用

## 现象与影响

本地按默认方式启动 Swarm 后，所有 `app_market.*` RPC（包括旧的 `app_market.list`）返回 `METHOD_NOT_FOUND`，应用市场扩展完全不可用；Hub 安装链路因此无法从 UI/WS 触发。受影响的是 `jiuwenswarm-start` 默认启动与桌面启动器路径；Docker、K8s、元绒部署脚本因显式追加内置扩展目录而不受影响。

## 关联设计与当前契约

依据 [S_03_app-hub-distribution](../../../specs/jiuwenswarm/extensions/app_market/S_03_app-hub-distribution.md) 与既有应用市场扩展契约：`app_market` 由 agent-gateway 的 ApplicationPlugin 扩展宿主加载，加载后通过 `bind_web_channel` 注册 `app_market.*` 方法。

## 根因与修复决策

链路：

1. `jiuwenswarm-init` 将模板配置写到 `<数据根>/config/config.yaml`；
2. Gateway 读取 `<数据根>/infra/config/config.yaml`，缺失时回退到 `agent_gateway/resources/config.yaml`；
3. 该 bundled 配置为 `extension_dirs: ${EXTENSION_DIRS:-agent_gateway/extensions}`，而 `.env.template` 中 `EXTENSION_DIRS=""`；
4. 空值按未设置处理，Gateway 只扫描自身包内扩展，不扫描安装包的 `jiuwenswarm/extensions`，`app_market` 未被发现。

修复选择：本地启动器在派生子进程时调用 `augment_extension_dirs()`，把包内 `jiuwenswarm/extensions` 追加到 `EXTENSION_DIRS`；同时让 `load_dotenv_runtime` 保留启动器注入的值，避免用户 `.env` 中的空值再次覆盖。没有修改 Gateway 默认配置，也没有要求用户手工设置环境变量。

## 回归证据与适用范围

- 新增 `tests/unit_tests/test_builtin_extension_dirs.py`：空值注入、自定义目录追加、幂等、空 `.env` 不覆盖启动器注入。
- 相关启动测试：`pytest tests/test_start_services_proxy.py tests/unit_tests/test_start_services_ready_hint.py tests/unit_tests/test_builtin_extension_dirs.py -q -o addopts=""` → 20 passed。
- 真实默认启动（未设置 `EXTENSION_DIRS`，用户 `.env` 保持 `EXTENSION_DIRS=""`）后，日志显示 `app_market` 被加载，`app_market.list` 经 WebSocket 返回 `ok=True`。
- 未覆盖：桌面安装包的完整启动链、用户显式设置多个扩展目录时的优先级（当前为追加内置目录）。

## 2026-11-20 复查与修订

Gateway 在自身进程中重新加载 dotenv，Swarm 侧保护启动器环境变量的做法无法覆盖这个加载入口。Gateway 既未指定配置目录、默认配置文件也不存在时，Swarm 本地启动改用实例配置。模板始终列出包内扩展目录，并保持自定义目录优先。配置迁移仅替换已知的旧本地默认表达式，并延续对空配置节和分布式配置的既有处理。当前契约见 [S_03_app-hub-distribution](../../../specs/jiuwenswarm/extensions/app_market/S_03_app-hub-distribution.md)。

- Windows `.venv` 的 `test_builtin_extension_dirs.py` 5 项通过，覆盖空值、自定义目录、显式 Gateway 配置、已有独立配置与 AgentOS。
- 使用锁定的 Gateway `d68e80bf` 源码，在空目录和自定义目录两种情况下验证旧配置迁移、重复迁移无额外改动、真实扩展发现和注册，并通过测试通道调用 `app_market.list`，均返回 `ok=True`。
- `test_config.py` 在本地虚拟环境收集时缺少 `openjiuwen.symphony.SymphonyConfig`，旧默认迁移由上面的锁定 Gateway 验证覆盖。未执行桌面原生窗口和完整 WebSocket 服务验收。

## 已有 Gateway 默认配置的升级

源码启动时，旧工作区可能已经存在 `<数据根>/infra/config/config.yaml`，使 Gateway 继续扫描自身的扩展目录。Swarm 托管启动现在也检查这份有效配置。仅当目录值仍为旧的 `${EXTENSION_DIRS:-agent_gateway/extensions}` 时，在 Gateway 配置写锁内追加 `jiuwenswarm/extensions`。本次迁移只调整这个字段，继续使用同一份 Gateway 配置，显式指定的配置、自定义目录以及 AgentOS 部署沿用原设置。

Windows 项目 `.venv` 定向运行 `test_builtin_extension_dirs.py`，6 项通过。回归用例覆盖已有 Gateway 默认配置的迁移、重复启动和显式配置保留。迁移使用临时工作区验证。

## Electron 启动入口补齐

Electron 开发版原先直接运行 `agent_gateway.app_gateway`，打包版的 `--desktop-run-gateway` 也直接导入 Gateway。两个入口现统一经 Swarm 的桌面 Gateway 入口，在导入 Gateway 前调用 `prepare_local_gateway_config`。

- Windows 项目 `.venv` 运行 `test_builtin_extension_dirs.py`，8 项通过。Electron 入口选择测试通过。
- 通过开发版 Python 模块入口与打包版参数分发入口，在隔离工作区验证新配置和旧默认配置。锁定的 Gateway `d68e80bf` 均发现 App Market 并绑定 17 个方法，`app_market.list` 返回成功。自定义扩展目录仍按用户配置加载。
- Vite 生产前端构建通过，产物包含 `app_market.convert_to_local`。未启动完整 Electron 安装包。

## 2026-11-26 的 0.2.10 全新初始化回归

全新初始化后，Gateway 加载了 0 个扩展，`app_market.list` 返回 `unknown method`。初始化从 Gateway 模板生成的 `infra/config/config.yaml` 只含 Gateway 默认扩展路径。启动准备检查了 `config/config.yaml`，而启动参数让 Gateway 读取 `infra/config/config.yaml`。

本地初始化生成的 Gateway 默认配置现在包含 `jiuwenswarm/extensions`。启动准备选定同一份配置，幂等补齐旧默认路径，并将配置目录传给 Gateway 子进程。

Windows 上 12 项定向测试通过，覆盖配置生成、两个配置文件并存、显式配置和启动传参。通过 `jiuwenswarm-init` 创建隔离数据根并启动 Gateway，实际调用 `/ws` 上的 `app_market.list` 成功返回空列表。验证范围为市场方法注册。

同一修复移植至 develop 时，通过 `common/paths.py` 选定 `config/config-gateway.yaml`。以上运行结果来自 0.2.10，develop 移植仅核对了提交差异。
