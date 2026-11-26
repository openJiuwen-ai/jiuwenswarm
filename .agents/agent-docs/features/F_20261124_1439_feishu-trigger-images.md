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

## 2026-11-25 通用媒体范围、统一配置与取消补齐

本节是当前候选状态；前述环境变量配置和会话内缓存路径作为历史实现记录保留，当前契约以 S_01 与产品说明为准。用户明确授权按评审建议开发，并允许用 Playwright 替代连续超时的 CUA 进行真实页面验收。

新增 `media_runtime.py`，将渠道的入站引用解析、当前 Session 输出范围及媒体子任务取消放在通用执行层。飞书适配器拥有下载与消息版本检查，物理图片和来源元数据移到 `jiuwenmate/media-cache/`，不重复保存群聊正文。通用层委托适配器解释 `media:` 引用，兼容已使用的飞书编号；不依赖 Issue 修复命令、工单或专家提示词。

不为截图增加文件授权数据库，也不以执行开始时的文件修改时间判断授权：权限确认或重启会创建后续执行，时间基线会误拒绝同一会话已有的合法截图。采用既有 Session 归属，只允许当前 Session 的 output 图片；办公空间相对路径和会话相对路径均可用。WeLink 保留当前已接收消息的 incoming 目录，通过宿主 route 指定，不能由用户正文指定其他目录。其他会话、私有缓存裸路径、任意 URL 及符号链接越界均拒绝。

ExecutionHandle 记录准备阶段子任务；现有 scheduler.abort 在 stop/cancel 时取消并等待清理，下载尚未结束时不进入 Core。页面继续使用原有输入框停止按钮，不增加 IM 停止指令。远端已接受的视觉请求仍不能保证追撤。

视觉工具读取现有 MultimodalStore 的已解密统一模型目录。页面关闭的图片开关优先于遗留 VISION 环境变量；只有目录尚未配置才兼容旧变量，损坏目录不会退回旧配置。不借用聊天密钥，不新增模型页面或另一份密钥配置。

### 本轮候选与证据

隔离候选使用本分支和 develop 锁定依赖，页面地址 `http://127.0.0.1:6273`；后端端口 19880、19881、20800。候选未绑定试点机器人、所有渠道及 GitCode 修复入口均关闭，旧试点服务未修改。多模态模型由真实页面添加自定义 OpenAI 协议的 qwen4-max，并在智能体图片处理开关选择；候选不设置 VISION 环境变量。API Key 仅保存在本机候选配置，不提交。

| 验证层次 | 实际结果 |
| --- | --- |
| 完整 AI 同事回归 | 2040 passed、1 skipped，96.67 秒；后续只针对渠道目录防逃逸、通用引用委托和连接创建顺序做定向复验 |
| 最终定向回归 | 83 passed，涵盖通用范围、统一目录优先级、飞书下载、两种调度器停止、来源检查、WeLink 当前消息兼容 |
| WeLink 补充回归 | 四个接入、授权提示和配置模块 32 passed，2.12 秒 |
| 前端生产构建 | npm run build 成功；本次不改前端代码 |
| 页面保存及重启保留 | 多模态页面保存成功，无 METHOD_NOT_FOUND；重启后模型及图片开关保留 |
| 真实会话生成截图并识图 | Pillow 运行时生成随机码，VQA 与 OCR 实际调用 qwen4-max 返回 OD58S9DA、蓝圆在红框右下方；随机码未输出到生成命令日志 |
| 重启后的同会话图片 | 实际使用办公空间相对路径调用 VQA 成功，无须重新生成 |
| 跨会话读取 | 真实视觉调用尝试读取另一会话图片被拒绝，最终回答明确未读取，不猜测图中文字 |
| 页面停止 | 观察到真实 VQA tool.intent 后点击停止，约 0.15 秒显示已停止，数据库记录 execution.paused/stopped |
| 页面关闭及恢复 | 关闭后新执行无视觉工具，不冒充本次识图；重新开启后实际 OCR 再次返回 OD58S9DA |
| 下载阶段停止 | 真实调度器与 HTTP 传输替身组合：stop/cancel 均取消等待、删除下载目录，Core 没有启动；不是人工飞书端到端截图证据 |
| 真实飞书资源回放 | 以原机器人只读获取此前人工测试消息的原图，SHA-256 与历史原图一致；新版通用范围和页面配置模型返回 VISION-K7Q9 及蓝圆位置，确认文件在办公空间外且关闭后已删除 |

截图、真实工具结果、停止耗时及 pytest HTML 报告留在本机 `.issue-repair-runtime-backups/media-e2e-20261125/`，截图来自候选实际页面，不提交账号配置或运行凭据。完整回归日志为 `/tmp/media-hardening-module-regression.log`，最终定向为 `/tmp/media-hardening-final-focused.log`。

### 验收限制

本轮真实浏览器覆盖页面→AI 同事→视觉工具→真实模型→页面回答；飞书只读资源回放不等于重新发送人工群消息的完整入口/回帖验收。本轮未切换旧试点机器人到候选，未在飞书重新发送消息；没有将旧版已通过的真实群回帖当作当前候选的复验结果。其他渠道的新下载、音视频、跨平台发布 wheel、多模态调用的独立预算计量仍未实现。

最终检视发现 WeLink 旧目录把标点替换并截断消息 ID，会使不同消息共用目录。已将复制和执行范围查询统一改为完整消息 ID 的 SHA-256 目录，并用标点碰撞、共同长前缀两例先复现覆盖旧文件、再验证隔离。既有旧格式 WeLink 附件路径不自动授权到新范围；部署前仍在执行或等待权限确认的旧媒体任务需要重新发送消息。普通新消息的下载、路径改写与视觉读取保持贯通。

## 2026-11-26 媒体模块命名与目录整理

按用户确认的命名建议，将图片相关实现集中到 `jiuwenmate/media/` 子包：`media_runtime.py` 改为 `media/execution_media.py`，`vision_runtime.py` 和 `feishu_inbound_media.py` 一并移入。`vision_config_from_env()` 改为 `resolve_vision_config()`，表明其优先读取统一配置、兼容旧环境变量的实际职责。包初始化文件仅声明用途，不做全局注册或集中重导出。

执行驱动、Bash 工具装配及 WeLink 消息接入仍在原目录，因为它们还承担非媒体业务；不为三个实现文件增加多层空目录。两个本特性新增测试文件同步归入 `tests/unit_tests/server/jiuwenmate/media/`，执行范围测试改名为 `test_execution_media.py`。所有代码导入、测试替换目标及当前文档路径一并更新；不修改媒体引用、配置键、缓存目录或持久化格式。旧源码导入路径不保留兼容壳，内部消费者统一迁移。

本轮定向回归 83 passed（9.51 秒）；完整 AI 同事回归 2047 passed、1 skipped（98.64 秒）。`uv build --wheel --out-dir /tmp/media-reorganization-dist` 成功构建 workswarm-0.2.9b5；独立检查 wheel 内含新 media 子包的四个 Python 文件，且不含三个旧平铺模块。没有重跑真实模型或人工飞书群场景，本轮不改变其行为；旧验收脚本若硬编码了原源码路径，需要改用新导入路径。
