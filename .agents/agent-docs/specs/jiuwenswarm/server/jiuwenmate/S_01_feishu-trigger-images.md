---
id: S_01_feishu-trigger-images
title: HyperAgent 飞书触发图片输入
type: S
author: huanghongyun
author_email: huanghongyun1@huawei.com
modules: [jiuwenswarm/server/jiuwenmate]
files: [jiuwenswarm/server/jiuwenmate/feishu_inbound_media.py, jiuwenswarm/server/jiuwenmate/vision_runtime.py, jiuwenswarm/server/jiuwenmate/execution_driver.py, jiuwenswarm/server/jiuwenmate/bash_runtime.py]
---

# HyperAgent 飞书触发图片输入

本规约适用于 HyperAgent 群消息执行入口，与 Work/Code 的多模态装配规约分别维护。保持现有 Request、Session、persistent_agent 和消息协议。

- 原消息以接收应用的机器人身份读取，应用必须唯一绑定当前 namespace/agent。消息 ID、群 ID、撤回状态及附件引用必须匹配；仅解析 image 或 post 中的 img 节点，按帖子顺序保留重复编号。
- 在既有 current_trigger 校验后准备图片。模型输入追加图片编号及失败提示，经现有执行日志记录。工具挂载从当前执行 extras 取范围，不使用进程全局图片范围。
- 当前执行视觉工具只接受 `feishu-image:N`，映射到执行自有路径；调用前重新校验群读取条件和飞书原消息版本。其他渠道保持已有路径规则，不更改 Bash 既有权限。
- 下载、格式、像素、耗时、失败回收和配置参数遵循产品说明 [HyperAgentFeishuImages](../../../../../../docs/zh/HyperAgentFeishuImages.md)。单图失败保留其他图的编号，整个准备超时则不给出已准备图片。
- 每个请求持久保存首次读取的来源版本及 key 序列。重试重新下载临时图片，沿用编号；原消息版本改变则拒绝恢复图片读取并要求重新发起。来源记录按 namespace、agent、session、request 和 app 哈希定位，不保存密钥或图片字节。
- 缺少视觉模型时不下载图片，向本次输入追加明确原因。代码不会自动复用聊天模型密钥。
- 清理发生在 driver 的 finally 中；准备途中取消也清理。视觉等待超时不保证底层线程或远端服务已终止。

行为回归：`tests/unit_tests/server/jiuwenmate/test_feishu_inbound_media.py`。其他渠道兼容：`test_welink_media.py`。执行、群读取与权限：`test_session_scheduler.py`、`test_group_chat_port.py`、`test_permission_approvals.py`。
