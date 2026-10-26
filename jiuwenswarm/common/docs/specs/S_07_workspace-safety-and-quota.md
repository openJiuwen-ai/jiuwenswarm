# 工作区安全与配额（workspace）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_07 / workspace-safety-and-quota |
| 关联模块 | jiuwenswarm/common/workspace/（4 文件） |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管 **会话工作区的文件安全（zone 分区 / 删除安全链 / 预览下载）与存储配额门禁**。
本域是 common 内**破坏力最高**的领域——删除链一旦放宽即可能抹掉用户数据。目录布局的
路径解析归 [[S_01]] utils / [[S_08]] path_provider；配额的 DB 策略存储与对账任务在
server 侧（本 spec 只管 common 内的判定与门禁核心）。

## 不变量

- **N1 zone 九分区枚举固定**：SESSION_WORKSPACE / SESSION_TODO / SESSION_META / SKILLS /
  ROLE_MD / MEMORY / RUNTIME / ARTIFACT / OTHER。分区由 12 条 `_PREFIX_RULES` 最长前缀
  匹配 + `_ROLE_MD_NAMES`（AGENT.md / SOUL.md / IDENTITY.md / HEARTBEAT.md / USER.md）
  判定；新前缀必须映射到既有 zone 之一。
- **N2 删除许可是白名单**：`DELETABLE_ZONES` 仅含 SESSION_WORKSPACE / SESSION_TODO /
  ARTIFACT。**扩删除面 = 修改本白名单 + 本 spec 修订 + 全场景矩阵回归**，没有其它路径。
  ROLE_MD / MEMORY / SKILLS 永不可删。
- **N3 词法校验前置**：`normalize_relative_path` 拒绝绝对路径、`..` 穿越、NUL 字符；
  校验发生在任何 IO 之前。
- **N4 symlink 只 unlink 不 follow**：删除链遇 symlink 仅摘链接本体，绝不递归进入
  目标——防 TOCTOU 与逃逸。
- **N5 resolve 后二次校验 + 预算**：路径 resolve 之后再次校验仍在租户根内；删除深度
  上限 32、条目预算 5000（防符号链接环与超深树拖死请求）。
- **N6 配额默认关闭、最终一致**：`WORKSPACE_QUOTA_ENABLED` 默认 False；用量异步对账
  （server 侧 usage_reconciler），门禁判定基于可能滞后的缓存——**宁可少拒不可错删**，
  但写入拦截本身必须同步生效。
- **N7 配额语义**：`UNLIMITED_LIMIT_BYTES = -1`（无限）/ `0`（零配额）/ 策略缺失
  fallback 10 TiB；soft 阈 80%（仅观测）/ hard 阈 100%（拦截）；`_evaluate_write`：
  `used + add >= hard` 即拒，抛 `WorkspaceQuotaExceeded`。
- **N8 身份绑定三 ContextVar**：quota identity 经 bind/reset_quota_identity 成对绑定；
  DB 策略缓存 TTL 2s + 用量缓存。**绑定必须成对**——漏 reset 会跨请求串租户。
- **N9 租户根三级解析**：`resolve_tenant_root` 的解析顺序（ContextVar 绑定 →
  全局配置 → 默认布局）是租户隔离的锚点。
- **N10 WorkspaceError.code 对齐 HTTP error.code**：错误码是 API 契约，改码即破坏
  前端错误处理。
- **N11 读取边界**：preview 截断 64 KiB + 二进制检测（不把二进制喂给文本通道）；
  download 上限 100 MiB + zip 跳过符号链接；`list_tree` 的目录 `size_bytes=None`
  懒加载（不递归统计）；`measure_used_bytes` 优先 `/usr/bin/du`（口径与系统一致）。

## 接口契约

| 模块 | 关键符号 | 语义要点 |
|---|---|---|
| `zones.py` | `WorkspaceZone`（9）、`DELETABLE_ZONES`、`_PREFIX_RULES`（12）、`_ROLE_MD_NAMES`、`normalize_relative_path`、zone 判定函数 | N1–N3 |
| `service.py` | `WorkspaceError`、`WorkspaceService`（frozen）：`resolve`（双重校验）/ `list_tree` / `delete_entries`（四层链）/ `preview` / `download` | N4 / N5 / N10 / N11 |
| `quota.py` | `check_workspace_write`（门禁）/ `WorkspaceQuotaExceeded` / `bind/reset_quota_identity` / `resolve_tenant_root` / `_evaluate_write` | N6–N9 |

**删除四层安全链**（顺序即契约）：

```
zone 白名单（N2）→ 词法校验（N3）→ symlink 只 unlink（N4）→ resolve 二次校验 + 深度/条目预算（N5）
```

## 执行路径 / 消费者

- gateway：`workspace/quota.py`、`web_method_register/workspace.py`、`file_http.py`
  （写入门禁挂接）。
- server：`handlers/workspace.py`、`bootstrap.py`（身份绑定）、`usage_reconciler`
  （对账）、`policy_reload`（策略热更）、`fs_quota_guard`、`interface.py`（多处）。
- agents：`workspace_quota_rail`（agent 工具调用侧的配额护栏）。

## 边界 case 清单

| 场景 | 现行为 | 风险 / 测试 |
|---|---|---|
| 删除 ROLE_MD zone 内文件 | 白名单拒绝 | `test_workspace_path_delete.py` |
| `..` 穿越 / 绝对路径 / NUL | 词法层拒绝 | 同上 |
| symlink 指向租户根外 | 只 unlink 本体 | 同上 |
| resolve 后逃逸（TOCTOU） | 二次校验拒绝 | 同上 |
| 超深目录 / 超多条目 | 深度 32 / 条目 5000 预算中止 | 同上 |
| 配额 used 缓存滞后 | 最终一致（N6），写入门禁仍同步拦截 | server 侧对账测试 |
| `-1` / `0` / 缺失策略 | 无限 / 零配额 / 10 TiB fallback | quota 单测 |
| 多租户并发 | ContextVar 身份隔离 | bind/reset 成对性测试 |

## 修改检查单

- **改任何删除链环节 → 必须跑全场景矩阵**（上表全行 + 多租户隔离），先红后绿。
- 扩 DELETABLE_ZONES → 本 spec N2 修订 + 安全评审（用户数据损失面评估）。
- 改配额阈值/语义 → N7 数值语义保持（-1/0/fallback）+ soft/hard 行为矩阵回归。
- 改 zone 前缀规则 → 最长前缀匹配顺序回归（新增前缀不得遮蔽既有更长的前缀）。
- 改 WorkspaceError 码 → 前端错误处理联动（N10）。

## 与其它 spec 的关系

- [[S_01]]：路径中枢（get_* 函数族）与 [[S_08]] path_provider（多租户重定向）是
  租户根解析的上游。
- [[S_14]]：cleanup 的过期会话目录清理**不经过** zone 删除链（目录级 mtime 判定，
  gateway 本地数据），两者边界勿混。
