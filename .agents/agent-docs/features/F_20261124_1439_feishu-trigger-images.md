---
id: F_20261124_1439_feishu-trigger-images
title: HyperAgent 飞书群触发图片接入与真实验收
type: F
author: huanghongyun
author_email: huanghongyun1@huawei.com
modules: [jiuwenswarm/server/jiuwenmate]
files: [jiuwenswarm/server/jiuwenmate/media/execution_media.py, jiuwenswarm/server/jiuwenmate/welink_inbound.py, jiuwenswarm/server/jiuwenmate/media/feishu_inbound_media.py, jiuwenswarm/server/jiuwenmate/media/vision_runtime.py, jiuwenswarm/server/jiuwenmate/execution_driver.py, jiuwenswarm/server/jiuwenmate/bash_runtime.py, tests/unit_tests/server/jiuwenmate/media/test_feishu_inbound_media.py]
designs: [D-20261123-2128-feishu-group-media]
---

# HyperAgent 飞书群触发图片接入与真实验收

飞书下载、执行范围和视觉工具集中在 `jiuwenmate/media/`。使用稳定的图片编号，便于任务恢复时重新下载并核对原消息。[设计](../designs/active/D-20261123-2128-feishu-group-media/design.md) · [执行规约](../specs/jiuwenswarm/server/jiuwenmate/S_01_feishu-trigger-images.md)

- AI 同事回归 2047 项通过、1 项跳过；构建的 wheel 包含媒体子包。
- 浏览器实测图片问答、停止和恢复后的 OCR。真实飞书群消息回放成功读取原图；当前候选版本仍待验证新群消息从 @ 到回帖的完整流程。
- 已有会话关闭图片处理后仍观察到工具调用尝试，工具报错，用户收到未启用提示。是否向模型服务发出请求尚未核实。
