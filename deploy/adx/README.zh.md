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

AgentOS profile 的工具入口只由节点启动器注入的
`JIUWENSWARM_ADX_NODE_INGRESS_URL` 决定。模板不保存 `sandbox.url`，Gateway
下发的 sandbox JSON 不携带地址，应用与用户 `.env` 均不持久化节点入口。
运行时才把注入值解析成传给 SDK 的 `sandbox.url`；缺失注入会明确报错。
部署控制后端类型、启用状态和 TLS 校验；用户历史 YAML 的 `url`、
`connection.server_address/gateway_address/token/use_tls` 不再覆盖当前节点连接。
凭据继续来自部署的 ADX 环境配置。个人版及其他后端沿用原来的合并方式。

重复部署保留模型、IAM、会话、有效的 ADX 参数、SSH known-hosts 和连接超时。
旧 CPU/内存、UID/GID、工作目录和绝对路径本地挂载继续使用。
`source/target/readonly` 转为 ADX 的 `host_path/target/options`；未写 target 时沿用
source，未写 readonly 时为可写，与原转换逻辑一致。
`rootfs.workdir/mounts/user` 同样可迁移。与平台一致的工作区挂载会去重，冲突会报错。

旧 Docker 镜像/executor、Jiuwenbox 模板选择器改用部署注册的 ADX 镜像与运行时，
日志仅提示字段名。自定义 Docker 镜像需先注册 ADX image binding，再显式配置
`type: adx-environment`。旧远程挂载源、其他 rootfs 字段、文件策略和命令黑名单
无法直接等价转换，会明确报错，要求迁移后再部署。部署会更新已有用户 YAML 与
连接环境文件，保留 workspace 内容、模型配置和会话数据库。

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
重复变量；低于 request 的已有非零 limit 同步抬高。非法值会在写文件前报错。
管理面在进程启动或运行时注入的 `TOOL_SANDBOX_*` 仍可覆盖 YAML，原有动态调整
语义保留。旧 YuanRong 的工具类型、镜像、executor、MGR_* 等变量不再保留。

Gateway 和工具 SDK 统一读取 `ADX_TOKEN`；`gateway.adx.token_env` 指向该变量，
不再生成值相同的 `ADX_GATEWAY_TOKEN`。旧 Gateway 配置在部署时同步更新。
模型与嵌入模型凭据继续保留在 `.env`，未知业务变量保留。

Gateway/Web 的 host、port 来自部署 settings，由启动器传入进程；应用 `.env`
中的旧端口变量会清除，避免覆盖部署选择。`AGENTOS_BUILTIN_AGENT_CPU/MEMORY`
仍由部署 settings 转换成进程变量，因为 Gateway 的资源读取方需要这些变量。
CA 和 `ADX_TOKEN` 继续保留在 `.env`。节点地址只保留启动器的运行时注入。

`/opt/adx/config/deployment.env` 则属于 ADX 部署绑定文件：它展开原生部署 YAML，
生成组件实际读取的 JSON；Ingress 的 HTTPS 监听和证书还直接读取组件环境变量。
这些输入和生成结果相同不属于两份独立可编辑配置，不能简单删掉绑定变量。
