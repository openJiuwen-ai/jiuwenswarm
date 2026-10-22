# 敏感信息安全体系（secrets）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_04 / secrets |
| 关联模块 | jiuwenswarm/common/secrets/（含 persistence/ 与 providers/，14 文件） |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管 **敏感配置项（api_key / token / 密码等）的存储与加解密体系**：四层架构
（门面 → 注册表 → 变换 → 持久化）+ 算法提供方。环境变量的双轨存储与敏感名判定的
权威在 `local_env_config`（[[S_08]]）；传输层安全（mTLS）归 [[S_05]]。

```
L1  store.SecretStore        门面：get/set/delete 明文进出；get_instance() 单例；build_default() 装配链
L2  registry.SecretRegistry 路由：secret_registry.yaml（storage/location/field）→ 选介质适配器
L3  transform               变换：encode/decode 决策树（信封 / legacy / 明文）
L4  persistence.*            介质适配器：env / file / default_file / db(stub) + gateway 分发
    providers.*             算法提供方：AES-256-GCM / X25519 DEK
```

## 不变量

- **N1 明文只存在于内存与 get/set 边界**。落盘前必经 transform 决策树；任何持久化介质
  里都不允许出现未决策的明文敏感值。
- **N2 信封格式固定**：`ENC:v1:<algorithm>:<wrap_b64>:<payload_b64>`（`envelope.py` 的
  `PREFIX = "ENC:v1:"`）。解析与生成都走 envelope 模块，禁止手工拼串。
- **N3 encode 决策树**（`transform.py`）：空值直存 → 显式 `algorithm` → 信封 →
  legacy 敏感名 + 已注册 custom crypto → Provider 加密 → 其余明文。三支顺序不可换。
- **N4 解密链全部 fail-open 返回原文**：解不开的值按明文返回（用户手填的明文密钥是
  合法输入）。**这是有意为之**——密钥体系坏了不能弄坏整个配置加载；但 encode 侧
  决策树必须严格（N3）。
- **N5 注册表双层合并**：bundled `resources/secret_registry.yaml`（约 60 条）为基线，
  用户 `config_dir/secret_registry.yaml` **同 key 覆盖**；企业版差异文件
  `secret_registry.enterprise.yaml` 单独维护。
- **N6 路径解析优先级**（`resolve_file_path`）：`workspace/` 前缀 → 绝对路径 → `~` →
  `/` → config_dir 相对。变更顺序属部署破坏性变更。
- **N7 点路径寻址受限**（`persistence/_dotted.py`）：仅 dict 下钻、无转义、**不支持
  列表下标**——文件介质的 field 定位能力是有意收窄的。
- **N8 AES 主密钥解析链**：env `JIUWEN_SECRET_MASTER_KEY` → 文件 `.master_key`；值格式
  b64-32B / utf-8 / sha256 派生 三档兼容；`wrap_b64 = "-"`（无包装密钥场景）。
  cryptography 为惰性 import（缺依赖时相关算法不可用但不炸包）。
- **N9 初始化 API 当前未接线**：`configure_aes256gcm` / `configure_dek` /
  `register_custom_crypto` / `bridge_legacy_extension_crypto` 在包内**无调用点**——生产
  装配尚未启用自定义加密，默认链实际是"legacy 敏感名 → 无 custom crypto → 明文"。
  接线时必须同步修订本条与 [[S_08]] 的敏感名加密路径。
- **N10 persistence.db 是 stub**：`NotImplementedError`；DB 介质是预留能力。
- **N11 介质间无跨介质 fallback**：`PersistenceGateway` 按注册表路由精确分发——找不到
  适配器即失败，不做"顺便写别处"的兜底。

## 接口契约

| 模块 | 关键符号 | 语义要点 |
|---|---|---|
| `store.py` | `SecretStore.get/set/delete`（明文进出）、`get_instance()`、`build_default()`、初始化 API（N9） | 业务侧唯一入口；`secrets/__init__` 仅导出 `SecretStore` |
| `registry.py` | `StorageLocation` / `DefaultLocation`、`SecretRegistry`、`resolve_file_path`、`derive_legacy_name` | N5 / N6 |
| `transform.py` | `encode_value` / `decode_value`（决策树） | N3 / N4 |
| `envelope.py` | `PREFIX`、信封解析/生成 | N2 |
| `legacy.py` | 敏感名判定（委托 `local_env_config.is_sensitive_env_name`：api_key / token / secret / header 或 == default_headers） | 单一真相源在 [[S_08]] |
| `providers/__init__.py` | `Aes256GcmAlgorithm`（32B 主密钥）、`DekAlgorithm`（X25519 ECDH + AESGCM 双层） | N8 |
| `persistence/gateway.py` | `PersistenceGateway`（分发） | N11 |
| `persistence/env.py` | `EnvMediumAdapter`（.env 单变量；`set_os_environ` 写 **Track B tip** 而非 os.environ，见 [[S_08]]） | |
| `persistence/file.py` | `FileMediumAdapter`（yaml / json / text；dotted field；非 str 值 json.dumps 返回） | N7 |
| `persistence/default_file.py` | `DefaultFileStorageBackend`（`secrets_store.json`；tmp + `os.replace` 原子写） | |
| `persistence/db.py` | stub（N10） | |

## 数据结构

- 注册表条目：key → storage（介质）/ location（文件路径或 env 名）/ field（dotted 路径）。
- 信封 payload：算法特定的密文 + 认证标签（GCM）；wrap 段承载 DEK 场景的包装密钥。
- `secrets_store.json`：默认文件介质的落盘文件，原子写保证半写不损坏。

## 消费者

包外唯一生产消费方：`gateway/a2a_manager/outbound/credentials.py`
（`A2AOutboundCredentialStore`）。其余潜在消费（模型 api_key 加解密）经
`local_env_config` 的敏感名通道间接进入（[[S_08]]）。

## 修改检查单

- 加敏感配置项 → `secret_registry.yaml`（bundled）登记 + `legacy.py` 敏感名判定确认 +
  （企业差异）enterprise yaml 同步。
- 改信封格式 → 版本号递增（`ENC:v2:`）+ 旧格式 decode 兼容 + [[S_08]] 出口加密联动。
- 改决策树 → `tests/unit_tests/common/test_secret_store.py` +
  `test_secret_envelope.py` 全量回归（encode 严格 / decode fail-open 两侧都要测）。
- 接线初始化 API（N9）→ 修订本 spec + 装配点登记（[[S_01]] 执行路径表）+ 主密钥
  生命周期说明（生成 / 轮换 / 丢失恢复）。

## 与其它 spec 的关系

- [[S_08]]：`local_env_config` 的敏感名加密出口（ExtensionRegistry crypto 无 provider 时
  明文直通）与 `EnvMediumAdapter.set_os_environ` 的 tip 语义。
- [[S_05]]：`base_crypto.CryptoProvider` Protocol 是本体系算法提供方的协议面（当前实际
  容器是 ExtensionRegistry，见 [[S_05]] N4）。
