# develop 的 AgentOS 多用户场景适配

实施原则：**“develop 的 AgentOS 多用户场景适配”实施，不必同时改变个人版行为。**

实现约束：**只实现必要适配，优先迁移 agent_os 的实现，不附带通用框架、接口重设计或无关重构。**

基于 develop `f0a69728c` 的 `codex/develop-agentos-adaptation` 分支吸收 agent_os
`0b03ec251` 所需的增量能力，保持 develop 的路由、canonical mode、会话生命周期和认证机制。
契约以 `D:\project\AgentBox-Client` 当前客户端为准，客户端无需修改。

## 启动与隔离

YuanRong 部署脚本在 systemd 和 nohup 两种 Gateway 启动方式中设置
`JIUWENSWARM_RUNTIME_PROFILE=agentos`。Router 为内置 AgentServer 容器注入同一标识；
内部启动的 JiuwenBox 子进程得到 `JIUWENBOX_RUNTIME_PROFILE=agentos`。
外部 JiuwenBox 服务需自行设置该标识及 `JIUWENBOX_ETCD_ENDPOINTS`。

Gateway 的 Web mode 兼容和数据面监听还以实际使用 AgentOS Router 为条件；
个人版选择本地 AgentClient 时不会启用。运行标识由部署设置，不接受客户端请求参数。
第三方 Agent 的运行配置沿用注册中心定义，不注入内置 Agent 的扩展配置。
容器环境标识和部署默认值只在创建新实例时注入；上线需滚动更新存量内置实例，
避免只升级 Gateway 后误认为旧容器已具备所有新增能力。

个人版继续使用既有入口、ProcessRuntime 和会话 mode 约定，Code Graph 默认关闭；
不增加 Conch 或语言包的默认安装、初始化、下载。全局 SDK pin 保持 develop 的值。

## 冻结客户端兼容

Web 出站统一投影：所有 canonical 单 Agent mode 回显 `agent`，团队 mode 回显 `team`，
`auto_harness`、未知 mode、成员角色、工具参数和用户文本保持原样。
覆盖会话列表、元数据、创建/切换响应中的 mode、会话/聊天推送和 `history.message` 中的历史记录。
运行时和磁盘仍使用 canonical mode，`work_mode` 仍独立表达 work/code。
公共会话列表、元数据和会话消息接口保持 develop 的 canonical mode；不保存新的
`wire_mode`，已有该字段的记录也不用于覆盖公共响应。冻结客户端兼容只在
AgentOS Router 的 Web 出站路径完成，个人版 Web、TUI 和内部 E2A 不套用投影。

无项目云端聊天携带的 `/home/agentos/workspace` 是执行目录提示，不写成新的项目绑定。
只有 AgentOS Web、无真实项目 ID 且没有其他已绑定目录时采用此规则。
已有项目和旧版“有目录、无 ID”的绑定继续生效。本次不批量清空历史元数据。

实际客户端没有调用 `session.kvc.prepare` 或 `session.input.intent`，因此不强制增加旧版 Web 的别名。
develop 的 KVC lifecycle participant、团队 quiesce/release/dispose、Ascend affinity 保留现有实现。
`PROACTIVE_RECOMMENDATION` 枚举补齐；客户端消费的 source 标记 `chat.final` 发送路径继续使用，
没有新增独立事件双发，以免同一条推荐展示两次。

## AgentOS 备用模型选择（2026-10-08 补齐）

参考 `agent_os` 的实际实现移植增量：

- `b8273f093`：团队模型按 `model_name#origin_index`、alias、裸名依次解析。
- `5cc86e59b`：空的 defaults 占位项不能遮住可用的 AgentOS 模型。
- `89f2e35ae`：Web 的 `modelSelectKey` 使用 alias，否则使用 `model_name#origin_index`。

冻结的 `D:\project\AgentBox-Client` 已包含 `is_default !== false || is_agentos === true`，
备份模型能进入选择器，不能套用旧内置 Web 的“前端过滤未适配”结论。
但它的 `modelWireName` 发送 `alias || model_name`；没有 alias 时不会发送 origin_index。
因此同名 defaults/agentos 或多条同名 agentos 在发送后仍可能合并成裸名。

为保持客户端不改，AgentOS Router 的 Web 出站响应对**同名且没有显式 alias 的 AgentOS 条目**，
补充响应 alias 为原分支的 `model_name#origin_index`。客户端解析到该列表项后，沿已有 alias
发送路径透传选择键。显式 alias、不同名模型、默认模型、active_model、model_id、source 和
is_default/is_agentos 字段保持原值；响应投影不改变运行时配置或磁盘 YAML。
既有 models.replace_all 的 is_agentos 过滤继续防止备用模型及响应 alias 写入 defaults。
这个 alias 只用于兼容旧模型选择协议，不是新的持久模型 ID。

团队加载器在 `JIUWENSWARM_RUNTIME_PROFILE=agentos` 下移植原分支选择逻辑，
继续使用 develop 的 get_default_models 归一化/解密结果，不覆盖整个加载器。
保留登录模型、Zen 缓存、reasoning 转换、不同凭据的模型池和成员显式模型优先级。
索引按未经重排的 defaults+agentos 合并列表解释，匹配时同时验证模型名和下标。
空占位仅在选择默认回退项时跳过，不过滤列表后重新编号。
裸名同名时仍优先 defaults；无匹配时记录 warning 并采用既有配置回退。

单 Agent 保留 develop 的 global_index -> per-name cache_key 映射，避免两种索引碰撞；
仅在 AgentOS 场景补上原分支团队实现同样的模型名校验。个人版原选择逻辑保持不变。
不改冻结客户端、不调整个人版入口、不将响应兼容 alias 存入配置，也不移植旧前端强制团队使用默认模型的逻辑。

示例：合并列表为 `other#0`、defaults 的 `shared#1`、agentos 的 `shared#2`。
第三项响应 alias 为 `shared#2`，聊天请求携带该值，单 Agent 和团队 fallback 都选择第三项
对应的 api_base/api_key；团队成员显式配置仍优先于页面选择。

索引是当前列表的位置，列表重排后需要刷新客户端 models.list；它不能跨重排稳定识别同名端点。
只有客户端先拿到列表并解析到对应项时 alias 路径才能保留索引；历史缓存中只有裸名的选择
无法恢复原先的同名备用端点。上线联调需覆盖重新登录、重连和配置列表变更。
项目没有升级为另一套模型选择协议，develop 的 model_selection/model_id 能力仍保留。

本次项目 .venv 定向运行 22 个用例通过（新增 13 个 + 既有 9 个），覆盖真实客户端响应键到团队
加载器、同名地址/凭据选择、全局/同名索引碰撞、alias、占位回退、成员优先级、个人版隔离，
以及 develop 登录/Zen/解密/模型池回归。六个涉及文件 Ruff F/E9 和 git diff --check 通过。
本次未运行全量 UT，也未连接真实集群发送模型请求；冻结客户端联调仍按下文上线流程执行。

## etcd 数据面热同步

Gateway 复用 Cron 的 endpoint 配置和公共 `common/etcd` 客户端；旧 Cron import 路径保留兼容导出。
JiuwenBox 是可单独部署的子包，保留只读 range/watch 实现，不依赖 swarm 包。
两者默认读取 `/agentos/config/data-plane`。配置为 YAML 或 JSON，例如：

```yaml
_version: 1
gateway:
  agent_sandbox:
    idle_timeout: 600
jiuwenbox:
  timeout:
    idle_timeout: 900
  conch:
    template_name: agentos-code
    ram_mb: 4096
```

外层 `gateway` 是组件 section，内层 `agent_sandbox.idle_timeout` 沿用 develop 的数据面协议。
Gateway 白名单只有 `agent_sandbox.idle_timeout`；`gateway.agentos.sandbox_idle_timeout_seconds`、
`sandbox.cpu/memory`、模型、凭据、认证和其他字段不会被远端覆盖。
空闲回收设置立即作用于 Router；CPU/内存保留部署配置，不通过 Gateway 数据面热更新。
不向活跃用户广播全量配置、不唤醒离线用户、不改写用户 config.yaml。

初次 range 后从 snapshot revision + 1 建立 watch。取消/compaction 会触发重新读取；
连接失败退避重连。应用失败按 revision 重试，新 revision 能接替失败 revision。
停止服务会取消应用任务并关闭客户端。

缺失字段保留已生效覆盖；删除键/空 section 不重置；null、非法数值拒绝应用。
Gateway 重启后重新读取当前数据面，不保留旧进程的覆盖缓存。
恢复默认应发布明确的目标值；JiuwenBox 若要回到基础 YAML，还需要停服务、备份并移走已保存快照后重启。

JiuwenBox 默认策略以完整合并快照原子保存至 `~/.jiuwenbox/update_policy.yaml`；
重启优先读取此快照。默认策略影响新建沙箱；存量沙箱只更新运行时支持的
`network.egress/ingress` 或 `conch.network.egress/ingress`。
存量更新失败不推进已应用 revision；默认保存成功并不代表所有存量沙箱都更新成功。
已有 `/api/v1` Bearer 认证机制继续保护策略 API。
GET `/api/v1/policies` 返回当前默认策略；PUT 批量接口可用
`update_default_policy` / `update_existing_sandboxes` 分别控制默认与存量更新。
个人版默认策略持久化关闭，既有网络方向替换语义不变。

## Conch

在 AgentOS `.env.custom` 中明确选择：

```dotenv
TOOL_SANDBOX_TYPE="jiuwenbox-conch"
TOOL_SANDBOX_ENABLE="true"
CONCH_TEMPLATE_NAME="agentos-code"
CONCH_SDK_CONFIG="/etc/conch/sdk.yaml"
```

工具沙箱配置与 YuanRong 部署容器的 `SANDBOX_TYPE` 是两层不同配置。
模板默认仍为 jiuwenbox/bubblewrap，Conch 必须显式选择。SDK 配置路径必须在 Agent 运行镜像中存在。
Gateway 将有限部署默认值传入内置容器，容器内显式本地配置优先。
已有用户的 config.yaml 若已显式选择另一个 sandbox.type，需按用户/镜像配置策略调整，避免强制覆盖用户选择。

AgentServer 的 provider 继续使用 JiuwenBox HTTP 协议，通过 `sandbox_runtime=conch` 选择服务端运行时。
服务端按 sandbox ref 分派创建、执行、文件操作、后台任务、删除与网络更新。
缺省仍为 ProcessRuntime；Conch SDK 在选中该运行时后才导入。
Conch 不支持 stop，停止操作返回明确错误，可用删除或冷重建；文件挂载仅支持目录。
单文件 allow/deny/upload 配置在创建前拒绝；内置的单文件只读 config.yaml 挂载在 Conch 路径中省略，
不扩大挂载到可能包含凭据的整个配置目录。配置解析仍由宿主 AgentServer 完成。
SDK provider 的宿主机 fallback 仍禁用。

镜像需预装 Conch SDK，提供有效 SDK 配置和可访问的 Conch 服务/模板。
本仓库不安装 SDK、不创建真实 Conch 沙箱、不修改生产服务。

## Code Graph

AgentOS `.env.custom` 设置 `AGENTOS_CODE_GRAPH_PROFILE="graph"`，模板选择 root 作为 owner。
也可在 AgentServer 本地 YAML 配置 `code_graph.agent: code_agent`。
profile 默认 off；部署默认值只填补容器配置中缺失的字段。

接线包括 root rail、code 子 Agent build 参数及既有 factory_kwargs、root prompt、实时工具刷新后的 rail 挂载、
后台图构建、配置/项目切换预载、原分支的 workspace 缓存路径、按需语法预载和
`command.status` overview 中的 `code_graph` 状态。
状态读取 SDK manager 的 stats，不在宿主维护另一套图状态；profile 关闭返回 absent，
SDK 不可用返回 unavailable。SDK/语法能力不可用时继续使用文本搜索并记录诊断。

**依赖限制：当前 develop 固定 SDK 和本机 .venv 缺少 Code Graph 模块。**
部署启用 graph 前必须提供兼容 develop 其他接口的 SDK 构建，包含：

- `openjiuwen.core.retrieval.code_graph.models.CodeGraphConfig`
- `openjiuwen.core.retrieval.code_graph.manager.get_code_graph_manager`
- `openjiuwen.harness.rails.code_graph_profile_rail.CodeGraphProfileRail`
- 若选择 code_agent，build_code_agent_config 及其 factory_kwargs 必须支持 Code Graph 参数
- tree-sitter language pack 和语言语法缓存

不能直接把全局 SDK 依赖切回旧 `agent_os` 分支。
离线镜像应预装语法；运行时预载在后台执行，下载失败回退并记录状态。
全局个人版启动不预载。

## 2026-10-08 实现收敛

保留冻结客户端实际需要的 mode 投影、同名模型选择、默认 cwd 处理和部署场景隔离。
保留原分支的配置监听/合并、Conch、策略同步与存储、Code Graph 能力；不因减少文件数量而删除必要功能。

本次收敛已经执行：

- 删除此前新增的 code_graph_runtime.py 和 CodeGraphRuntime 协调类。
- 在现有 interface_code.py 中按原分支组织配置、rail、子 Agent 参数和后台预载接线。
- 移除自建图状态缓存及 cache_dir 的用户/项目哈希目录，沿用原分支的 agent workspace 路径和 SDK 索引管理。
- command.status 直接读 SDK stats，不构造临时适配器，也不新增状态查询触发的后台构建。
- 去掉对 SDK factory 必需参数集合的额外探测；仅保留原分支已有的可选 retrieval_interface 参数判断。
- SandboxRef 直接使用 develop 的 runtime 字段；移除额外 sandbox_runtime 属性，保留读取旧状态字段的兼容。

相对本批补齐前的 develop，现有新增文件为 21 个（不计原有 .zcode）：15 个原分支已有，
2 个必要兼容模块（agentos_runtime / agentos_compat）、3 个定向测试文件、1 个实施说明。
新增文件中包含包初始化文件，数量不能等同于新设计数量。原分支已有模块按需要迁移，
个人版隔离和 develop 生命周期差异只做局部调整。

收敛后使用项目 .venv 和临时数据目录，38 个定向用例通过；包括 SDK 不可用、个人版不初始化图、
SDK rail 接线的注入验证、Conch 分派、策略存储、配置热更新和客户端模型契约。
这不构成真实 Code Graph/Conch/YuanRong 集成验证，也不替代个人版的完整回归。
真实 SDK 和集群验证未完成前，不应将本分支视为已完成上线验收。

## YuanRong 多用户挂载与身份模型（2026-10-09 对齐 agent_os）

在 `JIUWENSWARM_RUNTIME_PROFILE=agentos` 下对齐 agent_os `722c3c8f1` /
`b92008d5f` / `ba4e86016` / `0efb93f7a` / `8719af53f` 的 YuanRong 挂载与身份模型。
个人版保留 develop 的 agent_root 身份挂载、yaml mounts 合并及原默认工作目录。

- AgentOS 的 `_build_yuanrong_extra_params` 使用 workspace + `~/workspace` 项目目录挂载；
  `JIUWENSWARM_USER_DIRECTORY`（Router 创建内置容器时注入）存在时，
  workspace 挂载源映射为 `<user_dir>/.jiuwenswarm/agent/workspace`、
  项目目录映射为 `<user_dir>/workspace`（宿主真实路径 → 容器内路径）。
- `sandbox.user` / `sandbox.group` 透传（`get_sandbox_endpoint` yuanrong 可选键），
  sysop_builder 解析为数字 `uid:gid`（名称经 pwd/grp 查找）写入 extra_params.user，
  交给 YuanRong 作为容器运行身份；多用户身份的实际消费在 YuanRong 侧，
  本仓库只负责透传。user/group 必须同时设置才生效，数值或字符串的 `0` 均为合法值。
- AgentOS 的 `_resolve_project_dir` 与自动管理路径视图跳过 jiuwenswarm 数据根
  （`.jiuwenswarm` / `JIUWENSWARM_DATA_DIR`），该树已由 workspace 专属挂载覆盖。
- AgentOS 的 `build_yuanrong_sandbox_status_view` 异常回落空挂载列表；个人版仍回落 yaml mounts。
- deploy/yuanrong 模板 sandbox 段补 `cpu` / `memory` / `user` / `group` 占位符
  （TOOL_SANDBOX_CPU/MEMORY/USER/GROUP，默认留空；这些是工具沙箱的静态部署配置，
  Gateway etcd 数据面不会覆盖 sandbox.cpu/memory）。

Conch 的挂载构造（`jiuwenbox-conch` 分支的内联后处理、单文件 config 省略、
run_as_user/run_as_group）是本分支的既有实现，与 agent_os 的
`share_mounts_only` 方案等价，run_as 的数值 `0` 同样保留。个人版即使显式使用
YuanRong，也保持原有 `sandbox.mounts` / `rootfs.mounts` 合并、挂载去重、默认及
显式 workdir。YuanRong 的新增运行身份传递限定于 AgentOS 场景。

## 冻结客户端对齐审计（2026-10-09）

以 `D:\project\AgentBox-Client` 当前代码为契约逐功能域核对服务端（连接认证、
会话生命周期、mode 投影、默认容器目录、聊天流、模型选择、文件 API、团队、
3rdagent、cron、主动推荐、goal/skills/git/history），核心链路全部对齐：
4 项已实现兼容（mode 投影、同名模型响应 alias、默认 cwd 不落绑定、推荐单发）
抽查落地；agent_os 针对该客户端的 7 个历史修复全部覆盖。

本次补齐一处确凿不对齐：`command.mcp`（插件面板 MCP 连接器管理）此前 Web
通道既无本地 handler 也不在转发白名单，客户端会收到 METHOD_NOT_FOUND；
现加入 `_FORWARD_REQ_METHODS` 与 `_FORWARD_NO_LOCAL_HANDLER_METHODS`，
经 AgentServer 既有 `COMMAND_MCP` 处理器承接（与 TUI 通道同等能力，
agent_os 分支同样缺失该接线，非本次迁移回归）。

上线联调需实测的注意项：IAM `/verify` 必须回传与登录名一致的 `username`
（file-api USER_MISMATCH 校验依赖，缺失则文件接口 403）；模型列表重排后
历史缓存里的旧 `#index`/裸名选择会回退默认模型（协议固有限制，需引导客户端
刷新 models.list）；网关上传为整读内存（并发大文件需评估内存）；握手期 401
经桌面代理表现为 1011 重连（每轮重连先刷 token，可自愈）。

## 遗留项（已知不迁移 / 待决策）

- jiuwenbox 时间戳混用（`models/sandbox.py` naive 本地时间 vs 其余 UTC，
  CST 主机显示偏移 8 小时）、`SANDBOX_IP` 快照注入（沙箱内 agentserver 绑定
  IP 退化为探测）、isolated netns 孤儿清扫：均为 agent_os 侧修复，未随本次
  迁移对齐，需要时另行补齐。
- 首次注册中心注册 address 仍写 `"pending"` 占位（agent_os `b3f755c50` 改为
  留空待 placement PATCH）；消费方按 agent_os 语义适配时需注意。
- config_updater 的原分支单测（applier/client/merge/service 四件）未随代码
  移植，数据面热同步行为护栏依赖现有定向用例，补齐前结构性改动需人工回归。

## 验证与上线

本地定向验证使用项目 .venv，设置临时 `JIUWENSWARM_DATA_DIR`，避免既有自动迁移触及个人配置。
新增验证覆盖 mode 全矩阵、历史记录、默认 cwd 与绑定隔离、部署默认值优先级、配置白名单、
失败重试/新 revision 接替、策略保存与重启恢复、个人版运行时及必要的图接线。
Cron 的既有 etcd 持久化用例用于验证共享客户端迁移。
Windows 上策略编排采用注入运行时；这不等同于 Linux bubblewrap 或真实 Conch 集成验证。

模型选择补齐前已通过 20 个新增定向用例、10 个既有 Cron/etcd 用例；当时 42 个改动 Python 文件编译通过，
Ruff F/E9 与 develop 基线比较无新增问题，`git diff --check` 和部署 shell 语法检查通过。
默认模板及 Conch/Graph 模板使用部署检查阶段的地址/端口后均可解析为 YAML。

验证期间一次未隔离的导入触发了既有个人配置自动迁移：
`C:\Users\27674\.jiuwenswarm\config\config.yaml` 的版本从 0.2.7 被写成 0.2.5.beta1，
并合并缺失的默认字段。已保留 `config.yaml.agentos-verification.bak`，恢复原版本 0.2.7。
没有迁移前快照可准确区分新增字段，因此没有盲目删除默认字段。后续验证均使用临时数据目录。

上线前在测试集群验证冻结客户端的创建重试、列表/历史回显、团队删除、文件传输、推荐只展示一次；
验证 etcd 重连/compaction、存量策略更新、Conch 全生命周期及支持 Graph 的 SDK。
完成这些联调后再将 AgentOS 流量切到 develop，并废弃旧分支。本次没有执行部署或删除旧分支。
