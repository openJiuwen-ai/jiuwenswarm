# Prepared Local launch specifications

Third-party launch-spec writers prepare offline images and merge image defaults
before registration. The Gateway maps the registered source directly to the ADX
creation request.

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

Enterprise resolves builtin Local images into `gateway.adx.builtin_launch.rootfs`
and explicit launch settings. Tool sources are persisted in each workspace's
`sandbox.rootfs` and consumed by the existing Core provider.

## Upgrade and environment variables

For an existing offline registration that only contains an image reference, its
launch-spec writer must publish a complete Local specification before the new
Gateway creates that Agent. The obsolete `gateway.adx.local_images` key is
accepted during upgrades. Agent creation uses the registered source, so Image
registrations require the configured image source to be available.

The `sandbox.rootfs_template` setting is removed. Configure
`sandbox.rootfs: {type: local, path: /absolute/node/rootfs}` directly. A template
name is not converted to a path by the Gateway or Core provider.

Enterprise stores literal image defaults in `image_env` under `sandbox` and
`gateway.adx.builtin_launch`. For each section, configuration loading expands
placeholders in the explicit `env` and combines it with the literal image
defaults. Explicit `env` values take precedence. The final ADX request carries
the combined values as `env`.
