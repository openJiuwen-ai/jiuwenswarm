# Skill 授权共享存储

多实例部署中，同一会话可能由不同的 AgentServer 处理。如果授权和活跃技能只保存在进程内存中，切换实例后就会丢失，导致已批准的技能再次要求审批，或者依赖活跃技能的工具无法执行。

Redis 后端按逻辑会话共享授权和活跃技能，使实例切换后仍能读取相同状态。它不改变会话的 Pod 路由策略，也不改变现有技能审批和工具权限规则。

## 工作原理

每个会话保存一份 JSON 文档，包括各 Agent 作用域的完整授权记录、技能身份、审批绑定、权限增量、执行窗口，以及主 Agent 的活跃技能和闲置计数。会话与 Agent 作用域共同决定授权适用范围，不能借用其他会话或其他 Agent 的授权。

审批、激活和撤销沿用原有授权状态机。更新时使用 Redis Lua 原子比较并写入（CAS）：仅当读取后的文档仍未被其他实例修改，且授权失效版本号未变化时，才提交更新。发生并发冲突时，在有限次数和时间内重试。相关键使用同一部署的 Cluster hash tag，以支持 Redis Cluster 的原子操作。

**授权失效版本号**是同一部署共享的递增整数，用于使旧授权和旧审批答案失效。例如，审批开始时记录版本号 3；关闭技能授权功能会将共享版本号递增为 4，此后版本号 3 的授权不能继续使用，迟到的审批答案也不能重新建立授权。该编号由系统维护，无需手动配置。单个技能的完成或撤销直接更新该会话的授权记录，不需要递增全局版本号。

权限检查读取共享状态，不用本地旧授权兜底。技能完成、撤销和作用域清理会同步清理相应的活跃技能；仅清理本地 Agent 缓存不会删除共享授权。删除会话会记录共享删除标记，阻止迟到回调重新创建授权。删除标记采用固定大小的 Bloom 结构，可能误拒绝新会话，但不会漏掉已记录的删除会话。

## 启用与配置位置

**企业 Kubernetes 部署工具默认自动启用 Redis 授权共享，无需填写新增配置。** AgentServer 复用部署工具已有的 Redis 地址、认证和数据库配置；授权数据的 namespace 自动由 Redis 键前缀和部署的 Kubernetes namespace 派生。同一部署的所有 AgentServer 使用相同值。需要调整时，在部署工具的 `.env.custom` 中覆盖 `SKILL_GRANT_STORE_*`；设置 `SKILL_GRANT_STORE_BACKEND=memory` 可显式关闭共享存储。

配置传递路径为：Runtime 仓库 `deploy/jiuwenswarm-ee/templates/agentserver.template.env` → AgentServer 环境变量 ConfigMap → 容器环境变量 → 启动时生成的 `config.yaml`。Redis 密码沿用现有 Secret。源码 `jiuwenswarm/resources/config.yaml` 中的配置段提供环境变量映射和通用默认值。

其他部署方式默认使用内存。若需启用 Redis，将 `skill_authorization_store` 写入 **AgentServer 加载的主配置 `config.yaml` 顶层**，与 `redis:`、`permissions:` 等配置段并列。

- 设置了 `JIUWENSWARM_CONFIG_DIR` 时，文件位置为该目录下的 `config.yaml`。例如该变量为 `/app/config`，配置文件就是 `/app/config/config.yaml`。
- 标准安装默认使用用户工作区的 `config/config.yaml`，通常为 `~/.jiuwenswarm/config/config.yaml`；自定义工作区时以实际工作区为准。
- 源码中的 `jiuwenswarm/resources/config.yaml` 提供默认配置和环境变量映射。部署时应修改 AgentServer 实际加载的配置或对应部署配置来源。

可在 AgentServer 的运行环境中确认实际配置文件路径：

```sh
python -c 'from jiuwenswarm.common.utils import get_config_file; print(get_config_file())'
```

所有 AgentServer 必须连接同一 Redis，并使用相同的授权存储 namespace。Redis 地址、认证和数据库沿用主配置中已有的 `redis:` 段。安装 Redis 客户端依赖后启用：

```sh
pip install 'jiuwenswarm[redis]'
```

```yaml
skill_authorization_store:
  backend: redis
  namespace: skill-state-production
  ttl: 604800
  operation_timeout: 0.3
```

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `backend` | 通用默认 `memory`，企业部署工具默认 `redis` | `memory` 使用进程内存；`redis` 在实例间共享状态。 |
| `namespace` | 企业部署工具自动派生；其他部署沿用 `redis.key_prefix` | Redis 授权数据的隔离标识，同一部署必须一致，不同部署应不同。它不是 Kubernetes namespace；非 Kubernetes 部署无需提供部署空间。 |
| `ttl` | `604800` | 会话授权文档的滑动有效期，单位秒，必须大于 0。默认 7 天，独立于 Runtime 路由有效期。 |
| `operation_timeout` | `0.3` | Redis 连接和 socket 操作的超时，单位秒，范围为大于 0 且不超过 5。 |

也可使用默认配置模板的环境变量映射，在 AgentServer 进程或容器的环境中设置：

| 环境变量 | 对应配置 |
| --- | --- |
| `SKILL_GRANT_STORE_BACKEND` | `skill_authorization_store.backend` |
| `SKILL_GRANT_STORE_NAMESPACE` | `skill_authorization_store.namespace` |
| `SKILL_GRANT_STORE_TTL` | `skill_authorization_store.ttl` |
| `SKILL_GRANT_STORE_OPERATION_TIMEOUT` | `skill_authorization_store.operation_timeout` |

配置中已写入固定值时，应直接修改该值，或改为模板中的环境变量引用；环境变量不会覆盖 YAML 中的固定值。

存储后端在 AgentServer 启动时安装，修改上述配置后需重启所有 AgentServer。Kubernetes 部署应修改挂载配置的 ConfigMap、部署模板或 AgentServer 容器环境变量，使重建后的 Pod 使用同一配置。不要仅修改某个容器内的临时文件。从内存切换到 Redis 不会迁移已有内存授权，已有会话需要重新加载并审批技能。

## 有效期与故障行为

每次成功读写会话授权文档都会刷新 TTL。路由过期或 Pod 重建不会直接撤销共享授权；授权文档自身到期后，不能恢复旧权限，需要重新加载并审批。TTL 应覆盖业务允许的会话等待时间。

Redis 是授权状态的可信存储，需要部署隔离、访问控制和持久化。AgentServer 启动时，若授权存储的 Redis 连接失败，会记录警告并保留原有内存后端，使技能审批仍可使用。该进程不会自动切换回 Redis；连接恢复后应重启。内存后端不具备跨实例授权和撤销一致性，应确认所有 AgentServer 均已启用 Redis。配置错误仍会明确报错，不当作连接故障处理。此回退只适用于技能授权存储，不替代 Gateway、Runtime 等组件已有的 Redis 依赖。

已经安装 Redis 后端的进程在运行中不会退回内存：读取失败不提供技能权限增量，活跃技能读取失败返回空；写入失败抛错，不能将失败当成撤销成功。这样可避免断连期间继续使用已在其他实例撤销的旧授权。连接和操作采用短超时，不自动重试传输故障。

共享授权不保存 Redis 连接凭据、可执行 Python 对象或聊天历史，也不从聊天历史中推断用户已经授权。
