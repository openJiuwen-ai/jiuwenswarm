# 员工知识选择

## 1. 元信息

| 项 | 值 |
| --- | --- |
| 建档日期 | 2026-10-17 |
| 范围 | 员工详情选择已发布的个人上下文页面，并在发布时复制到该员工办公目录 |
| 方案成熟度 | 已确认（按开发计划 JM-22 的责任边界实现；旧发布快照可从已校验 Spec 恢复所选知识目录） |
| 计划条目 | [JM-22](../designs/jiuwenmate-development-plan.md) |
| 负责人 | 常钰聪 |
| 状态 | 进行中（已补飞书私聊对当前发布版本知识副本的只读授权；真实飞书与真实模型读取未验收） |
| 设计依据 | [JiuwenMate 服务端架构设计](../designs/jiuwenmate-architecture.md) 第 10.4 节；开发计划 JM-22 |
| 关联 feature | 前置：[F_02](F_02_colleague-management.md)、[F_14](F_14_workspace-read.md) |
| 验证摘要 | 复制、拒绝与目录列举测试随 `test_resource_assembly.py`。前端详情页测试随 `test:jiuwenmate-profile`。浏览器与真实模型未执行。 |
| Refs | 无独立 issue；需求来源为员工详情「知识」分区需要实际生效 |

## 2. 背景、目标与范围

员工配置可以保存知识名称，但发布不会使用这些名称，员工也不能读取对应内容。个人上下文已经能采集并发布 Markdown 页面。本特性让员工从这些已发布页面中选择，并在发布时把所选页面复制到自己的办公目录。

本次包含：列出个人上下文根目录下的普通 Markdown 页面；在员工详情增删选择；保存并发布后复制所选页面；系统提示写明副本的相对路径；飞书私聊仅可读取当前发布版本所选页面的副本。

本次不包含：学习、来源失效、知识版本撤权、core 可选知识端口、读取整个个人上下文目录、复制符号链接或根目录之外的文件。

## 3. 方案

`jiuwenmate.knowledge.list` 返回 `~/.jiuwenswarm/.personal_context/workspace/context` 下的普通 `.md` 页面，名称是相对路径，可以包含中文。反斜杠、`..` 和越出根目录的名称不列出。发布时 `assemble_resources` 只复制所选页面到 `resources/knowledge/<内容摘要>/`。摘要变化才生成新目录，已发布版本仍指向原来的副本。修改副本不改原件。未发布名称、`..` 和符号链接拒绝发布。

Web 私聊使用既有 `workspace_read` 读取整个办公目录。飞书私聊不获得通用 `workspace_read`，Request 只冻结 `workspace_knowledge_read`；`ConfigSnapshot` 保存知识根目录的相对路径，`HostWorkspaceAccess` 将允许根限制到该发布版本的 `resources/knowledge/<摘要>/`。飞书群聊没有该权限，飞书私聊也不能读其他办公目录文件或写文件。配置快照随 Request 固定，因此已受理请求只能读其版本中选择的副本；未选择的旧版本副本不进入允许根。

系统提示明确区分飞书私聊和群聊的文件范围，避免同事尝试访问办公目录其他内容。路径仍由 agent-core 的工作空间读取工具做相对路径、越界和符号链接校验。
