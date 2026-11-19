# Prepared Local launch specifications / 已准备的 Local 启动规格

Third-party launch-spec writers prepare offline images and merge image defaults
before registration. The Gateway reads the registered source and performs the
existing ADX protocol mapping. It does not resolve image names through a catalog.

三方 launch-spec 写入端在注册前准备离线镜像，并合并镜像启动默认值。Gateway 使用注册的来源，
完成已有的 ADX 协议字段转换，不根据镜像名查询目录或改写来源。

```yaml
runtime_spec:
  sandbox_type: runsc
  rootfs:
    type: local
    path: /var/lib/agentos/local-images/objects/local-IMAGE_ID/rootfs
    user: '1000:1000'
  cmds: [[python3, /app/server.py]]
  code_path: /app
env_vars:
  PATH: /usr/local/bin:/usr/bin:/bin
```

`cmds`, `code_path` and `rootfs.user` map to the existing ADX process fields.
`env_vars` supplies the final environment. Local paths must exist on eligible
execution nodes. An Image source continues to use the existing Image request.
ADX enforces read-only access for explicit Local rootfs. Upgrade the ADX API
server before deploying the Gateway and tool adapters that rely on this policy.

`cmds`、`code_path`、`rootfs.user` 对应已有 ADX process 字段；`env_vars` 提供完整环境变量。
Local 路径需在候选执行节点上存在。Image 来源继续使用原有 Image 创建请求。
显式 Local rootfs 的只读模式由 ADX 强制执行。先升级 ADX API Server，再更新 Gateway 和工具适配。

Enterprise resolves builtin Local images into `gateway.adx.builtin_launch.rootfs`
and explicit launch settings. Tool sources are persisted in each workspace's
`sandbox.rootfs` and consumed by the existing Core provider. The obsolete
`gateway.adx.local_images` key is accepted for upgrades and ignored.

Enterprise 将内置 Local 镜像写入 `gateway.adx.builtin_launch.rootfs` 及显式启动参数。
工具来源保存在用户 workspace 的 `sandbox.rootfs`，由既有 Core provider 使用。
旧 `gateway.adx.local_images` 仅接受升级读取兼容，不参与创建。


## Upgrade and environment values / 升级与环境值

For an existing offline registration that only contains an image reference, its
launch-spec writer must publish a complete Local specification before the new
Gateway creates that Agent. Retaining `gateway.adx.local_images` alone does not
perform this migration. A registration intentionally using Image still follows
the Image path and requires that source to be available.

The `sandbox.rootfs_template` setting is removed. Configure
`sandbox.rootfs: {type: local, path: /absolute/node/rootfs}` directly. A template
name is not converted to a path by the Gateway or Core provider.

Enterprise stores literal image defaults separately as `image_env` in the tool
sandbox or builtin launch configuration. Configuration loading merges them into
`env` after expanding user placeholders; explicit user `env` values take
precedence. The final ADX request carries ordinary `env` values.

已有离线注册若只有镜像名，应由 launch-spec 写入端先发布完整 Local 规格，再通过新
Gateway 创建该 Agent。仅保留 `gateway.adx.local_images` 不会完成注册数据迁移。
显式 Image 来源仍走 Image 创建链路，需要其来源可用。

`sandbox.rootfs_template` 已移除，请直接配置
`sandbox.rootfs: {type: local, path: /absolute/node/rootfs}`。
Gateway 与 Core 不负责将模板名转换为路径。

部署生成的镜像默认环境单独保存在 `image_env`；配置加载时先展开用户占位符，再按字面值
合入镜像默认环境，用户显式 `env` 优先。ADX 收到的仍是普通 `env`。
