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

本规约适用于 HyperAgent 群消息执行入口，与 Work/Code 的多模态装配规约分别维护。保持现有 Request、Session、persistent_agent 和消息协议。媒体实现集中在 `jiuwenmate/media/` 子包；执行范围位于 `execution_media.py`，统一模型解析函数为 `resolve_vision_config()`，不通过包初始化文件隐式装配工具。

- 原消息以接收应用的机器人身份读取，应用必须唯一绑定当前 namespace/agent。消息 ID、群 ID、撤回状态及附件引用必须匹配；仅解析 image 或 post 中的 img 节点，按帖子顺序保留重复编号。
- 在既有 current_trigger 校验后准备图片。模型输入追加图片编号及失败提示，经现有执行日志记录。工具挂载从当前执行 extras 取范围，不使用进程全局图片范围。
- 通用 ExecutionMediaScope 绑定既有 Session 目录及渠道适配器，不新建用户授权表。飞书 `feishu-image:N` 或通用 `media:image:N` 映射到执行自有文件；调用前重新校验群读取条件和飞书原消息版本。当前会话 output 图片及其办公空间相对路径可读，权限暂停或服务重启不使其失效；其他会话输出、任意 URL 和未授权办公文件拒绝读取。WeLink 只沿用当前 route.source_message_id 对应的既有 incoming 目录，不从正文推断授权目录；不更改 Bash 既有权限。
- 飞书下载及来源元数据放在 AI 同事数据目录的 media-cache 下，移出普通办公空间；不复制群聊正文，文件存放位置不授予模型读取权。
- 下载、格式、像素、耗时、失败回收和配置参数遵循产品说明 [HyperAgentFeishuImages](../../../../../../docs/zh/HyperAgentFeishuImages.md)。单图失败保留其他图的编号，整个准备超时则不给出已准备图片。
- 每个请求持久保存首次读取的来源版本及 key 序列。重试重新下载临时图片，沿用编号；原消息版本改变则拒绝恢复图片读取并要求重新发起。来源记录按 namespace、agent、session、request 和 app 哈希定位，不保存密钥或图片字节。
- 优先使用统一 multimodal 目录中的 tools.vision 开关和模型连接。配置影响新执行；统一目录存在时禁用状态优先于遗留 VISION 环境变量，只有目录尚未配置时兼容旧环境配置；配置损坏不能静默退回环境变量。缺少模型时不下载，追加明确原因，不自动复用聊天密钥。
- 清理发生在 driver 的 finally 中；ExecutionHandle 持有准备阶段子任务，调度器停止/取消时取消并等待下载清理，随后不进入 Core。沿用页面停止入口，不新增 IM 停止命令。视觉等待超时或取消不保证底层线程或远端服务已终止。

通用范围与配置回归：`media/test_execution_media.py`。行为回归：`tests/unit_tests/server/jiuwenmate/media/test_feishu_inbound_media.py`。其他渠道兼容：`test_welink_media.py`。执行、群读取与权限：`test_session_scheduler.py`、`test_group_chat_port.py`、`test_permission_approvals.py`。
