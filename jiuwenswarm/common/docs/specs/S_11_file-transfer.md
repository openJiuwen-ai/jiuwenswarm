# 文件分块传输

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_11 / file-transfer |
| 关联模块 | jiuwenswarm/common/{file_transfer_config, file_transfer_types}.py |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管 **Gateway ↔ AgentServer 之间的文件分块传输**：配置模型与启用开关
（file_transfer_config）、两侧共享的数据类型与纯工具（file_transfer_types）。传输在
E2A 协议上的方法域与 kind 常量归 [[S_02]]；工作区文件的安全删除 / 下载边界归
[[S_07]]。

## 不变量

- **N1 默认关闭**：`enabled` 默认 False（企业下载主路径为 OBS）；YAML
  `file_transfer.enabled` 显式值优先；env `JIUWENSWARM_FILE_DOWNLOAD_VIA_PUSH` 为
  逃生开关。启用与否带进程缓存（`clear_file_transfer_config_cache` 失效）。
- **N2 默认参数**：chunk 64 KB / max 100 MB / 超时 300s / 并发 5。调阈值需评估
  AgentServer 内存暂存压力（`TransferProgress.chunks: dict[int, bytes]` 全量在内存）。
- **N3 safe_filename 白名单字符集**：字母数字 + `._- `——防路径遍历与控制字符；
  mime 缺省 `application/octet-stream`。
- **N4 类型两侧共享**：`FileTransferStartParams` / `TransferProgress` 定义在 common，
  gateway 与 server 各自实现传输编排，但**不得各自复写这两类**。

## 接口契约

| 模块 | 关键符号 | 语义要点 |
|---|---|---|
| `file_transfer_config.py` | `FileTransferConfig` / `get_file_transfer_config` / `resolve_file_transfer_enabled` / `clear_file_transfer_config_cache` | N1 / N2 |
| `file_transfer_types.py` | `FileTransferStartParams` / `TransferProgress` / `safe_filename` / `guess_mime_type` | N3 / N4 |

## 执行路径 / 消费者

- gateway：`file_transfer_handler`、`routing/agent_client`、
  `message_handler/file_transfer_mixin`。
- server：`file_transfer_manager`、`handlers/file_transfer`。
- agents：`agents/harness/common/tools/send_file_to_user`。

## 修改检查单

- 改默认阈值 → N2 四值成对评估（内存 / 超时 / 并发联动）+ 两侧（gateway / server）
  确认一致性。
- 改 `safe_filename` 字符集 → 路径遍历回归（`..` / 分隔符 / 控制字符 / Unicode 同形）。
- 改启用开关优先级 → YAML 显式值 / env 逃生 / 默认 False 三序回归。

## 与其它 spec 的关系

- [[S_02]]：`FILE_TRANSFER_*` 常量（E2A kind）与传输消息的协议承载。
- [[S_07]]：落盘侧的工作区边界。
