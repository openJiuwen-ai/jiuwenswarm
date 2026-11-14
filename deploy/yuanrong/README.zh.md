# Gateway/Web 部署

`deploy/yuanrong/` 保留 Swarm 应用安装和 Gateway/Web 启停脚本，
`deploy/adx/deploy.sh` 与 `package.sh` 转发到这里，安装路径仍是 `deploy/jiuwenswarm/`。

启动前由调用方准备 `<gateway.home>/.jiuwenswarm/config/` 下的 YAML 和 `.env`。
AgentOS 的配置生成、历史 workspace 迁移、新用户模板和手动刷新工具统一由
Enterprise 提供；Swarm 读取准备好的配置，运行时接入当前节点地址和凭据。

`settings.yaml` 仅包含服务身份、监听地址和端口；个人部署默认使用回环地址。
`service.py` 只读这些字段。`up` 保留运行中的服务，修改端口后用 `restart` 生效。
停止、状态查询和卸载只解析服务身份，不要求 ADX 凭据及启动配置就绪。

安装、启停和打包命令见 [README](README.md)。
