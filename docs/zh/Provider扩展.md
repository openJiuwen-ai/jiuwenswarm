# PathProvider 与 ConfigProvider 扩展

JiuwenSwarm 提供两个进程级 SPI，用于按业务替换工作区路径或配置来源。
未注册 Provider，或 Provider 对某一类别返回 `None` 时，继续使用内置默认实现。

完整示例见 `examples/custom_path_config_provider.py`。

## PathProvider

实现 `jiuwenswarm.common.path_provider.PathProvider` 后，通过扩展注册：

```python
async def register_extensions(registry):
    extension = MyPathProviderExtension()
    registry.register_path_provider(extension)
    return [extension]
```

Provider 可选择性覆盖单一路径类别，也可通过
`build_workspace_directories()` 替换或追加 openjiuwen `Workspace` 节点。
路径优先级为：显式调用参数或构造参数、Provider、内置默认实现。

`PathContext` 提供当前 `service_id`、`agent_id`、`workspace_key`、
`session_id` 及已绑定的租户路径，Provider 应据此保持租户隔离。
Provider 抛出异常时宿主记录 warning 并回退默认路径。

扩展必须在首个 checkpointer 创建前完成 `CHECKPOINT` 注册。AgentServer
会先加载扩展再初始化 AgentManager，因此标准启动链路满足该约束；
运行期间替换 Provider 不会迁移已经创建的 checkpointer。

当 `build_workspace_directories()` 返回绝对节点路径时，应由业务预先创建目录，
并避免让当前 openjiuwen `DirectoryBuilder` 自动创建该节点。

## ConfigProvider

实现 `jiuwenswarm.common.config_provider.ConfigProvider` 后，通过扩展注册：

```python
async def register_extensions(registry):
    extension = MyConfigProviderExtension()
    registry.register_config_provider(extension)
    return [extension]
```

`get_process_config()` 用于同步返回进程级配置快照。返回稀疏字典时，宿主会补齐
发布模板、解析环境变量并执行现有配置归一化；返回 `None` 或抛出异常时继续读取
`config.yaml`。

`load_agent_config()` 用于异步返回 agent 级配置。Provider 返回 `None` 或抛出
异常时，宿主回退内置实现。内置实现通过以下选择器决定是否叠加企业策略：

1. 环境变量 `JIUWENSWARM_CONFIG_SOURCE`；
2. `config.yaml` 顶层 `config.source`；
3. 未显式配置时，企业版使用 `enterprise`，其他版本使用 `yaml`。

可选值为 `yaml` 和 `enterprise`。社区版显式选择 `enterprise` 时会记录 warning
并回退 `yaml`；企业版可显式选择 `yaml`，用于只使用本地配置。

`set_config()` 仅在 Provider 提供 `write_process_config(config)` 时写回 Provider；
否则记录 warning 并保持原有的本地 `config.yaml` 写回行为。
