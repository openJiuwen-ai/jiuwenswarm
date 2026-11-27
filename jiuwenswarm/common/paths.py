# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""

JiuWenSwarm 路径管理。

数据根 ``JIUWENSWARM_DATA_DIR``（默认 ``~/.jiuwenswarm``）按数据所有权分层：
用户业务数据收敛到 ``profile/``（统一用户数据目录，跨 agent server 共享），
程序系统数据保持数据根原位（不共享、不迁移、不建软链）。

背景：企业版 agent server 作为资源池弹性伸缩，不与用户/agent 强绑定，不同
agent server 可能服务同一 agent 实例。原布局里业务数据与系统数据混居数据根：
config/ 内部"业务配置不该共享、会话运行态必须共享"的冲突无解；run/、runtime/
等锁与缓存目录多 Pod 共享实测异常；每新增一类数据都要重新评估共享与挂载。

划分依据是数据所有权，与部署形态无关：用户业务数据进 profile（企业版挂共享卷
PVC/NFS，跨 Pod 存活），系统数据留在数据根原位（企业版不挂私有卷：数据根
本身为 Pod 本地存储，仅 profile/ 经共享卷挂载，系统态随 Pod 生命周期）；
个人版两者均为普通子目录，代码路径一致、无开关。

数据根完整布局（真实目录/文件，迁移后的规范形态）：

    ├── profile/                        【通用】 统一用户数据目录：用户直接产生、需要留存
    │   ├── agent/                      【个人版专属】 agent 根：会话、检查点、任务、团队库
    │   │   ├── sessions/                       会话轨迹（含 sessions_archived/ 归档）
    │   │   ├── home/                           定时与心跳任务（懒创建）
    │   │   ├── teams/                          团队控制库
    │   │   ├── .checkpoint/                    会话映射检查点
    │   │   └── workspace/                      DeepAgent 工作空间（企业版名 jiuwenclaw_workspace）
    │   │       ├── AGENT.md                    人设
    │   │       ├── SOUL.md                     灵魂
    │   │       ├── IDENTITY.md                 身份
    │   │       ├── USER.md                     用户
    │   │       ├── skills/                     技能库（skills_state.json）
    │   │       ├── memory/                     长期记忆
    │   │       ├── projects/                   项目工作区
    │   │       ├── todo/                       待办
    │   │       ├── messages/                   消息
    │   │       ├── agents/                     子代理
    │   │       ├── extensions/                 rail 扩展（rail_manager 按 manifest 加载的运行面扩展）
    │   │       ├── interactions/               待交互上下文
    │   │       ├── mcp/                        MCP：credentials/ + mcp_builtins/
    │   │       └── plugins/                    agent_templates/ 与 plugin_packages/
    │   │           └── {built_in,local}/   内置/本地两个来源桶
    │   ├── .agent_teams/               【个人版专属】 团队名册/任务/工作区
    │   ├── .agent_history/             【个人版专属】 工具历史
    │   ├── runtime_state/              【个人版专属】 每会话运行态快照
    │   ├── profile_{tk}/               【企业版专属】 企业版租户工作空间（租户隔离，内含各自的：）
    │   │   ├── agent/                          agent 根（结构与个人版 profile/agent/ 相同）
    │   │   ├── .agent_teams/                   该租户的团队名册/任务/工作区
    │   │   ├── .agent_history/                 该租户的工具历史
    │   │   └── runtime_state/                  该租户的会话运行态快照
    │   ├── application_plugins/        【通用】 运维部署的插件（运维资产，全租户共享，无需隔离）
    │   ├── app_market/                 【通用】 应用市场状态：apps/ 应用清单（manifest.json）、instances/
    │   │                                       应用实例、install_ops/ 安装事务、run_consents.json 运行同意
    │   ├── apps/                       【通用】 市场安装的应用工作区本体（hub 安装/本地发布的源码树，
    │   │                                       与 app_market/ 清单成对迁移成对留存）
    │   ├── .personal_context/          【通用】 个人上下文运行态（personal_context.yaml 配置 +
    │   │                                       workspace/ 采集的上下文图与来源元数据）
    │   ├── agents/                     【通用】 子代理会话种子
    │   ├── designer/                   【通用】 designer 图存储
    │   ├── lifecycle/                  【通用】 生命周期状态
    │   ├── sub_agents/                 【通用】 子代理状态
    │   ├── symphony/                   【通用】 symphony 状态
    │   ├── teams/                      【通用】 团队控制库
    │   ├── rsi/                        【通用】 RSI 优化工件
    │   ├── tasks/                      【通用】 任务
    │   ├── jiuwenmate/                 【通用】 JiuwenMate 服务数据（agents.sqlite3、agent_mail/、workspaces/ 等）
    │   ├── organization-owner-inbox.sqlite3    【通用】 组织所有者收件箱
    │   ├── recommendation.db                   【通用】 推荐库
    │   ├── session_messages.sqlite3            【通用】 会话消息库
    │   ├── voice-agent-tasks.sqlite            【通用】 语音 agent 任务库
    │   ├── cli_trusted_dirs_state.json         【通用】 CLI 可信目录状态
    │   │                               （以上散落业务项为既有版本散落于数据根的内容，迁移入内）
    │
    │   ── 以下为系统态：保持数据根原位（与 profile/ 同级）、不共享、不迁移 ──
    ├── .trace/                         【通用】 观测轨迹库（每会话 SQLite）
    ├── run/                            【通用】 运行期 IPC 与暂存（系统态，Pod 本地）：supervisor IPC socket 与锁、
    │   extension-cli-<domain>-<port>.json（扩展 CLI 注册）、runtime-attached（attached 模式 socket）
    ├── config/                         【通用】 业务配置
    │   ├── config.yaml                     AgentServer 配置
    │   └── config-gateway.yaml             Gateway 专属配置（gateway 子进程读取，与
    │                                       AgentServer config.yaml 同目录不同名）
    ├── logs/                           【通用】 系统日志区（企业版经 stdout 采集留存）
    │   ├── agent/                              业务日志：channel / agent_server / full / permissions /
    │   │                                                  service_exits / context_usage / runtime_diagnostics
    │   ├── gateway/                            gateway.log（AGENTOS_GATEWAY_LOG_DIR 可独立指定）
    │   ├── core/                               openjiwen 全家族：jiuwen / llm / server / session /
    │   │                                                           runner / memory / context_engine / sys_operation +
    │   │                                                           interface/ + performance/
    │   ├── process_cli/                        CLI 按次诊断转储
    │   ├── worker-logs/                        历史 Worker 输出日志集中目录（兼容路径）
    │   └── video/                              视频扩展事件流 jsonl
    ├── runtime/                        【通用】 本机安装的运行时资产（含安装锁）
    │   ├── custom-agents/                      自定义 agent runtime（wheel + site-packages/）
    │   ├── npm/                                npm CLI（preserved/、installed/）
    │   ├── external-cli/                       外部 CLI 依赖（<cli>/site-packages/）
    │   ├── playwright-mcp/                     bundled Playwright MCP runtime（<version>/<hash>/，含 .extract.lock）
    │   └── upgrade-config/                     升级待办
    ├── extension/                      【通用】 extension_v2 插件子系统状态（数据根原位，不共享不迁移；
    │   │                                       与 agent workspace 的 extensions/ 复数目录无关）
    │   │                                       （common/extension_config.py 默认相对路径基于数据根解析，
    │   │                                       extensions 配置段与 EXTENSION_*_DIR 环境变量可覆盖）
    │   ├── store/                              插件包仓库（下载暂存）
    │   ├── state/                              插件运行状态
    │   ├── extension_install_report.yaml       跨进程安装报告（附 .lock 与 .locks/ 锁目录）
    │   ├── system_plugin_install_state.json    插件安装状态机（marketplace 扩展安装记录）
    │   └── system_plugin_recovery.json         系统插件恢复台账（附 .lock，独立恢复进程读；
    │                                           报告与台账为带锁的进程间协调文件，须落机器本地）
    ├── extension_pkgs/                 【通用】 已安装插件包本体（extensions.install_dir 配置；
    │                                           随数据根按 Pod 生命周期留存）
    ├── tokenizers/                     【通用】 tokenizer 下载缓存
    ├── auth/                           【通用】 登录态凭据副本（机器域）
    ├── permission_audit/               【通用】 自动权限审批审计流水
    ├── .updates/                       【通用】 升级子系统暂存与状态（安装器下载、升级编排；系统态不迁移不共享）
    │   ├── *.exe/*.dmg/*.part              安装器成品与分片（updater / electron macos_update 下载并定期清理）
    │   ├── .restart_pending.json           gateway 升级重启标记（upgrade_executor.restart_pending_path）
    │   ├── web_process.json                web 前端进程登记 pid+argv（app_web 写、upgrade_executor 升级重启时读）
    │   └── shutdown-*.result               升级关闭回执（desktop_app / electron createShutdownReceipt，pending/ok/failed）
    ├── .browser/                       【通用】 maas skill 专用浏览器状态（机器态：不迁移不共享，新老版本同路径读写）
    │   ├── user-data/                      专用 Chromium user-data-dir（与用户日常浏览器隔离，CDP 调试用，含 SingletonLock 清理）
    │   └── profiles.json                   浏览器 profile 选择与 CDP 配置（历史运行时写入，现仅兼容读取）
    ├── instances.yaml                  【通用】 多实例注册表（仅本机启动流程消费，不可共享）
    └── *.lock/*.txt 等                  【通用】 散落系统态文件：.gateway.lock、last_file_picker_dir.txt

归类规则（按数据所有权，版别无关）：
   - 业务 = 用户直接产生、需要留存 → profile（新增路径一律经 ``get_profile_data_dir()``
     或 workspace 根 ``get_workspace_root_dir()`` 派生）；
   - 系统 = 程序自身维护的数据 → 保持数据根原位，不新增分层；

升级兼容（migrate_profile_data_root_layout，幂等）：
   - 版本初始化时，老目录结构存在（且非软链）→ 剪切迁移到 profile 新结构；
   - 迁移后创建老路径 → 新目录的兼容软链：个人版安装新版本运行一段时间后
     再回滚老版本时，新数据都已落在 profile 下，老版本沿老路径经软链继续
     读写同一份数据；回滚期间老版本写入的数据同样经软链汇入 profile 新
     目录，之后再升级回新版本时无缝衔接——降级/升级双向均不丢不断；
   - 个人版迭代三个正式版本后删除软链创建逻辑。

兼容软链目录树（老版本回滚后沿这些路径访问 profile 新数据）：
    .jiuwenswarm/
    ├── config/
    │   └── runtime_state -> ../profile/runtime_state
    ├── workspace -> profile
    ├── agent -> profile/agent
    │   ├── .logs -> ../../logs/agent
    │   └── process_cli_logs -> ../../logs/process_cli
    ├── application_plugins -> profile/application_plugins
    ├── .agent_teams -> profile/.agent_teams
    ├── .agent_history -> profile/.agent_history
    └── logs/
        └── logs -> core

日志统一收敛到数据根 ``logs/`` 桶
"""


from __future__ import annotations

import logging
import os
import re
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)


# --- 目录名常量 ---
# 统一用户数据目录与工作空间命名（布局契约与迁移规则见模块 docstring）
_PROFILE_DIR_NAME = "profile"

# 企业版租户工作空间目录名：profile/{profile_{tk}}（tk = 租户键）
_ENTERPRISE_TENANT_DIR_PREFIX = "profile_"

# 个人上下文目录名（PersonalContext 运行态 home，挂 profile/ 下）
_PERSONAL_CONTEXT_DIR_NAME = ".personal_context"


# --- 用户主目录与数据根 ---
_user_home: Path | None = None

_workspace_base_dir: Path | None = None


def get_user_home() -> Path:
    """
    获取当前用户主目录。

    优先级：
    1. 缓存值（已通过 set_user_home 或先前调用设置）
    2. JIUWENSWARM_HOME 环境变量
    3. 系统默认 Path.home()
    """

    global _user_home
    if _user_home is not None:
        return _user_home
    env_home = os.getenv("JIUWENSWARM_HOME")
    if env_home:
        _user_home = Path(env_home)
        return _user_home
    _user_home = Path.home()
    return _user_home


def set_user_home(path: Path, initialized: bool = False) -> None:
    """
    设置自定义用户主目录。

    调用后所有路径 getter 将基于新的主目录返回路径。

    Args:
        path: 新的用户主目录路径。
        initialized: 为 True 时跳过缓存重置（路径已在别处初始化时使用）。
    """

    global _user_home, _initialized, _config_dir, _workspace_dir, _root_dir
    global _workspace_base_dir
    _user_home = Path(path)
    if initialized:
        return
    _initialized = False
    _config_dir = None
    _workspace_dir = None
    _root_dir = None
    # _workspace_base_dir 缓存 ``<home>/.jiuwenswarm``，在首次路径查询时填充——
    # 该查询发生在导入期（经 setup_logger()，用的是真实 home）。若保留缓存值，
    # get_user_workspace_dir() 会直接返回缓存而不再询问 get_user_home()：
    # 重定向 home 的测试仍会解析到开发者自己的工作区，强制初始化还会
    # 删除真实的 ~/.jiuwenswarm/config 子目录。
    _workspace_base_dir = None


def get_user_workspace_dir() -> Path:
    """
    获取用户工作区目录路径（``~/.jiuwenswarm`` 或自定义路径）。

    优先级：
    1. 缓存值（已通过 set_user_workspace_dir 或先前调用设置）
    2. JIUWENSWARM_DATA_DIR 环境变量（多实例隔离）
    3. get_user_home() / ".jiuwenswarm"（默认实例）
    """

    global _workspace_base_dir
    if _workspace_base_dir is not None:
        return _workspace_base_dir
    env_workspace = os.getenv("JIUWENSWARM_DATA_DIR")
    if env_workspace:
        _workspace_base_dir = Path(env_workspace)
        return _workspace_base_dir

    _workspace_base_dir = get_user_home() / ".jiuwenswarm"
    return _workspace_base_dir


def reset_path_caches() -> None:
    """清除已解析的路径缓存，下次查询按当前 home/数据根重新解析。

    prepare_workspace 新建工作区（config/ 首次落位）后调用，使后续
    get_config_dir() / get_workspace_dir() 等看到新工作区，而不是命中
    解析失败时缓存下的空值。
    """
    global _initialized, _config_dir, _workspace_dir, _root_dir
    _initialized = False
    _config_dir = None
    _workspace_dir = None
    _root_dir = None


# --- 包资源定位 ---
def _find_source_root() -> Path:
    """
    源码形态下定位仓库根（其下包含 jiuwenswarm/ 包）。
    """
    current = Path(__file__).resolve().parent.parent
    jw_pkg = current / "jiuwenswarm"
    if (jw_pkg / "resources" / "agent").exists():
        return current
    parent = current.parent
    jw_pkg2 = parent / "jiuwenswarm"
    if (jw_pkg2 / "resources" / "agent").exists():
        return parent
    return current


def _find_package_root() -> Path | None:
    """
    尽力探测 jiuwenswarm 包根。

    包安装形态（whl）下 __file__ 位于 site-packages/jiuwenswarm/common/paths.py，
    parent.parent 即 site-packages/jiuwenswarm/。
    源码/可编辑安装形态下 __file__ 位于 <project>/jiuwenswarm/common/paths.py，
    parent.parent 即 <project>/jiuwenswarm/。
    """

    current = Path(__file__).resolve().parent.parent
    jw_pkg = current / "jiuwenswarm"
    if (jw_pkg / "resources").exists():
        return current
    return current


def _find_config_template_path() -> Path:
    """
    定位包内自带的 ``config.yaml`` 模板。
    """
    package_root = _find_package_root()
    if not package_root:
        raise RuntimeError("package root not found")

    candidates = [
        package_root / "resources" / "config.yaml",
        package_root / "config" / "config.yaml",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError(
        "config.yaml template not found; tried: "
        + ", ".join(str(p) for p in candidates)
    )


# --- config 解析与环境 ---
_config_dir: Path | None = None

_workspace_dir: Path | None = None

_root_dir: Path | None = None

_initialized: bool = False


def _resolve_paths() -> None:
    """
    解析并缓存全部路径。
    """
    global _initialized, _config_dir, _workspace_dir, _root_dir

    if _initialized:
        return

    workspace_dir = get_user_workspace_dir()

    # 优先使用已初始化的用户工作区 (~/.jiuwenswarm)，
    # 保证源码运行与安装包运行后的读写路径完全一致。
    # 业务 config 属系统态，保持数据根原位；DeepAgent workspace 属业务态，
    # 落 profile/agent/workspace（见 get_agent_workspace_dir）。
    user_config_dir = workspace_dir / "config"
    user_workspace_dir = get_agent_workspace_dir()
    if user_config_dir.exists():
        _root_dir = workspace_dir
        _config_dir = user_config_dir
        _workspace_dir = user_workspace_dir
    else:
        # 尚未初始化 ~/.jiuwenswarm：从包内 resources 直读配置，工作区指向包内 agent/workspace
        package_root = _find_package_root()
        if package_root and (package_root / "resources" / "config.yaml").exists():
            res = package_root / "resources"
            _root_dir = package_root.parent
            _config_dir = res
            _workspace_dir = res / "agent" / "workspace"
            _workspace_dir.mkdir(parents=True, exist_ok=True)
        else:
            source_root = _find_source_root()
            pkg = source_root / "jiuwenswarm"
            res = pkg / "resources"
            _root_dir = source_root
            _config_dir = res if (res / "config.yaml").exists() else source_root / "config"
            _workspace_dir = res / "agent" / "workspace"
            _workspace_dir.mkdir(parents=True, exist_ok=True)

    _initialized = True


def get_config_dir() -> Path:
    """
    获取配置目录路径。
    """
    _resolve_paths()
    return _config_dir


def get_root_dir() -> Path:
    """
    获取根目录路径。
    """
    _resolve_paths()
    return _root_dir


def get_workspace_dir() -> Path:
    """
    获取工作区目录路径。
    """
    _resolve_paths()
    return _workspace_dir


def get_config_file() -> Path:
    """
    获取 config.yaml 文件路径。
    """
    return get_config_dir() / "config.yaml"


def get_env_file() -> Path:
    """
    获取 ``.env`` 文件路径（config 目录下）。
    """
    return get_config_dir() / ".env"


def get_gateway_config_dir(
    workspace_dir: Path | None = None, *, env: Mapping[str, str] | None = None,
) -> Path:
    """
    获取 Gateway 专属配置目录（与 AgentServer 同目录：``config/``）。

    - 默认 ``<数据根>/config``；
    - 环境变量 ``JIUWENSWARM_GATEWAY_CONFIG_DIR`` 覆盖（相对路径按 base 解析）；
    - 由 prepare_workspace 安装 gateway 模板、app.py 注入 env 供 gateway 子进程读取。
    """
    base = Path(workspace_dir) if workspace_dir is not None else get_user_workspace_dir()
    source = os.environ if env is None else env
    override = source.get("JIUWENSWARM_GATEWAY_CONFIG_DIR", "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            path = base / "config" / path
        return path
    return base / "config"


def get_gateway_config_file(
    workspace_dir: Path | None = None, *, env: Mapping[str, str] | None = None,
) -> Path:
    """
    获取 Gateway 专属配置文件（``config/config-gateway.yaml``，系统态）。
    """
    return get_gateway_config_dir(workspace_dir, env=env) / "config-gateway.yaml"


def get_gateway_config_template() -> Path:
    """
    获取 agent-gateway 包内置的 Gateway 配置模板。
    """
    try:
        import agent_gateway
    except ImportError as exc:
        raise RuntimeError(
            "agent-gateway is not installed; cannot initialize the Gateway config"
        ) from exc
    template = Path(agent_gateway.__file__).resolve().parent / "resources" / "config.yaml"
    if not template.is_file():
        raise RuntimeError(f"agent-gateway config template not found: {template}")
    return template


# --- profile 与租户工作空间 ---
def get_profile_data_dir() -> Path:
    """
    获取统一用户数据目录（profile）：用户直接产生、需要留存、跨 agent
    server 共享的数据一律经此派生；
    - 企业版挂共享卷（PVC/NFS，跨 Pod 存活）
    - 个人版为普通子目录。
    """

    return get_user_workspace_dir() / _PROFILE_DIR_NAME


def get_application_plugins_dir() -> Path:
    """
    获取运维部署的应用插件目录（profile 下 ``application_plugins/``）。

    部署期由运维放入插件包、运行期经扩展加载器只读消费：部署期资产而非
    用户数据，全租户共享、不按租户隔离；企业版随 profile 共享卷跨 Pod
    存活（组件内各副本共享同一目录）。
    """

    return get_profile_data_dir() / "application_plugins"


def get_app_market_dir() -> Path:
    """
    获取应用市场状态目录（profile 下 ``app_market/``）。

    应用清单（apps/）、实例（instances/）、安装事务（install_ops/）与运行
    同意（run_consents.json）：用户经应用市场安装/发布应用的业务状态，随
    profile 共享卷跨 Pod 存活（组件内各副本共享同一目录）；写入一律
    tmp + os.replace 原子替换，无跨进程文件锁。
    """
    return get_profile_data_dir() / "app_market"


def get_market_apps_dir() -> Path:
    """
    获取市场安装的应用工作区根（profile 下 ``apps/``）。

    hub 安装与本地发布应用的源码树本体，与 :func:`get_app_market_dir` 的
    清单成对迁移、成对留存；环境变量 ``APP_MARKET_INSTALL_ROOT`` 与安装
    请求显式路径可覆盖默认落位。
    """
    return get_profile_data_dir() / "apps"


def get_personal_context_dir() -> Path:
    """
    获取个人上下文目录：``<profile>/.personal_context``。
    - PersonalContext 运行态 home，属用户业务数据，企业版随共享卷跨 Pod 存活；
    - AgentServer内唯一 Host 固定 home，不随租户隔离。
    """

    return get_profile_data_dir() / _PERSONAL_CONTEXT_DIR_NAME


def get_user_agents_dir() -> Path:
    """
    散落业务项 agents/（子代理会话种子；工作空间根级，租户隔离）。
    """
    return get_workspace_root_dir() / "agents"


def get_designer_dir() -> Path:
    """
    designer 图存储（工作空间根级 designer/：graphs/、runs/ 等）。
    """
    return get_agent_root_dir() / "designer"


def get_extensions_dir() -> Path:
    """
    Agent 扩展目录（agent workspace 下 ``extensions/``）。
    """
    return get_agent_workspace_dir() / "extensions"


def get_extension_data_dir() -> Path:
    """
    extension_v2 插件子系统状态根（数据根 ``extension/``）。

    安装报告与恢复台账是带锁的进程间协调文件，须落机器本地；插件包仓库与
    运行状态同根存放。 ``extensions.*`` 配置段与 ``EXTENSION_*_DIR`` 环境变量
    可覆盖默认落位，解析逻辑见 ``common/extension_config.py``。
    """
    return get_user_workspace_dir() / "extension"


def get_extension_pkgs_dir() -> Path:
    """
    已安装插件包本体目录（数据根 ``extension_pkgs/``）。
    """
    return get_user_workspace_dir() / "extension_pkgs"


def get_lifecycle_dir() -> Path:
    """
    会话生命周期资源（agent 根下 lifecycle/）。
    """
    return get_agent_root_dir() / "lifecycle"


def get_sub_agents_dir() -> Path:
    """
    子代理状态（agent workspace 下 sub_agents/）。
    """
    return get_agent_workspace_dir() / "sub_agents"


def get_symphony_dir() -> Path:
    """
    symphony 运行时状态（agent workspace 下 symphony/）。
    """
    return get_agent_workspace_dir() / "symphony"


def get_teams_dir() -> Path:
    """
    团队绑定与控制（agent 根下 teams/）。
    """
    return get_agent_root_dir() / "teams"


def get_rsi_dir() -> Path:
    """
    RSI 优化工件（zone 级 profile/rsi/；当前数据模型无租户键，跨租户共享）。
    """
    return get_profile_data_dir() / "rsi"


def get_jiuwenmate_dir() -> Path:
    """
    JiuwenMate 服务数据目录（``profile/jiuwenmate/``：agents.sqlite3、agent_mail/、
    workspaces/ 等业务态数据；迁移自数据根 ``jiuwenmate/``）。
    """
    return get_profile_data_dir() / "jiuwenmate"


def get_workspace_root_dir() -> Path:
    """
    获取承载 agent 子树的工作空间根。
    """

    return get_agent_root_dir().parent


def get_agent_root_dir(tenant_key: str | None = None) -> Path:
    """
    获取 agent 根目录路径。

    ``tenant_key`` 显式传入时按该租户解析；未传时优先读 TenantContext
    绑定路径，再兜底 get_multi_tenant_user_workspace_dir()（个人版即
    get_user_workspace_dir()，企业版未绑定则为 default 三元组）。
    """
    if tenant_key is None:
        try:
            from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext

            bound = TenantContext.current_agent_root()
            if bound is not None:
                return bound
        except ImportError:
            logger.debug(
                "tenant_context unavailable for agent root bind", exc_info=True
            )
    # 函数级导入，避免 paths 模块级依赖 enterprise。
    from jiuwenswarm.enterprise.tenant.tenant_paths import (
        get_multi_tenant_user_workspace_dir,
    )

    return get_multi_tenant_user_workspace_dir(tenant_key) / "agent"


def get_agent_history_dir() -> Path:
    """
    获取工具历史目录（``.agent_history``）。

    - file_ops_{agent_id}.json 的 agent_id 为常量，不含租户键，须随工作空间
      根隔离，避免企业版跨租户合并泄漏；旧位置由迁移与兼容软链覆盖
    """
    return get_workspace_root_dir() / ".agent_history"


def get_agent_teams_dir() -> Path:
    """
    获取团队名册/任务/工作区目录（``.agent_teams``）。

    - 未绑定租户兜底默认租户，与 agent 根一致——读操作可接受，
      写操作应走租户守卫 extension_package_manager._agent_group_home
    - core 内部对应 openjiuwen.agent_teams.paths.get_agent_teams_home
    """
    return get_workspace_root_dir() / ".agent_teams"


def get_updates_dir() -> Path:
    """
    升级子系统目录（数据根 ``.updates/``：安装器下载、升级编排状态）。
    """
    return get_user_workspace_dir() / ".updates"


def get_trace_dir() -> Path:
    """
    观测轨迹库目录（数据根 ``.trace/``：每会话 SQLite + WAL/SHM）。
    """
    return get_user_workspace_dir() / ".trace"


def get_runtime_state_dir() -> Path:
    """
    获取会话运行态目录（工作空间根下 ``runtime_state/``）。

    - 随租户绑定；未绑定的后台流落默认租户，与 agent 根兜底一致
    - 目录内容：每会话 <sid>.yaml、检索预算快照、Electron 桌面端
      发现心跳（机器本地态）
    """
    return get_workspace_root_dir() / "runtime_state"


def get_runtime_state_path(session_id: str | None = None) -> Path:
    """
    获取每会话运行态文件路径（``runtime_state/<sid>.yaml``）。

    - 每个 session 独占一份，避免心跳/定时/并发 session 共用单文件
      互相覆盖 channel/mode/model/git 等字段
    - session_id 为空时回退 ``default``
    - 会话态跟会话走（Pod 重建/重路由后可恢复）
    """

    sid = re.sub(r"[^A-Za-z0-9_.-]", "_", (session_id or "").strip())[:128] or "default"
    return get_runtime_state_dir() / f"{sid}.yaml"


# --- agent 树（按工作空间根隔离） ---
def get_agent_workspace_dir(tenant_key: str | None = None) -> Path:
    """
    获取 agent 工作区目录路径。

    即 agent 根下的 DeepAgent 标准工作区，含 skills、memory、todo、messages
    等标准节点。

    - 个人版: ``~/.jiuwenswarm/profile/agent/workspace``
    - 企业版: ``~/.jiuwenswarm/profile/profile_{tenant_key}/agent/workspace``

    ``tenant_key`` 可选：显式传入时按该租户解析；未传时优先读
    ``TenantContext`` 绑定路径，再兜底 ``get_agent_root_dir() / "workspace"``。
    """

    if tenant_key is None:
        try:
            from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext

            bound = TenantContext.current_agent_workspace_root()
            if bound is not None:
                return bound
        except ImportError:
            logger.debug(
                "tenant_context unavailable for workspace bind", exc_info=True
            )
    return get_agent_root_dir(tenant_key) / "workspace"


def get_agent_sessions_dir() -> Path:
    """
    获取会话轨迹目录（agent 根下 ``sessions/``）。
    """
    return get_agent_root_dir() / "sessions"


def get_default_project_workspace_dir() -> Path:
    """
    获取未绑定项目时的默认任务工作区。

    memory、skills、todo、sessions 等 agent 数据始终直接位于
    ``get_agent_workspace_dir()`` 之下；本目录仅作为未选择项目时用户任务
    产物的默认 cwd / 工作区边界。
    """
    
    return get_agent_workspace_dir() / "projects"


def get_default_project_session_workspace_dir(session_id: str | None = None) -> Path:
    """
    获取单会话的无项目任务工作区。

    布局：
        <agent-workspace>/projects/<session_id>

    ``session_id`` 在可用时参与命名，使同一会话保持稳定的工作区。
    适配器早期初始化缺失该信息时，返回共享 projects 根，避免创建
    一次性会话目录。
    """

    from jiuwenswarm.common.sandbox_fs import ensure_sandbox_writable

    base = get_default_project_workspace_dir()
    raw_session = str(session_id or "").strip()
    if not raw_session:
        base.mkdir(parents=True, exist_ok=True)
        ensure_sandbox_writable(base)
        return base
    safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_session).strip("._-")
    if not safe_session:
        base.mkdir(parents=True, exist_ok=True)
        ensure_sandbox_writable(base)
        return base
    workspace = base / safe_session
    workspace.mkdir(parents=True, exist_ok=True)
    ensure_sandbox_writable(workspace)
    return workspace


def get_agent_home_dir() -> Path:
    """
    获取定时与心跳任务目录（agent 根下 ``home/``）。
    """
    return get_agent_root_dir() / "home"


def get_agent_memory_dir() -> Path:
    """
    获取长期记忆目录（agent workspace 下 ``memory/``）。
    """
    return get_agent_workspace_dir() / "memory"


def get_agent_skills_dir() -> Path:
    """
    获取技能目录（agent workspace 下 ``skills/``）。
    """
    return get_agent_workspace_dir() / "skills"


def get_project_skills_dirs(project_dir: str | os.PathLike[str] | None) -> list[str]:
    """
    返回 *project_dir* 下的项目级 Skill 根。

    无论目录是否存在都返回：Skill rail 每次扫描跳过缺失根，会话中途
    创建的目录会在下次刷新时被拾取，无需重建 rail。
    """
    
    raw = str(project_dir or "").strip()
    if not raw:
        return []
    root = Path(raw).expanduser().resolve(strict=False)
    return [str(root.joinpath(*parts)) for parts in PROJECT_SKILL_DIR_NAMES]


PROJECT_SKILL_DIR_NAMES: tuple[tuple[str, str], ...] = (
    (".agents", "skills"),
    (".claude", "skills"),
)


def get_builtin_skills_dir() -> Path:
    """
    获取包资源内置技能目录。
    """
    package_root = _find_package_root()
    # 优先检查 workspace/skills 目录（标准布局）
    primary_path = package_root / "resources" / "agent" / "workspace" / "skills"
    if primary_path.exists() and primary_path.is_dir():
        return primary_path
    # 回退到 skills 目录
    fallback_path = package_root / "resources" / "agent" / "skills"
    return fallback_path


def get_checkpoint_dir() -> Path:
    """
    获取会话映射检查点目录（agent 根下 ``.checkpoint/``）。
    """
    return get_agent_root_dir() / ".checkpoint"


def get_deepagent_todo_dir() -> Path:
    """
    获取 DeepAgent 待办目录（agent workspace 下 ``todo/``）。
    """
    return get_agent_workspace_dir() / "todo"


def get_deepagent_messages_dir() -> Path:
    """
    获取 DeepAgent 消息目录（agent workspace 下 ``messages/``）。
    """
    return get_agent_workspace_dir() / "messages"


def get_deepagent_agents_dir() -> Path:
    """
    获取 DeepAgent 子代理目录（agent workspace 下 ``agents/``）。
    """
    return get_agent_workspace_dir() / "agents"


def get_deepagent_agent_md_path() -> Path:
    """
    获取 DeepAgent AGENT.md 人设文件路径（agent workspace 下）。
    """
    return get_agent_workspace_dir() / "AGENT.md"


def get_deepagent_soul_md_path() -> Path:
    """
    获取 DeepAgent SOUL.md 灵魂文件路径（agent workspace 下）。
    """
    return get_agent_workspace_dir() / "SOUL.md"


def get_deepagent_identity_md_path() -> Path:
    """
    获取 DeepAgent IDENTITY.md 身份文件路径（agent workspace 下）。
    """
    return get_agent_workspace_dir() / "IDENTITY.md"


def get_deepagent_user_md_path() -> Path:
    """
    获取 DeepAgent USER.md 用户文件路径（agent workspace 下）。
    """
    return get_agent_workspace_dir() / "USER.md"


def get_interactions_dir() -> Path:
    """
    获取待交互上下文目录（agent workspace 下 ``interactions/``）。
    """
    return get_agent_workspace_dir() / "interactions"


def get_cron_jobs_path() -> Path:
    """
    cron_jobs.json 的规范路径，固定在 ``agent/home`` 下。

    Gateway、Agent 工具与存储层统一经本函数取路径，禁止在业务代码中
    硬编码该路径。历史版本遗留的 ``gateway/cron_jobs.json`` 不再读取。
    """

    return get_agent_home_dir() / "cron_jobs.json"


def get_heartbeat_jobs_path() -> Path:
    """
    心跳任务文件（heartbeat_jobs.json）规范路径，位于 ``agent/home``。

    与 :func:`get_cron_jobs_path` 同目录，禁止在业务代码中硬编码该路径。
    """
    
    return get_agent_home_dir() / "heartbeat_jobs.json"


def get_logs_dir() -> Path:
    """
    日志根目录（数据根 logs/，系统态不共享），各组件桶经此派生。

    组件桶：agent/（应用运行日志）、gateway/、core/（openjiwen 家族）、
    runtime/（openjiuwen_runtime foundation）、video/、process_cli/。
    运行日志为程序自维护数据：企业版容器内直接打印 stdout 由客户采集，
    文件副本随数据根（Pod 本地存储）按 Pod 生命周期留存；个人版本机持久。
    """

    return get_user_workspace_dir() / "logs"


def get_core_logs_dir() -> Path:
    """
    openjiwen 全家族日志桶（``logs/core/``：jiuwen/llm/server/session 等）。
    """
    return get_logs_dir() / "core"


def get_agent_logs_dir() -> Path:
    """
    应用运行日志桶（数据根 ``logs/agent/``）。

    channel / agent_server / full / permissions / service_exits /
    context_usage / runtime_diagnostics / async_dump / faulthandler /
    jiuwen_memory 等应用侧日志一律经本函数派生，不得自行拼接；
    core/、gateway/、process_cli/、video/ 等其他桶经 :func:`get_logs_dir` 派生。
    """
    
    return get_logs_dir() / "agent"


def get_runtime_logs_dir() -> Path:
    """
    openjiuwen_runtime foundation 日志桶（``logs/runtime/``）。
    """
    return get_logs_dir() / "runtime"


def get_worker_logs_dir() -> Path:
    """
    历史 Worker 输出日志集中目录（数据根 ``logs/worker-logs/``，系统态，Pod 本地）。

    保留供显式使用集中路径的调用方兼容；Worker 启动默认使用 socket 父目录下的
    ``worker-logs``，也可通过 ``JIUWEN_WORKER_LOG_DIR`` 指定。
    """
    return get_logs_dir() / "worker-logs"


def get_process_cli_logs_dir() -> Path:
    """
    CLI 按次诊断转储桶（``logs/process_cli/``）。
    """
    return get_logs_dir() / "process_cli"


def get_video_logs_dir() -> Path:
    """
    视频扩展事件流 jsonl 桶（``logs/video/``）。
    """
    return get_logs_dir() / "video"


def get_runtime_dir() -> Path:
    """
    本机安装的运行时资产桶（数据根 ``runtime/``，系统态不共享）。

    子桶：custom-agents/（自定义 agent runtime）、npm/、external-cli/、
    playwright-mcp/（bundled Playwright MCP runtime，``<version>/<hash>/`` 含
    ``.extract.lock``）、upgrade-config/（升级待办）。各组件桶一律经本函数
    派生，不得自行拼接。
    """

    return get_user_workspace_dir() / "runtime"


def get_gateway_log_dir() -> Path:
    """
    获取 gateway 日志目录（数据根 ``logs/gateway/`` 桶）。

    可通过环境变量 ``AGENTOS_GATEWAY_LOG_DIR`` 指定独立目录（如 Linux 部署的
    ``/var/log/agentos``）；gateway.log 及其轮转归档都落在该目录。
    """
    
    env_dir = os.getenv("AGENTOS_GATEWAY_LOG_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    return get_logs_dir() / "gateway"


def get_run_dir() -> Path:
    """
    运行期 IPC 目录（数据根 ``run/``：supervisor IPC socket、worker-logs、
    扩展 CLI 注册文件；系统态，Pod 本地存储，不共享、不迁移）。
    """
    return get_user_workspace_dir() / "run"


def get_xy_tmp_dir() -> Path:
    """
    获取小艺临时目录（数据根 ``tmp/xiaoyi/``，懒创建）。
    """
    xy_tmp_dir = get_user_workspace_dir() / "tmp" / "xiaoyi"
    xy_tmp_dir.mkdir(parents=True, exist_ok=True)
    return xy_tmp_dir


def get_package_avatar_cache_dir() -> Path:
    """
    获取 HTTP 下发包头像的租户级缓存目录（agent workspace 下 ``.cache/package-avatars``）。
    """
    return get_agent_workspace_dir() / ".cache" / "package-avatars"


# --- 旧布局迁移 ---
_legacy_migration_done: bool = False

_partition_migration_done: bool = False


def _is_migration_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or (
        os.name == "nt" and getattr(info, "st_reparse_tag", None) == stat.IO_REPARSE_TAG_MOUNT_POINT
    )


def _same_migration_path(old: Path, new: Path) -> bool:
    try:
        return old.samefile(new)
    except FileNotFoundError:
        return False


def _migrate_absorb_file(old: Path, new: Path) -> None:
    """迁移归并的文件级处理：目标不存在剪切；同名同内容剪源；同名异内容源
    改名 ``.migrated`` 备份（正本不覆盖，不静默丢数据）。
    """
    if _is_migration_link(old) or _is_migration_link(new) or _same_migration_path(old, new):
        return
    if not new.exists():
        new.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(old), str(new))
        except FileNotFoundError:
            pass
        return
    try:
        is_duplicate = (
            old.stat().st_size == new.stat().st_size
            and old.stat().st_size <= 32 * 1024 * 1024
            and old.read_bytes() == new.read_bytes()
        )
    except OSError:
        is_duplicate = False
    if is_duplicate:
        old.unlink()
        return
    migrated = new.with_name(f"{new.name}.migrated")
    index = 1
    while migrated.exists():
        index += 1
        migrated = new.with_name(f"{new.name}.migrated.{index}")
    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old), str(migrated))


def _migrate_absorb(old: Path, new: Path) -> None:
    """把 old 递归归并进 new（目录对目录；文件/符号链接按文件处理）。

    场景：目标骨架已先行建立（prepare_workspace 播种 / 并发部分迁移 / 上次
    迁移中断）时，``_move_if_absent`` 不能整体剪切——递归归并保证老数据
    合入新位置，避免幂等跳过导致数据永久滞留老布局。
    """
    if _is_migration_link(old) or _is_migration_link(new):
        return
    if old.is_dir() and new.is_dir():
        if _same_migration_path(old, new):
            return
        for entry in sorted(old.iterdir()):
            _migrate_absorb(entry, new / entry.name)
        try:
            if not any(old.iterdir()):
                old.rmdir()
        except OSError:
            pass
        return
    _migrate_absorb_file(old, new)


def _move_if_absent(old: Path, new: Path) -> None:
    """
    源路径存在时将其搬迁到目标位置（幂等 + 归并）。

    - 源或目标是软链或 Windows 目录联接 → 静默跳过；
    - 目标不存在 → 整体剪切（rename）；
    - 目标已存在（骨架先行 / 并发部分迁移 / 上次迁移中断）→ 递归归并：
      正本不覆盖，同名异内容源改名 ``.migrated`` 备份，源侧空树剪除——
      骨架先建时老数据不滞留老布局。并发进程抢先搬迁视为对方获胜。
    """
    if _is_migration_link(old) or _is_migration_link(new):
        return
    if not old.exists():
        return
    if new.exists():
        _migrate_absorb(old, new)
        return
    new.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(old), str(new))
    except FileNotFoundError:
        pass


def migrate_profile_data_root_layout() -> None:
    """
    一次性迁移到 profile 布局。

    旧布局 → 新布局（业务数据收敛 ``profile/``，系统数据保持数据根原位），
    幂等 + 归并：目标不存在整体剪切；目标已存在（骨架先行 / 上次迁移中断 /
    并发部分迁移）递归归并，正本不覆盖，同名异内容源改名 ``.migrated`` 备份，
    可安全重复调用。覆盖两类起点：
    - 个人版旧数据根（config/runtime_state、agent/、application_plugins/、
      .agent_teams/、.agent_history/ 平铺在数据根；.trace 保持数据根原位
      不迁移——SQLite WAL 不上共享卷）；
    - 更早期商用布局（workspace/ 内含 agent、sessions 等）。

    迁移完成后创建老路径 → 新目录的兼容软链（见
    ``_ensure_legacy_compat_symlinks``），支撑已商用老版本对老目录的访问。
    失败仅降级告警，不抛出：迁移幂等，下次启动自动重试。
    """

    from jiuwenswarm.enterprise.edition import is_enterprise

    if is_enterprise():
        # 企业版：容器弹性伸缩、无就地升级，数据卷由部署工具/运维迁移，
        # 代码不自迁移也不建兼容软链；个人版本机升级场景正常执行。
        return
    global _partition_migration_done
    if _partition_migration_done:
        return
    _partition_migration_done = True
    try:
        _do_migrate_profile_data_root_layout()
    except Exception:  # noqa: BLE001
        logger.error(
            "workspace profile migration failed; continuing with degraded layout"
            " (retried on next start)",
            exc_info=True,
        )


def _do_migrate_profile_data_root_layout() -> None:
    # 旧平铺 .checkpoint/.logs 搬运（幂等）先于 profile 搬迁执行
    _migrate_legacy_checkpoint_and_logs()

    root = get_user_workspace_dir()
    profile = get_profile_data_dir()
    if not root.exists():
        return

    def _move(old: Path, new: Path) -> None:
        _move_if_absent(old, new)

    # 迁移辅助：目录递归归并与空树剪除（symlink 视为兼容链接，跳过）。

    def _prune_empty_tree(top: Path) -> None:
        if _is_migration_link(top) or not top.is_dir():
            return
        # 尽力而为：任一目录剪除失败（如并发写入）即放弃本轮，
        # 迁移幂等，下次启动重试
        try:
            for child in top.iterdir():
                _prune_empty_tree(child)
            if not any(top.iterdir()):
                top.rmdir()
        except OSError:
            pass

    def _absorb_tree(old: Path, new: Path) -> None:
        if _is_migration_link(old) or _is_migration_link(new):
            return
        if not old.is_dir() or _same_migration_path(old, new):
            return
        for entry in sorted(old.iterdir()):
            _move_if_absent(entry, new / entry.name)
        _prune_empty_tree(old)

    # 1) 会话运行态：历史位置（electron 散落态与 config/runtime_state）
    #    → profile/runtime_state（目标已存在时跳过，正本不覆盖）。
    #    落位固定 zone 级（迁移期无租户上下文，不得用 get_runtime_state_dir——
    #    其企业版未绑定态落默认租户根，会破坏幂等）；与
    #    ensure_config_runtime_state_compat_link 的链目标严格配对。
    _move(root / "runtime_state", profile / "runtime_state")
    _move(root / "config" / "runtime_state", profile / "runtime_state")

    # 2) 业务态：agent/、application_plugins/、团队库、观测轨迹、工具历史
    #    → profile/ 对应位置；agent 子树落个人版工作空间下。
    _move(root / "agent", profile / "agent")
    _move(root / "application_plugins", profile / "application_plugins")
    _move(root / ".agent_teams", profile / ".agent_teams")
    # .agent_history（文件工具操作审计）按工作空间根隔离：
    # 个人版 → profile/.agent_history；企业版租户随 profile_{tk}
    # 整树迁入，无需单独处理。
    _move(root / ".agent_history", profile / ".agent_history")

    # 2.2) zone 级散落业务项（不按租户隔离）：既有版本写在数据根、新方案挂在
    #      get_workspace_root_dir()（工作空间根）下的业务数据 → profile/ 同名。
    #      （这些目录/文件由既有版本按需创建，老环境存在则搬迁。）
    for name in ("agents", "designer", "lifecycle", "sub_agents",
                 "symphony", "teams", "apps", "app_market",
                 "organization-owner-inbox.sqlite3", "recommendation.db",
                 "session_messages.sqlite3", "voice-agent-tasks.sqlite"):
        _move(root / name, profile / name)
    _move(root / "rsi", profile / "rsi")
    _move(root / "tasks", profile / "tasks")
    _move(root / "jiuwenmate", profile / "jiuwenmate")
    _move(root / "cli_trusted_dirs_state.json", profile / "cli_trusted_dirs_state.json")
    # 既有版本的 process_cli 诊断在 agent/process_cli_logs（agent 树内）：
    # 随 agent 整树迁入后，吸收进 logs/process_cli 桶并在老位置留软链。
    legacy_process_cli_logs = profile / "agent" / "process_cli_logs"
    if legacy_process_cli_logs.is_dir() and not legacy_process_cli_logs.is_symlink():
        _absorb_tree(legacy_process_cli_logs, get_process_cli_logs_dir())
        _prune_empty_tree(legacy_process_cli_logs)
    _move(root / _PERSONAL_CONTEXT_DIR_NAME, get_personal_context_dir())

    # 2.1) 企业版旧租户根：<root>/workspace_{tk} → profile/profile_{tk}
    #      （旧目录名 workspace_{tk} 在新方案下更名 profile_{tk}）
    for entry in sorted(root.glob("workspace_*")):
        if entry.is_dir() and not entry.is_symlink():
            tenant_part = entry.name[len("workspace_"):]
            tenant_root = profile / (_ENTERPRISE_TENANT_DIR_PREFIX + tenant_part)
            _move(entry, tenant_root)
            # 租户内遗留运行态（workspace_{tk}/config/runtime_state）→ 租户根 runtime_state
            legacy_rs = tenant_root / "config" / "runtime_state"
            if legacy_rs.is_dir() and not legacy_rs.is_symlink():
                _absorb_tree(legacy_rs, tenant_root / "runtime_state")
                _prune_empty_tree(legacy_rs)

    # 3) 更早期商用布局：workspace/ 内 agent、sessions 等平铺项
    #    吸收进 profile/（同名目录递归归并；agent/ 即新方案的 agent 根）。
    stray_workspace = root / "workspace"
    if stray_workspace.is_dir() and not _is_migration_link(stray_workspace):
        for entry in sorted(stray_workspace.iterdir()):
            _absorb_tree(entry, profile / entry.name)
        _prune_empty_tree(stray_workspace)

    # 4) 散落日志归位 logs/ 桶：agent/.logs → logs/agent，logs/logs → logs/core
    moved_agent_root = profile / "agent"
    legacy_agent_logs = moved_agent_root / ".logs"
    if legacy_agent_logs.is_dir() and not legacy_agent_logs.is_symlink():
        _absorb_tree(legacy_agent_logs, get_agent_logs_dir())
        _prune_empty_tree(legacy_agent_logs)
    stray_logs = root / "logs"
    if stray_logs.is_dir() and not _is_migration_link(stray_logs):
        for entry in sorted(stray_logs.iterdir()):
            if entry.name == "logs":
                # openjiwen core 家族日志（run/、interface/、performance/）
                _absorb_tree(entry, get_core_logs_dir())
            elif entry.is_dir():
                bucket = get_logs_dir() / entry.name
                # 活跃桶自吸收守卫：entry 与目标桶解析为同一路径（如本次运行
                # 刚创建的 logs/agent、logs/process_cli），说明该桶已是新规范
                # 本体——沿树吸收会把正在写入的日志当作"内容一致的重复"
                # unlink 掉，必须跳过。
                if bucket.resolve() == entry.resolve():
                    continue
                _absorb_tree(entry, bucket)
            else:
                _migrate_absorb_file(entry, get_logs_dir() / entry.name)
        _prune_empty_tree(stray_logs)

    # 5) 老路径兼容软链（降级兼容，见 _ensure_legacy_compat_symlinks）
    _ensure_legacy_compat_symlinks(root)


def _migrate_legacy_checkpoint_and_logs() -> None:
    """
    迁移更早期平铺在数据根的 ``.checkpoint`` / ``.logs``。

    - ``.checkpoint``（业务态）→ ``profile/agent/``
    - ``.logs``（系统日志）→ ``logs/agent/``

    幂等：仅当旧路径存在且新路径不存在时搬迁；先于 profile 整体搬迁执行。
    """

    global _legacy_migration_done
    if _legacy_migration_done:
        return
    _legacy_migration_done = True

    workspace = get_user_workspace_dir()
    _move_if_absent(workspace / ".checkpoint", get_checkpoint_dir())
    _move_if_absent(workspace / ".logs", get_agent_logs_dir())


def _cleanup_team_files_under(base: Path, agent_dir: Path) -> None:
    """
    清理 base（数据根）下旧版本 team 遗留文件。
    """
    # 清理 {base}/workspace/ (旧版本 team workspace)
    legacy_workspace = base / "workspace"
    if legacy_workspace.exists() and not legacy_workspace.is_symlink():
        try:
            shutil.rmtree(legacy_workspace)
            logger.info(f"[Cleanup] Removed legacy workspace directory: {legacy_workspace}")
        except OSError as e:
            logger.warning(f"[Cleanup] Failed to remove legacy workspace directory: {e}")

    # 清理 {agent_dir}/team_data/ (旧版本 team 数据库目录)
    legacy_team_data = agent_dir / "team_data"
    if legacy_team_data.exists():
        try:
            shutil.rmtree(legacy_team_data)
            logger.info(f"[Cleanup] Removed legacy team_data directory: {legacy_team_data}")
        except OSError as e:
            logger.warning(f"[Cleanup] Failed to remove legacy team_data directory: {e}")

    # 清理 {base}/team.db* (旧版本 team 数据库文件)
    legacy_team_db_root = base / "team.db"
    for suffix in ["", "-wal", "-shm"]:
        db_file = legacy_team_db_root.with_suffix(".db" + suffix)
        if db_file.exists():
            try:
                db_file.unlink()
                logger.info(f"[Cleanup] Removed legacy team database file: {db_file}")
            except OSError as e:
                logger.warning(f"[Cleanup] Failed to remove legacy team database file: {e}")

    # 清理 {agent_dir}/team.db* (旧版本 team 数据库文件)
    legacy_team_db_agent = agent_dir / "team.db"
    for suffix in ["", "-wal", "-shm"]:
        db_file = legacy_team_db_agent.with_suffix(".db" + suffix)
        if db_file.exists():
            try:
                db_file.unlink()
                logger.info(f"[Cleanup] Removed legacy team database file: {db_file}")
            except OSError as e:
                logger.warning(f"[Cleanup] Failed to remove legacy team database file: {e}")


_LEGACY_COMPAT_LINKS = (
    # (数据根上的老路径, 相对数据根的新目标)；目标存在才建链
    ("workspace", Path(_PROFILE_DIR_NAME)),
    ("agent", Path(_PROFILE_DIR_NAME) / "agent"),
    ("application_plugins", Path(_PROFILE_DIR_NAME) / "application_plugins"),
    (".agent_teams", Path(_PROFILE_DIR_NAME) / ".agent_teams"),
    (".agent_history", Path(_PROFILE_DIR_NAME) / ".agent_history"),
)


# --- 兼容软链 ---
def _ensure_legacy_compat_symlinks(workspace_dir: Path, *, require_target: bool = True) -> None:
    """
    按老路径创建指向 profile 新目录的兼容软链（降级/回滚兼容）。

    个人版安装新版本运行一段时间后回滚老版本：业务数据已剪切进 profile/，
    老版本仍按数据根平铺路径读写——在老位置放相对软链，使新老两条路径
    指向同一份数据，回滚后老版本继续读写新数据不丢不断。规则：
    - 老位置存在真实目录/文件 → 不动（迁移未完成，软链会遮蔽数据）；
    - 老位置已是软链 → 仅在指向不一致时替换；
    - 平台禁止软链（如 Windows 未开开发者模式）→ 静默跳过；
    - ``require_target=False``（prepare_workspace 新初始化路径）：目标目录尚
      未创建也建链（必要时先建空目录），悬挂软链由老版本首次写入时填实。
    config/runtime_state 的兼容链单独处理（config 本体保持真实目录，
    仅 runtime_state 子目录出链），见 ensure_config_runtime_state_compat_link。
    """

    profile = workspace_dir / _PROFILE_DIR_NAME
    if not profile.is_dir():
        if require_target:
            return
        profile.mkdir(parents=True, exist_ok=True)
    for old_name, target in _LEGACY_COMPAT_LINKS:
        tgt = workspace_dir / target
        if not tgt.is_dir():
            if require_target:
                continue
            tgt.mkdir(parents=True, exist_ok=True)
        _ensure_compat_symlink(workspace_dir / old_name, target)
    ensure_config_runtime_state_compat_link(workspace_dir, require_target=require_target)
    _ensure_log_compat_symlinks(workspace_dir, require_target=require_target)


_LOG_COMPAT_LINKS = (
    # (link 相对 workspace_dir, 挂载桶名)：
    # 老版本（回滚后）的日志写入点 → 新收敛的 logs/ 桶，保证日志查询连续
    (Path(_PROFILE_DIR_NAME) / "agent" / ".logs", "agent"),
    (Path(_PROFILE_DIR_NAME) / "agent" / "process_cli_logs", "process_cli"),
)


def _ensure_log_compat_symlinks(workspace_dir: Path, *, require_target: bool = True) -> None:
    """
    日志收敛的老路径兼容软链（回滚后老版本日志汇入新桶）。

    既有版本的日志是分散的：主运行日志 agent/.logs、process_cli 诊断
    agent/process_cli_logs、openjiwen core 家族（cwd=数据根时代）logs/logs。
    新方案收敛到 logs/{agent,process_cli,core,…}；回滚后的老版本仍按老
    位置写——在老位置放软链指入新桶，日志查询不断档。桶目录不存在时先
    创建（否则悬挂软链会让老版本写日志直接失败）。
    """

    logs_root = workspace_dir / "logs"
    logs_root.mkdir(parents=True, exist_ok=True)
    for link_rel, bucket in _LOG_COMPAT_LINKS:
        bucket_dir = logs_root / bucket
        bucket_dir.mkdir(parents=True, exist_ok=True)
        # 老位置经 agent 兼容软链已落在 profile/agent/ 下：
        # <root>/profile/agent/.logs → ../../logs/agent
        # （悬挂软链会让老版本写日志直接失败，故目标桶先建、父目录先建）
        link = workspace_dir / link_rel
        link.parent.mkdir(parents=True, exist_ok=True)
        _ensure_compat_symlink(link, Path("..") / ".." / "logs" / bucket)
    # 老版本 openjiwen core 家族按 cwd=数据根写 logs/logs → logs/core
    core_dir = logs_root / "core"
    core_dir.mkdir(parents=True, exist_ok=True)
    _ensure_compat_symlink(logs_root / "logs", Path("core"))


def _ensure_compat_symlink(link: Path, target: Path) -> None:
    """
    幂等创建/校正相对软链：已指向目标则跳过，真实路径绝不覆盖。

    替换旧链采用"临时名先行"两步换入：Windows 未开开发者模式（无
    SeCreateSymbolicLinkPrivilege）时建链会失败，若先 unlink 旧链再建新链，
    失败会让老路径链接直接丢失——先在临时名下建新链，成功后才换入。
    """
    if link.is_symlink():
        try:
            if link.resolve() == (link.parent / target).resolve():
                return
        except OSError:
            pass
        staged = link.with_name(f".{link.name}.compat-new")
        if staged.is_symlink():
            try:
                staged.unlink()
            except OSError:
                logger.debug("[Compat] failed to clean stale staged symlink: %s", staged)
                return
        elif staged.exists():
            # 临时名被真实路径占用（异常残留）：不动它，放弃本次校正
            return
        try:
            staged.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            logger.debug("[Compat] compat symlink replacement skipped at %s", link)
            return
        try:
            link.unlink()
        except OSError:
            logger.debug("[Compat] failed to replace stale symlink: %s", link)
            try:
                staged.unlink()
            except OSError:
                pass
            return
        try:
            staged.replace(link)
        except OSError:
            logger.debug("[Compat] failed to swap in staged symlink: %s", link)
    elif link.exists():
        # 老位置仍是真实目录/文件（含 Windows junction，is_symlink 对其恒为
        # False）：迁移未完成，软链会遮蔽数据
        return
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
            logger.info("[Compat] linked legacy path: %s -> %s", link, target)
        except (OSError, NotImplementedError):
            logger.debug("[Compat] compat symlink not created at %s", link)


def ensure_config_runtime_state_compat_link(workspace_dir: Path, *, require_target: bool = True) -> None:
    """
    config/runtime_state → profile/runtime_state 兼容软链。

    会话运行态快照迁移到 profile/runtime_state（跨 Pod 共享）；config 目录
    本体保持真实目录不迁移（业务配置不共享），仅 runtime_state 子目录出链，
    兼容按 <root>/config/runtime_state 定位的旧版本与外部脚本。
    链目标固定 zone 级、与迁移 §1 落位配对；运行期读写走
    get_runtime_state_dir()（个人版与本链目标一致，企业版随租户绑定落
    profile_{tk}，不经本链）。
    """

    profile_state = workspace_dir / _PROFILE_DIR_NAME / "runtime_state"
    profile_state.mkdir(parents=True, exist_ok=True)
    config_dir = workspace_dir / "config"
    if config_dir.is_dir() and not config_dir.is_symlink():
        config_dir.mkdir(parents=True, exist_ok=True)
        _ensure_compat_symlink(
            config_dir / "runtime_state",
            Path("..") / _PROFILE_DIR_NAME / "runtime_state",
        )


__all__ = [
    "ensure_config_runtime_state_compat_link",
    "get_agent_history_dir",
    "get_agent_home_dir",
    "get_app_market_dir",
    "get_application_plugins_dir",
    "get_agent_logs_dir",
    "get_core_logs_dir",
    "get_process_cli_logs_dir",
    "get_worker_logs_dir",
    "get_runtime_logs_dir",
    "get_video_logs_dir",
    "get_agent_memory_dir",
    "get_agent_root_dir",
    "get_agent_sessions_dir",
    "get_agent_skills_dir",
    "get_project_skills_dirs",
    "get_agent_teams_dir",
    "get_agent_workspace_dir",
    "get_builtin_skills_dir",
    "get_checkpoint_dir",
    "get_config_dir",
    "get_config_file",
    "get_cron_jobs_path",
    "get_default_project_session_workspace_dir",
    "get_default_project_workspace_dir",
    "get_deepagent_agent_md_path",
    "get_deepagent_identity_md_path",
    "get_deepagent_messages_dir",
    "get_deepagent_soul_md_path",
    "get_deepagent_todo_dir",
    "get_deepagent_user_md_path",
    "get_env_file",
    "get_gateway_config_dir",
    "get_gateway_config_file",
    "get_gateway_config_template",
    "get_gateway_log_dir",
    "get_heartbeat_jobs_path",
    "get_interactions_dir",
    "get_logs_dir",
    "get_jiuwenmate_dir",
    "get_designer_dir",
    "get_extension_data_dir",
    "get_extension_pkgs_dir",
    "get_extensions_dir",
    "get_jiuwenmate_dir",
    "get_lifecycle_dir",
    "get_market_apps_dir",
    "get_personal_context_dir",
    "get_rsi_dir",
    "get_sub_agents_dir",
    "get_symphony_dir",
    "get_teams_dir",
    "get_user_agents_dir",
    "get_profile_data_dir",
    "get_root_dir",
    "get_runtime_dir",
    "get_run_dir",
    "get_runtime_state_dir",
    "get_trace_dir",
    "get_updates_dir",
    "get_runtime_state_path",
    "get_user_home",
    "get_user_workspace_dir",
    "get_workspace_dir",
    "get_workspace_root_dir",
    "get_xy_tmp_dir",
    "migrate_profile_data_root_layout",
    "reset_path_caches",
    "set_user_home",
]
