# Config layers and upgrades

For developers adding or changing config keys. End-user panel fields: [Configuration](Configuration.md).

Live config lives only under the user data root (`JIUWENSWARM_DATA_DIR/config/`). Repo / installer `jiuwenswarm/resources/` is a **copy source**; `get_config()` does not read the package. Key names and template defaults are not renamed for the split. No stamp file. `.env` is not split.

XiaoYi Work paths: production/dev `%APPDATA%\xiaoyiwork\users\<uidKey>\jiuwenswarm\config\`; debug flavor uses `xiaoyiWorkDebug`.

| Term | Location |
|---|---|
| Template | `jiuwenswarm/resources/config.yaml`, `builtin_rules.yaml` |
| System yaml | User `config.yaml` (template copy; may be whole-file overwritten) |
| Overlay | User `config.user.yaml` (sparse; survives upgrades) |

Runtime view = system yaml ⊕ overlay (sparse merge). Do not dump the merged view back onto the system file.

**Force overwrite** compares whole-file bytes (not per-key merge) on Agent start (`maybe_extract_user_overlay`) and `prepare_workspace`. Overlay and `.env` are never overwritten this way.

**Allowlist** (extract + keep across upgrades): `_SCALAR_PATHS` in `jiuwenswarm/common/config_split.py`. `permissions` currently stays on the system file; persist must use `follow_overlay=False`.

How to add a key, write-path table, merge rules, and the XiaoYi startup sequence: see the Chinese doc [配置分层与升级](../zh/配置分层与升级.md) (source of truth; keep both in sync when changing behavior).
