# 在已有 ADX 集群部署 Swarm

本目录仅部署 Swarm 应用，不安装 ADX 集群，不创建集群凭据，也不准备共享存储。
独立模式不读取 AgentOS 拓扑，不依赖 IAM 或 AgentRegistry；默认只监听回环地址，
供可信本机客户端使用。客户端的 `X-User-Id` 必须对应已准备的 workspace 子目录。

具体环境变量、镜像约定、安装/启停命令及打包方式见 [README](README.md)。
`runtime.user` 为工具执行用户；镜像必须具备 AgentServer、Core provider 和 SDK，
并由集群方配置凭据、CA、可达的工具入口。跨节点需事先准备一致的 workspace 路径。
工具沙箱默认的 CPU 和内存请求与上限均为 2 核和 4096 MiB。

AgentOS 模式由 Enterprise 显式传入 IAM、Registry、共享存储及凭据位置，
决定 Master/VIP 的服务落点后调用同一个入口。Swarm 不维护另一份集群编排。
`SWARM_SERVICE_PREFLIGHT` 是可选的调用方启动检查，独立模式无需此参数。

停止、状态查询和卸载只解析服务身份，不要求启动配置、凭据或 CA 就绪。
绑定文件仅在服务身份使用未解析变量时读取。组合部署方可显式传入
`SWARM_HOME` 与 `SWARM_INSTANCE_NAME`（默认实例为空字符串），支持首次启动前清理。
无法确定自定义实例名时会明确报错，不会回退去停止默认实例。

### 托管工具配置与旧 workspace

生成的 `sandbox` 仅保留 ADX 工具配置。ADX 负责工具环境生命周期，无需配置
`startup_mode`；旧的 `internal/external` 值仍可读取，ADX 统一使用外部生命周期。

AgentOS 可通过 `JIUWENSWARM_ADX_NODE_CONTEXT_FILE` 指定只读节点上下文；未配置文件时
沿用启动器注入的 `JIUWENSWARM_ADX_NODE_INGRESS_URL`。共享 `.env` 不保存节点入口。
运行时读取目标节点的有效地址，部署控制后端、运行时、协议及凭据，旧用户 URL 和
连接配置不能覆盖。文件或注入值缺失会明确报错。个人版及其他后端沿用原来的合并方式。

重复部署保留模型、IAM、会话、有效的 ADX 参数、SSH known-hosts 和连接超时。
旧 CPU/内存、工作目录和绝对路径本地挂载继续使用；身份按部署运行时检查。
`source/target/readonly` 转为 ADX 的 `host_path/target/options`；未写 target 时沿用
source，未写 readonly 时为可写，与原转换逻辑一致。
`rootfs.workdir/mounts/user` 同样可迁移。与平台一致的工作区挂载会去重，冲突会报错。

旧 Docker 镜像/executor、Jiuwenbox 模板选择器改用部署注册的 ADX 镜像与运行时，
日志仅提示字段名。自定义镜像须为所选 ADX 后端支持的镜像，并显式配置
`type: adx-environment`。旧远程挂载源、其他 rootfs 字段、文件策略和命令黑名单
无法直接等价转换，会明确报错。AgentOS 可设置 `update_user_configs: false`，校验并保留
用户 YAML 原文件，通过内存叠加部署配置生效，仅清理用户环境文件中的部署凭据。
workspace 内容、模型配置和会话数据库保留。

不再生成没有读取方的 `checkpoint.sqlite_journal_mode`、
`workspace_sqlite.journal_mode` 和旧 SSH `user_template`。
共享存储上实际生效的 Team SQLite `db_enable_wal/mmap_size_mb` 继续保留。
AgentOS 不向应用或用户 `.env` 持久化节点地址；旧的
`persist_user_node_ingress_url` 参数仍可用于 standalone 部署。

管理面注入的 `TOOL_SANDBOX_CPU/MEMORY/IDLE_TIMEOUT` 继续优先于用户 YAML。
用户资源配置作为回退值；管理面调整 request 时，低于 request 的历史 limit
同步抬高，较高的用户 limit 继续保留。

### YAML 与 .env 的唯一来源

ADX 部署的持久化工具参数以 `config.yaml::sandbox` 为准。升级时先把旧 `.env`
中实际生效的 `TOOL_SANDBOX_CPU/MEMORY/IDLE_TIMEOUT` 转存到 YAML，再删除这些
重复变量；保留用户 YAML 时也保留其旧资源环境覆盖值，直到显式迁移。低于 request 的已有非零 limit 同步抬高。非法值会在写文件前报错。
管理面在进程启动或运行时注入的 `TOOL_SANDBOX_*` 仍可覆盖 YAML，原有动态调整
语义保留。旧 YuanRong 的工具类型、镜像、executor、MGR_* 等变量不再保留。

Gateway 和 AgentServer 内的工具 SDK 读取 `ADX_TOKEN`；工具沙箱不继承该变量。`gateway.adx.token_env` 指向该变量，
不再生成值相同的 `ADX_GATEWAY_TOKEN`。旧 Gateway 配置在部署时同步更新。
模型与嵌入模型凭据继续保留在 `.env`，未知业务变量保留。

Gateway/Web 的 host、port 来自部署 settings，同时生成到应用 `.env`，供 CLI、
Web 和升级入口读取。AgentOS 启动器已注入的端口优先于 `.env`，避免旧值覆盖
当前部署选择；这些变量本身仍有效。旧变量清理仅作用于 AgentOS 托管配置，
standalone 保留已有环境输入与凭据别名。`AGENTOS_BUILTIN_AGENT_CPU/MEMORY`
仍由部署 settings 转换成进程变量，因为 Gateway 的资源读取方需要这些变量。
CA 设置保留；`ADX_TOKEN` 仅保留在 Gateway 服务 `.env`，用户共享 `.env` 不保存部署凭据。节点地址只保留启动器的运行时注入。

`/opt/adx/config/deployment.env` 则属于 ADX 部署绑定文件：它展开原生部署 YAML，
生成组件实际读取的 JSON；Ingress 的监听协议和证书由组件环境配置决定。
这些输入和生成结果相同不属于两份独立可编辑配置，不能简单删掉绑定变量。

### runsc AgentServer 与工具环境

AgentOS 分别配置 AgentServer 和工具运行时。选择 `agent_runtime: runsc` 时，部署生成 `gateway.adx.builtin_launch`，明确 Python 启动命令、宿主 Python/动态库/CA 的只读挂载及节点上下文文件。跨节点须保证 Python 和挂载布局一致。

配置 `JIUWENSWARM_ADX_NODE_CONTEXT_FILE` 后，运行时从只读 JSON 中读取 `ingress_url`、`node_id`；Core 每次获取工具环境前重新读取，节点变化时重建 SDK 连接作用域。文件缺失或无效时报错。未配置该文件时仍支持原启动器注入的节点 URL。部署可显式使用可信回环/网桥 HTTP 与 WS；HTTPS 与 WSS 继续校验证书。历史 URL、token、连接协议及运行时不能覆盖受管部署值。

部署 token 仅保留在 Gateway 服务配置，通过 `JIUWENSWARM_ADX_API_TOKEN` 注入 AgentServer，由入口在读取用户 dotenv 前转换为 SDK 的 `ADX_TOKEN`；不写入用户共享 `.env`，也不注入工具沙箱。AgentServer 按有效用户模型配置及 `.env` 回退提取目标地址；内网模型须显式允许。工具默认拒绝流量和 DNS，可通过 `network.tool_origins` 指定联网目标；请求仍受节点网络权限上限限制。

AgentOS 可设置 `update_user_configs: false`：校验旧配置，保留用户 YAML 原文件，通过内存叠加生效；仅清理用户 `.env` 中的部署凭据。runsc 的身份来自 OCI 配置，不兼容的旧 user/group 或 Conch 专属配置须明确迁移。旧网络拒绝规则转换保留，资源、目录和可转换挂载继续沿用。共享部署 token 不提供逐用户资源授权。

`network.ingress_policy_peer` 对应 ADX 节点基线中的逻辑 peer，由各目标节点映射到本机网桥；恢复请求保留该 peer，连接地址继续从目标节点上下文文件读取。模型策略更新使用生命周期操作头 `X-ADX-Request-ID`。未配置模型时不授予模型网络权限，也不继承 Gateway 自身的模型地址。旧 `agentos-tools:v1` 是托管 Conch 绑定，切换 runsc 时跟随部署镜像；其他显式 OCI 自定义镜像继续保留。

runsc AgentServer 的 Environment 空闲删除和 checkpoint 保留由 ADX 管理。Gateway 在创建时将 Agent 空闲超时写入 `idleTimeoutSeconds`；后续超时配置变更对新建 Environment 生效。Gateway 的空闲/断连清理只释放本地路由状态并关闭 Relay 活动，不删除原生 Environment。业务传输异常也保留 Running、Pausing、Paused、Resuming 及等待重试的执行；确认终态失败后仍可使用原清理流程。用户显式删除继续回收 Environment 及其 checkpoint。工具沙箱的空闲超时独立配置。

受管 runsc AgentServer 恢复后，若节点上下文、沙箱地址或原生 ADX runtime ID
变化，Front 会先发布新的 Gateway 推送连接，再关闭旧连接并取消旧连接所属的
请求流。驻留 Team producer 和历史保留，Web 客户端按原协议重新订阅 Team。
runtime ID 读取现有 runsc `/proc/gvisor/spec_environ` 接口，同节点、同 IP
恢复也能识别新执行实例。未配置受管节点上下文时，Front 保持原有多连接行为。

Gateway 重建使用按代次区分的原生 Environment 名称，并通过 `env_vars` 核对代次后发布新实例。DELETE 结果未知时，必须等原生查询确认旧实例不存在才能重建。Gateway 重启后，runsc 路由通过注册记录查找对应原生实例，复用 Running/Paused 实例，并兼容旧逻辑 ID；原生实例确实不存在时允许创建。注册元数据不作为实例存活依据。
