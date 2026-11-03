# 在已有 ADX 集群部署 Swarm

本目录仅部署 Swarm 应用，不安装 ADX 集群，不创建集群凭据，也不准备共享存储。
独立模式不读取 AgentOS 拓扑，不依赖 IAM 或 AgentRegistry；默认只监听回环地址，
供可信本机客户端使用。客户端的 `X-User-Id` 必须对应已准备的 workspace 子目录。

具体环境变量、镜像约定、安装/启停命令及打包方式见 [README](README.md)。
`runtime.user` 为工具执行用户；镜像必须具备 AgentServer、Core provider 和 SDK，
并由集群方配置凭据、CA、可达的工具入口。跨节点需事先准备一致的 workspace 路径。

AgentOS 模式由 Enterprise 显式传入 IAM、Registry、共享存储及凭据位置，
决定 Master/VIP 的服务落点后调用同一个入口。Swarm 不维护另一份集群编排。
`SWARM_SERVICE_PREFLIGHT` 是可选的调用方启动检查，独立模式无需此参数。

停止、状态查询和卸载只解析服务身份，不要求启动配置、凭据或 CA 就绪。
绑定文件仅在服务身份使用未解析变量时读取。组合部署方可显式传入
`SWARM_HOME` 与 `SWARM_INSTANCE_NAME`（默认实例为空字符串），支持首次启动前清理。
无法确定自定义实例名时会明确报错，不会回退去停止默认实例。
