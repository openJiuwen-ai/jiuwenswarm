---
id: D-20261123-2128-feishu-group-media
title: 飞书群聊 AI 同事图片理解与问答首发方案
slug: feishu-group-media
status: in-development
stage: active
author: huanghongyun
author_email: huanghongyun1@huawei.com
reviewers: []
created: 2026-11-23
updated: 2026-11-25
domain: messaging
modules: [jiuwenswarm/server/jiuwenmate]
tags: [feishu, vision, group-chat]
scope: 飞书群聊触发消息中的独立图片与富文本内嵌图片问答
related: []
type: D
files: []
---

# 飞书群聊 AI 同事图片理解与问答首发方案

沿用群聊的触发、调度和回帖流程，在执行阶段将原消息里的图片交给现有视觉工具。

1. 飞书适配器用绑定机器人的身份核对原消息，按消息顺序下载已登记的图片。
2. 执行器向模型提供图片编号。视觉工具按编号读取前再次校验来源，调用图片问答或 OCR，并将结果交给同事回复。
3. 任务结束或取消时清理私有缓存。其他渠道可接入同一执行范围，自行负责资源读取和授权核验。

验收时在同一条消息里放入截图和问题，检查图中文字及位置、多图顺序，以及原消息撤回后的读取结果。
