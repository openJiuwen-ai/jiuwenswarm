---
id: F_20261124_1439_feishu-trigger-images
title: HyperAgent 飞书群触发图片接入与真实验收
type: F
author: huanghongyun
author_email: huanghongyun1@huawei.com
modules: [jiuwenswarm/server/jiuwenmate]
files: [jiuwenswarm/server/jiuwenmate/feishu_inbound_media.py, jiuwenswarm/server/jiuwenmate/vision_runtime.py, jiuwenswarm/server/jiuwenmate/execution_driver.py, jiuwenswarm/server/jiuwenmate/bash_runtime.py, tests/unit_tests/server/jiuwenmate/test_feishu_inbound_media.py]
designs: [D-20261123-2128-feishu-group-media]
---

# HyperAgent 飞书群触发图片接入与真实验收

依据 [D-20261123-2128-feishu-group-media](../designs/active/D-20261123-2128-feishu-group-media/design.md)，用户于 2026-11-24 明确要求验证机器人支持后开发。当前契约见 [S_01_feishu-trigger-images](../specs/jiuwenswarm/server/jiuwenmate/S_01_feishu-trigger-images.md)，用户配置与使用见 [HyperAgentFeishuImages](../../../docs/zh/HyperAgentFeishuImages.md)。原始草案在另一个用户工作区保持不变；本隔离工作区保存 active 副本，尚未归档交付。

## 实现与选择

新增 `feishu_inbound_media.py`，使用既有绑定机器人的应用身份获取原消息及原图，不依赖个人 CLI 就绪状态。它只接受触发消息的结构化图片节点及已登记附件，完成限量下载、格式验证、执行级文件所有权、来源版本记录和清理。

现有 `execution_driver.py` 在群来源检查后准备图片，通过本次请求文本向模型提供稳定的 `feishu-image:N` 编号。`bash_runtime.py` 从本次执行 extras 将图片范围传给既有视觉工具工厂；`vision_runtime.py` 进行编号映射和调用前检查。保持既有专家、调度、会话、权限与飞书回帖路径，不增加 auto harness、接收器进程或新的专家。未修改 agent-core、agent-gateway 或 persistent_agent 命名。

选择虚拟编号而非临时文件路径，是为了让暂停或重试后原工具参数仍有意义；每次执行重新下载，来源版本改变则拒绝读取。来源小记录保留在该会话目录，原图按执行清理。与原草案的主要实施细化是增加来源记录和视觉等待超时；Bash 的既有完全访问权限不被这项视觉工具范围替代。

## 候选与依赖

开发工作区 `<local-checkout-or-evidence>`，分支 `codex/hyperagent-feishu-images`，基线 `e6add3ffeed905a124589dc7bd10febf114787fb`（来源工作区当前为 releases/0.2.9）。没有将用户其他未提交修改带入，也没有宣称该基线为最新 develop。

真实试点运行于 `<local-checkout-or-evidence>`，沿用其中已有的 Issue 修复及恢复改动，三个受影响既有模块在部署前已备份到本机 `.issue-repair-runtime-backups/feishu-media-before-deploy`。功能补丁仅回移本次接线，不覆盖试点的调度恢复逻辑。尚未提交、推送或创建 PR。

测试使用现有试点虚拟环境及本地 editable Core：`PYTHONPATH=.:<local-checkout-or-evidence>`。不等同于锁定依赖 wheel 安装验证，也没有构建发布 wheel。本次新模块使用现有 Pillow、httpx 依赖。

## 已执行证据

| 验证 | 实际结果 |
| --- | --- |
| 绑定机器人读取旧富文本附件 | 原消息可读，旧图片返回 14005 Resource Has Been Deleted；不能据此判定平台不支持 |
| 批量读取群历史 | 230027，缺 im:message.group_msg；这项权限没有新增，也没有作为接入前置条件 |
| 新独立图片下载 | 当前机器人返回 200 image/png，5,055 字节；本地 SHA-256 为 `1d7d9590b1e4476140d94eb919f11448082f2b3e61a37bff99ce7938c136dbe9` |
| 新富文本图文读取 | 新下载模块成功提取 img 节点并下载；既有视觉工具实际返回正确字符与空间位置 |
| 百炼 Token Plan | 用户明确授权复用现有密钥配置 qwen4-max；真实图片输入 200，返回 VISION-K7Q9 及蓝圆在红框右下方；密钥仅在运行配置内保存 |
| 改前真实群消息 | @ 同事加图文后，回答未取得图片，CLI profile not ready；复现接入断点 |
| 改后真实群消息 | 原群正确回复字符、蓝色实心圆及右下方；同时记录 VQA 与 OCR 两次工具成功结果，任务 completed，临时图片已删除 |
| 最新开发工作区定向回归 | 117 passed，含 17 个新增场景以及 WeLink、调度、群读取、权限既有回归 |
| 试点回移候选回归 | 64 passed，覆盖新增场景、WeLink 与会话调度 |
| 扩大到整个 jiuwenmate 单测目录 | 收集失败：test_model_refresh.py、test_web_adapter.py 无法导入 MultimodalConfigAdapter；在未修改的来源工作区复验同样失败，不归为本功能修复完成 |

真实群为“HyperAgent辅助社区维护”，同事为“ISSUE解决大师”。改后验收请求 `本机验收记录中的标识`，源消息 `本机验收记录中的标识`，执行 `本机验收记录中的标识`。提问正文不含 VISION-K7Q9；该字符仅出现在测试图片中。CUA 确认原群看到了正确最终回答，数据库独立核对 `tool.completed` 的 VQA/OCR 结果及 output.proposed。

本机详细证据位于 `<local-checkout-or-evidence>` 和 `feishu-image-new-code-result.json`，未提交运行日志或凭据。真实群验收后新增的来源恢复与超时保护分别经过变更前失败、变更后通过的定向回归；最终试点更新后另做机器人下载与视觉工具调用探针：返回正确字符与方向，临时图片为 0，来源记录存在，同一请求重新准备通过；证据为同一备份目录的 `feishu-image-final-probe.json`。提示词文字调整后 WeLink 的 6 项回归再次通过。

可重复运行：在仓库根目录设置上述 PYTHONPATH，使用试点 `.venv/bin/python -m pytest --no-cov -q`，路径为 `tests/unit_tests/server/jiuwenmate/` 下 `test_feishu_inbound_media.py`、`test_welink_media.py`、`test_session_scheduler.py`、`test_group_chat_port.py`、`test_permission_approvals.py`。

## 覆盖与遗留

已验证单图图文从真实人工发送、接收、下载、视觉调用到原群回复；独立图片原图下载也已验证。多图顺序、重复下载、单图部分失败、撤回、跨群拒绝、取消清理、来源恢复、缺模型、绑定错误及超时有自动化覆盖。未真实测试多图比较、重启时的完整许可恢复、其他租户、Windows/Linux 或锁定依赖发布包。精确多图差异识别仍受逐图视觉调用限制。

现有群消息正文解析有重复文字现象，本次未改。试点还存在 video_duplex 的 managed_tasks 表缺失告警，未处理或将其归为图片接入问题。视觉调用仍未增加专门的预算 usage 计量；超时释放等待方，不保证已发出的 SDK 线程/远端请求终止。群成员退出或消息撤回在最后一次检查后到远端请求发送之间仍存在外部平台竞态，已发出的图片无法追撤。

本地候选及试点可体验；合入 develop、发布包与跨平台验证仍需按正式发布流程进行。


## 2026-11-24 迁移到 develop

当前开发候选改为 `<local-checkout-or-evidence>`，分支 `codex/hyperagent-feishu-images-develop`，基线 `b94f4ca1257264005c3014985953df9b0e3a1327`。上文 releases/0.2.9 候选及真实试点结果作为历史证据保留；原候选和试点服务未被改写。

只迁移三个既有模块的功能补丁，以及图片模块、测试和配套文档；未迁移旧分支的生成索引、Issue 修复代码、账号配置或密钥。补丁应用没有冲突。最新 develop 的群读取接口允许空触发消息结果，图片调用前检查增加 `None` 拒绝路径；回归先复现 AttributeError，修复后按明确的来源不可读错误拒绝。执行入口测试替身同步补齐 develop 已有的 related-agents 接口。

独立 `.venv` 通过 `uv sync --frozen` 安装 develop 锁定依赖，不再借用试点环境。本次安装核对：Core `02b2ce4538b60cc4fd6085db36021b48ee5a1d99`、Gateway `77c62403f7727bd3a4f83e677e151bed79d042ac`、E2A `10ad4a2eeff11e0600c06a8235a797c5dd9f0a38`。没有修改 pyproject.toml 或 uv.lock。

定向回归命令沿用上列五个文件，改为本工作区 `.venv/bin/python -m pytest ... --no-cov -q`，不设置试点 Core 的 PYTHONPATH：119 passed。完整 AI 同事模块执行 `.venv/bin/python -m pytest tests/unit_tests/server/jiuwenmate --no-cov -q`：2030 passed、1 skipped，耗时 85.82 秒。此前旧环境无法收集的 test_model_refresh.py 和 test_web_adapter.py 在本次锁定依赖环境正常执行。定向结果见 `/tmp/feishu-develop-focused.log`，完整结果见 `/tmp/feishu-develop-module-regression.log`；日志只留本机，不提交。

本次迁移没有重新部署试点、发送群消息或重跑真实模型验收；旧真实群结果不能解释为 develop 新候选已完成真实群复验。本次迁移完成后提交独立 PR，提交与目标分支以 PR 信息为准。
