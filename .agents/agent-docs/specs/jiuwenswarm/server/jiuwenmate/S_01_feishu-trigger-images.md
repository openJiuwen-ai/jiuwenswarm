---
id: S_01_feishu-trigger-images
title: HyperAgent 飞书触发图片输入
type: S
author: huanghongyun
author_email: huanghongyun1@huawei.com
modules: [jiuwenswarm/server/jiuwenmate]
files: [jiuwenswarm/server/jiuwenmate/media/execution_media.py, jiuwenswarm/server/jiuwenmate/media/feishu_inbound_media.py, jiuwenswarm/server/jiuwenmate/media/vision_runtime.py, jiuwenswarm/server/jiuwenmate/execution_driver.py, jiuwenswarm/server/jiuwenmate/bash_runtime.py]
---

# HyperAgent 飞书触发图片输入

- **来源**：绑定机器人只能下载当前群触发消息已登记的图片。按消息顺序编号，重复图片只下载一次，部分失败保留原编号。
- **读取**：`media:image:N` 与 `feishu-image:N` 指向本次执行的图片。调用前复核群权限和消息版本。视觉工具也可读取当前会话的 `output/`，以及当前 WeLink 消息的附件目录。
- **清理**：原图放在办公空间外的私有缓存。完成、失败或取消时清理，进程退出的残留在后续准备图片时按一天期限回收。恢复时按来源记录复核并重新下载。
- **配置**：优先读取页面保存的多模态配置；仅未建立统一配置的部署兼容旧 `VISION_*` 设置。视觉调用默认等待 60 秒，远端请求可能在本地停止后继续执行。
