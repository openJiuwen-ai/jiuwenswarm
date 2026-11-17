---
id: S_01_authorized-file-download
title: 授权文件下载规约
type: S
author: nypdmax
author_email: chengkunck@gmail.com
created: 2026-11-14
updated: 2026-11-17
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

## 适用范围

仅非企业、要求精确执行授权的 send_file_to_user 在消费本次路径/目标授权后创建原文件引用。普通交付、企业 OBS、其他 token 用途及旧正期限快照保持既有行为。

## 核心契约

- stage(reference_only=True, expires_at=0) 复用原 sidecar 原子写入及 staged/committed 状态，不创建字节副本或修改源权限；引用模式下 Owner 仅拥有登记。
- token 保持 kind、asset_id、path、exp、size、digest、name、sid 八字段及原签名。仅 verified_asset_v1 的数值 exp=0 跳过时间比较；bool、字符串和空值不能作为标记。
- 仍须验签、校验适用会话，并匹配 sidecar 的 ID、路径、期限、首次大小、摘要及合法状态，确认当前路径为普通文件；无登记、撤销或声明不匹配均拒绝。
- sealed_path 保存规范化原路径；读取仍对原输入执行不跟随符号链接检查。登记路径须等于宿主已选定交付路径，偏移时投递前拒绝并撤销；Owner 私有缓存目录内的引用源须在 prune 前拒绝。
- SHA 在授权消费后、链接生成前采集，保存于 sidecar.content_digest、token.digest 及随 history.jsonl 落盘的原 token/URL。成功 flush 后可恢复原摘要，不因文件变化重算、不恢复一次性 grant。
- 下载读取当前字节，size、offset、eof 使用当前 fstat 大小并保留原读取上限。旧快照继续固定大小校验和原 TTL。
- stage/投递前失败、revoke、prune 只能清理引用登记，不能删除或 chmod 原文件。push 已开始时保留不确定投递登记；引用不启 TTL sweeper、不按时间回收，旧副本清理行为不变。

## 恢复与边界

恢复下载依赖相同签名密钥、原文件及登记目录。JIUWENSWARM_DOWNLOAD_ASSET_ROOT 优先；否则 Windows 使用 get_user_workspace_dir()/download-assets，macOS/Linux 保留临时目录加 UID。配置须在共享 Owner 首次导入前就绪；旧 PID 目录不自动迁移，可用显式覆盖读取。

原文件可变可删，登记丢失会使链接失效，已落盘历史仍保留摘要；不过期 JSON 元数据可能累积。不保证父目录并发替换或下载期间修改的原子一致性，回退旧服务不保证接受 exp=0。

设计依据及验证结果见关联 [D](../../../designs/active/D-20261113-1809-authorized-original-file-download/design.md) / [F](../../../features/F_20261114_0017_authorized-original-file-download.md)。
