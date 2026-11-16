---
id: S_01_authorized-file-download
title: 授权文件下载规约
type: S
author: nypdmax
author_email: chengkunck@gmail.com
created: 2026-11-14
updated: 2026-11-16
modules:
  - jiuwenswarm/common
  - jiuwenswarm/agents/harness/common/tools
  - jiuwenswarm/server/runtime/gateway_adapter
files:
  - jiuwenswarm/common/verified_download_assets.py
  - jiuwenswarm/common/file_download_token.py
  - jiuwenswarm/agents/harness/common/tools/send_file_to_user.py
  - jiuwenswarm/server/runtime/gateway_adapter/workspace_file_adapter.py
---

# 授权文件下载规约

## 适用范围与责任

非企业模式下，要求精确执行授权的 `send_file_to_user` 在消费本次路径及目标授权后，登记实际交付路径的原文件引用。普通模式、企业 OBS 和旧正期限附件保持既有分支。

`VerifiedDownloadAssetOwner` 负责登记及旧快照副本的生命周期；原文件字节仍由其原有文件所有者管理。登记、HMAC、会话及原执行授权分别承担原有职责，历史记录不授予下载权限或恢复一次性 grant。

## 登记、令牌与指纹

- 引用以内部 `stage(reference_only=True, expires_at=0)` 显式创建，原调用默认仍创建有期限快照。引用源不能位于 Owner 私有缓存目录内，须在任何 prune 前拒绝；沿用普通文件及现有符号链接检查。
- 引用只读取文件并计算 SHA-256，不创建字节副本、不修改源权限。`sealed_path` 这个兼容属性名在引用模式承载原文件的规范化路径；读取源文件仍使用原输入路径执行不跟随符号链接检查。精确授权投递端须确认登记路径等于宿主已选定的交付路径，不等时在投递前拒绝并撤销登记。
- 保留既有 sidecar 字段、原子写入和 staged/committed 状态。正 exp 快照仍持有私有副本；引用只持有外部路径、首次大小和摘要等登记元数据。
- 非企业受管 token 保持 `kind, asset_id, path, exp, size, digest, name, sid` 八字段和原签名。数值 `exp=0` 仅对受管 `verified_asset_v1` 引用跳过时间比较，bool、字符串和空值不能作为此标记。
- 签名及适用的会话检查后，必须在当前登记目录匹配 ID、路径、期限、首次大小、摘要和合法状态，并确认当前路径为普通文件。没有登记、登记撤销或声明不匹配均拒绝；不能仅凭 0 期限下载。
- 摘要在 grant 消费后、链接生成前的既有 stage 工作线程中采集，表示交付阶段读取到的指纹，不能解释为审批决策瞬间的内容快照。此后文件变化不覆盖该摘要，也不触发摘要重比或自动重审。

## 下载及清理

引用不受 600 秒限制；读取当前路径的当前字节，大小、offset 和 eof 使用当前 fstat 结果。保留既有读取上限和文件检查；旧正期限快照仍校验固定大小并遵守原 TTL。

stage 失败或投递前失败可撤销登记，引用的原文件不可被删除或 chmod；push 已开始时沿用既有不确定投递策略保留登记，commit 将 staged 标为 committed。引用不新启 TTL sweeper，prune 不按时间删除有效引用；非法引用最多清理登记。旧副本的 revoke、TTL 和孤儿清理维持原行为。

## 持久化与边界

首次摘要保存于原 sidecar 的 `content_digest` 和签名 token 的 `digest`；原 `chat.file.files[].download_token` 及 URL 随 `history.jsonl` 落盘。成功 flush 后，刷新或历史恢复可取回原始 token/摘要，文件修改或删除不重算历史。沿用普通与团队历史投影，团队 URL 内仍保留 token。

下载恢复还依赖相同签名密钥、可访问的原文件和同一登记目录。`JIUWENSWARM_DOWNLOAD_ASSET_ROOT` 显式配置优先；未配置时，无 `getuid` 的 Windows 使用 `get_user_workspace_dir() / "download-assets"`，默认位于 `~/.jiuwenswarm/download-assets`，遵循既有 DATA_DIR/HOME 设置，跨 PID 使用同一数据根即可定位相同登记。有 getuid 的 macOS/Linux 保留既有临时目录加 UID 路径。

共享 Owner 在模块导入时固定登记根，须在进程启动/首次导入前设置配置。解析路径不创建登记目录，stage/prune 负责创建；首次加载 utils 可能初始化既有日志目录。旧 Windows PID 目录不扫描或自动迁移，已知旧目录可用显式覆盖读取。登记丢失仍会使链接失效，但已落盘历史仍保留原摘要。写入未完成、历史被删除或密钥变化不提供额外保证。

引用登记不会自动过期，可能积累少量 JSON 元数据。原文件可被修改、移走或删除；不承诺父目录并发替换下的原子路径保证或下载期间文件一致性。回退旧服务不保证接受新 0 期限链接。

## 依据与验证入口

设计依据为 [D-20261113-1809-authorized-original-file-download](../../../designs/active/D-20261113-1809-authorized-original-file-download/design.md)。行为断言位于 `test_verified_download_assets.py`、`test_verified_web_file_download.py`、`test_send_file_execution_consumer.py`、`test_gateway_adapter.py` 和 `channels/web/test_web_file_download.py`；实际环境、结果及未验证项由关联 F 记录维护。
