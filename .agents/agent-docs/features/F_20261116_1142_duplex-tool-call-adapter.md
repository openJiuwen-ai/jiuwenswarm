---
id: F_20261116_1142_duplex-tool-call-adapter
title: Duplex WebSocket 工具适配器实施与验证
type: F
author: chickeyton
author_email: ngton2014@gmail.com
modules: [jiuwenswarm/runtime, jiuwenswarm/extensions/video_duplex, jiuwenswarm/channels/web/frontend/src/features/settings]
files:
  - jiuwenswarm/runtime/tool_calls/coordinator.py
  - jiuwenswarm/runtime/tool_calls/types.py
  - jiuwenswarm/extensions/video_duplex/backend/tool_adapters/vllm_omni.py
  - jiuwenswarm/extensions/video_duplex/backend/duplex_task_executor.py
  - jiuwenswarm/extensions/video_duplex/backend/duplex_session_bridge.py
  - jiuwenswarm/extensions/video_duplex/backend/task_adapter.py
  - jiuwenswarm/extensions/video_duplex/backend/video_search.py
  - jiuwenswarm/extensions/video_duplex/backend/video_live.py
  - jiuwenswarm/extensions/video_duplex/frontend/VideoLivePanel/qwenOmniSession.ts
  - jiuwenswarm/channels/web/frontend/src/features/settings/services/SettingsSourceProvider.tsx
designs: [D-20261116-1104-duplex-tool-call-adapter]
created: 2026-11-16
updated: 2026-11-18
---

# Duplex WebSocket 工具适配器实施与验证

## 关联依据与范围

独立新特性，对应 `D-20261116-1104-duplex-tool-call-adapter`。用户在设计检视后明确授权实现及指定服务器测试。仅接入 vLLM-Omni duplex WebSocket，不支持 HTTP tool calling，不依赖旧 #10517 合入。

本地实现基线 `3eea3124015885d3b556d7832a561a958b7a6b62`，候选为当前未提交工作区。外部协议及 E2E 源码固定为 vLLM-Omni PR #9909 head `fbdf47bf2b400034987f380089b2bd83435fdb91`。

## 实施与决策

- runtime 提供无传输依赖的调用/执行端口与协调器；扩展负责 WebSocket、能力协商、媒体映射和任务桥。
- vLLM 新路径工具执行归服务端，浏览器只发送明确用户输入、媒体和播放反馈。配置 RPC 签发一次性短时 ticket，避免把模型凭据或会话执行身份交给模型。
- 业务命令身份由后端 session、generation 和 call id 派生，避免不同连接复用 call id 时命中旧 TaskService 操作。
- 只公开 `jiuwen_delegate` 和 `jiuwen_queue_manage`，队列管理显式禁用嵌套回答；原权限、审批、Host 原生执行及持久任务服务继续生效。
- 等待实际任务结果，失败发送逐调用取消；成功等待后端关联接纳，再由独立调度器驱动 continuation。关闭结果 gate 不影响成功 continuation。
- 原 Qwen 入口与新执行桥复用 `VideoSearchManager.submit_task`；取消管理操作时收尾该操作自身的 shielded worker，不误取消管理目标任务。
- 设置使用共享 renderer 和新增 duplex 数据源提供普通字段，凭据只保留在服务端。旧连接配置卡片可展示新 provider，旧 Qwen/JoyAI 路径不迁移。

## 验证环境与证据

测试服务器 `test-server-1`。本任务目录 `<workspace>/duplex-tool-adapter/`，Swarm 源码 `<workspace>/repo/chickeyton/jiuwenswarm_work`，隔离测试虚拟环境位于任务目录 `.venv`。每次测试先激活用户指定的 `<workspace>/.venv`、加载 `<workspace>/profile`，再激活隔离测试环境；数据、显式临时/编译缓存及日志目录限定在工作区。`HF_HOME=/workspace/models`。

保留已有脏 vLLM editable checkout，另建 `<workspace>/repo/chickeyton/vllm_omni_pr9909` 固定 PR SHA。GPU 只使用授权编号 0–3，启动前与失败后检查占用，不清理其他用户进程。CPFS 不支持文件型 Unix socket，测试部署通过 `VLLM_RPC_BASE_PATH` 使用 Linux 抽象 IPC 命名空间。

GitCode 下载返回 HTTP 418；Core/Gateway/E2A 分别从官方 GitHub 镜像安装相同 SHA `f3f8f5761` / `2c40ac150` / `06a75d6`，不改变仓库依赖锁。普通依赖复用已有 Swarm 环境的只读 site-packages；新环境单独升级一致的 OpenTelemetry API/SDK，解决初轮收集的依赖版本冲突。

已取得的阶段证据（均在上述测试服务器执行）：

- `unit-e2e-acceptance.log`：基于 `3eea31240` 的实现完成全后端测试，**686 passed、无 skipped**，包含真实 PR #9909、128×128 JPEG 输入、真实音频输出及播放 ACK 的工具闭环。独立 SQLite 与 JSONL 记录证明任务 completed、只执行一次；模型回答为 `The measured test value is 17 degrees.`。Core 执行端为受控文件写入器，其余生产路由、桥、任务执行器和 TaskService 为真实实现。
- `e2e-live-playback.log`：协议/桥/真实模型定向验证 40 passed；实际发现并修复标准 arguments.done 无工具名、内部 created 字段不进入 GA wire，以及续答必须等待播放完成三处映射问题。前两轮失败日志保留用于原因追溯，不算通过证据。

- `unit-first.log`：最初协议/协调器用例 16 passed；只证明该轮版本。
- `unit-backend-latest.log`：完整视频双工后端 664 passed、1 skipped；跳过项为需要真实后端的 opt-in E2E。
- `unit-complete-final.log`：加入媒体与归档校验后的完整后端 677 passed、1 skipped；仍未包含真实模型 E2E。
- `unit-review.log`：最终协议桥与协调器定向回归 26 passed，包含结算记录预算、结算后关闭等待原生清理且不二次取消。
- `frontend-review.log`：实时前端回归 143 passed，覆盖服务端唯一工具执行、文本/转录去重、有界静音预录、后端清空音频及迟到响应隔离。
- `frontend-final.log`：144 passed，进一步覆盖文字提问带上当前摄像头。
- `unit-media-final.log`：32 passed，验证远程图片 URL 被拒绝及真实 WebSocket 桥在文字响应前设置当前画面。
- `unit-scope-final.log`：60 passed，覆盖每次工具前复核会话状态和签发票据时拒绝归档会话，同时回归原 Gateway/AgentServer 任务路由边界。
- `unit-config-final.log`：7 passed，补充新 provider 的中英/跟随语言 instructions 服务端传递、凭据不返回浏览器，并回归 Qwen/JoyAI 配置及恢复上下文所有权。
- `settings-fixed.log`：设置面板 67 passed。修复新增输入项缺少中英文 placeholder，以及旧契约断言遗漏 duplex 字段和 RPC 路由。
- `settings-i18n.log`：配置校验与双语文案 11 passed。
- `vite-build.log`：Vite 产品打包成功；未运行 TypeScript 静态检查或格式化。该打包早于最后音频清空修复，后者由上述实时前端回归验证。
- `visual-test.log`：Playwright 使用真实共享 renderer、声明式配置及 Settings 服务层，受控 RPC 记录模型字段保存；1280px 与项目支持下限 584px 渲染及无横向溢出检查通过，桌面/窄屏截图已人工查看。该证据是组件装配 fixture，不是完整产品应用启动；中文字体在服务器缺失，因此视觉检查使用英文，中文文案由国际化测试核对。明暗主题未新增样式，本轮仅检查浅色。

截图：[桌面](assets/duplex-tool-adapter-settings-desktop.png)、[窄屏](assets/duplex-tool-adapter-settings-narrow.png)。

通过 `candidate.sha256` 对照测试服务器源文件与本地候选；后续改动须更新清单及定向证据。测试日志与浏览器依赖留在任务目录，不作为源码或配置模板提交。

## 复查及实施偏差

相对设计，runtime 只公开最小 `ToolCall`、`ToolExecutionContext(owner_id, scope)`、`ToolResult` 和调用/结算事件端口；能力、异步输出接纳关联及待生成状态封装在具体适配器内，未额外创建 `ToolCapabilities`、`Submission` 类型。后端会话和 generation 已包含在调用身份，不从模型参数接收执行身份。

产品 request id 与原生执行租约继续由 TaskService/AgentServer 管理，不复制为新的协议状态权威。修改 provider/模型设置影响下一次连接，既有连接保留创建时的配置并通过显式停止释放；不在保存配置时自动重建在途会话。新路径的语言偏好由配置 RPC 作为受信会话 instructions 传给后端，浏览器仍无 session.update 权限。

已保存业务会话的归档/删除通过现有 AgentServer 元数据查询每秒检查，而非新增跨进程通知协议；因此撤销有最多一个查询周期的检测延迟。普通未保存 live scope 随连接和显式停止释放。跨进程 Core 实际任务与权限继续由原服务维护。

按 `jw-review` 对 runtime 协议/协调器、扩展装配/配置/任务执行/关闭、媒体消费者、Settings 数据源/请求路由/字段/文案及中英文文档核对。已修复成功接纳后续生成被 gate 关闭抑制、嵌套审批回答、跨连接 command id 碰撞、取消已完成任务误生成后继、二次取消打断清理、伪造浏览器命令、ticket 诊断泄漏、摄像头载体、设置状态漂移、转录重复、空闲音频积累和后端清空音频遗漏。新录入的媒体回归使用真实前端会话类；服务端媒体/输出接纳用例使用实际 WebSocket 桥与受控上游。真实模型行为仍需单独验收。

## 后端启动调查

PR SHA 源码已固定；使用现有 `Qwen3-Omni-30B-A3B-Instruct`，没有使用 MiniCPM。spawn 和 forkserver 启动均未达到模型就绪。CPFS 文件型 Unix socket 不可用，已通过抽象 socket 处理测试环境 IPC；后续启动在引擎 readiness sentinel 检查结束，父进程记录 `Failed core proc(s): {}`，诊断当刻子进程仍 alive。不能据此认定 PR #9909 工具调用逻辑有错。

现有 editable 元数据报告旧 vLLM-Omni dev 版本并产生版本警告，但实际 `PYTHONPATH` 固定 PR 源码；PR requirements 声明使用 vLLM 0.33 / Transformers 5.16+，不能仅用此警告认定依赖不兼容。另行对照不加载 vLLM-Omni 的 CUDA 多进程程序，正常退出也可能在 `exitcode` 更新前触发 sentinel 事件，说明瞬时 alive 记录不能证明子进程未退出。继续调查实际退出状态。

后续信号跟踪 `startup-signals.2301005` 捕获到 `SIGTERM` 的发送者 `si_pid=2966587, si_uid=0`；对应进程为 `canhazgpu`，父进程 `supervisord`。这是外部终止证据，不是 PR 工具协议错误证据；具体自动/人工触发方式未确认。直接启动绕过服务器 GPU 预约不能用于持续测试。用户进一步明确要求使用 canhazgpu，改为其标准 run 队列，预约 `b23d7f2c` 分配 GPU 0、1，60 分钟超时，其他用户任务未改动。

另建 GPU 测试环境 `duplex-tool-adapter/vllm-venv`，复用原 GPU 环境只读依赖并把隔离 PR checkout 安装为 editable；`direct_url.json` 确认 `editable=true`，安装版本 `0.1.dev4361+gfbdf47bf2`。正式预约运行去掉信号调试，仅保留 CPFS 抽象 socket 的测试环境适配；预约中的 CUDA_VISIBLE_DEVICES 由 canhazgpu 决定并校验仅 0–3，profile 不覆盖已分配列表。该轮已越过先前终止位置并加载真实 Qwen3-Omni 权重。

## 原基线验证与 PR 集成前状态

原基线的实现、复查与上述测试已完成；预约 `b23d7f2c` 已通过 canhazgpu cancel 释放，模型服务及其 3 个 GPU 子进程停止，其他用户任务未改动。用户随后授权提交 PR；准备期间上游 develop 增加 15 个提交，涉及同一任务执行/授权契约，PR 集成候选需对齐并重验，不把旧基线的通过结果直接当作新基线通过。

真实模型测试在 Core 执行边界使用可计数文件写入器，生产桥、适配器、任务执行器、TaskService 和 SQLite 保持真实；该证据不替代完整 Core/Gateway 进程、模型权限审批和麦克风实机验收。候选或后端配置变化后重验受影响场景。

## PR 集成与最终复查

已整合上游 develop `b6d82411bc5715b8255484315b84d101902538d5`。共享任务提交的唯一合并冲突保留上游登录模型凭据绑定；ticket 绑定原鉴权连接的解析闭包，按实际执行目标调用既有刷新/引用解析函数，仅把 credential_ref 传给共享 submit_task。委托画面在工具开始时捕获并沿原 Core 参数传递，后续帧不改写已捕获值。两者均不接受模型参数替代。

上游新代码及其登录认证测试引用已移除的 `jiuwenswarm.common.e2a.constants`，导致收集/运行失败；改为协议唯一真源 `e2a.constants`，未改变认证断言或降低覆盖。按整合基线 pyproject/lock 将隔离环境 Core 更新为 `2f4bedb9c5d85ab70d7c79d36183e6a98799e94e`，direct_url.json 验证实际安装提交；其余 Gateway/协议 pin 不变。

最终候选验证（不得与前述旧基线数相加）：

| 入口 | 结果 | 证据 |
| --- | --- | --- |
| 视频双工全后端，含真实模型 E2E | **737 passed，零 skipped** | unit-e2e-pr-final.log |
| 实时双工前端回归 | **150 passed** | frontend-rebase.log |
| 设置面板 | **69 passed** | settings-pr-updated.log |
| 配置与国际化 | **11 passed** | i18n-rebase.log |
| Vite 产品打包 | **passed** | vite-rebase.log |

真实 E2E 独立读取 TaskService SQLite、外部 JSONL 和 e2e-evidence.json：任务 completed、实际执行一次、当前 JPEG 已传到委托执行端、模型最终回答 `The measured test value is 17 degrees.`。固定 PR head 未修改。headless 音频接收端消费 PCM 并确认，非物理音频播放；Core 外部执行边界仍为受控写入器。

复查覆盖新增端口/适配器、结果与播放 gate、Host 身份/模型授权/帧传递、生命周期、共享任务入口、设置读写与消费方、设计/spec 及中英文文档；对照上游已合入变更保留新的原生结算、认证与前端任务行为。没有在已声明范围内发现待修复问题。正式 Maintainer/QA 审阅、真实 Core LLM 与物理音视频验收未冒充通过，后续按使用环境验证。

测试计划在本地 ignored 的 designs/testing/D-20261116-1104-duplex-tool-call-adapter/testing.md，PR 正文包含可公开的计划和结果。截图为真实共享设置组件装配，见前述附件；原始日志、数据、虚拟环境及服务器连接信息不入库。

通过 pr-source-manifest.json 核对本地/远端 38 个变更源文件和依赖文件，仅归一化已知 CRLF/LF 传输差异。最终预约 976a7cfa 已释放，模型父进程及 3 个 GPU 子进程停止；未触碰其他用户预约。设计保持 active，待实际交付确认后按既有生命周期归档。

PR #11838 建立后，develop 更新至 `a64caf423`；再次整合时仅自动生成索引冲突，通过同步工具重建，保留全部上游文档。runtime/tool_calls、video_duplex（含测试）及前端 src 与已验证候选逐文件相同；上游新增一项 Settings 测试，测试服务器重新运行设置面板为 69 passed。上游依赖变更涉及构建工具及 harmony extra，不改变本轮默认 Core/Gateway/E2A pin。737 项后端（含真实模型）证据对应前述 b6d82411b 集成候选，不声称在 a64caf423 上重新运行完整 E2E；新基线只补验受影响的设置测试。

## 2026-11-18 上游 main E2E 复验

用户要求在 #9909 合并后使用最新 main 重新验证。测试服务器隔离 vLLM-Omni checkout 从合并提交 `320cf50cfd30c463d5ed598d5cd08fc7eb2e37a1` 快转合并到本次 fetch 的 main `16ad661a496c6466b09e85860b4c8647bce0c7da`，工作树无受管文件修改。重新 editable 安装版本为 `0.33.0rc2.dev360+g16ad661a4`；vLLM 0.33.0、Torch 2.14.0+cu130、Transformers 5.19.0，模型仍为 Qwen3-Omni-30B-A3B-Instruct，沿用三 stage 测试部署配置及 CPFS 抽象 IPC 启动适配。

JiuwenSwarm 候选为 `53774939acf274db2332d780f77ac77f0ad4d08c`，没有修改实现或测试；38 个源码/依赖文件的远端指纹与本地候选一致。只重跑现有 opt-in `test_vllm_omni_live.py`，设置 `VLLM_OMNI_E2E_URL` 和上述 main SHA 的 `VLLM_OMNI_E2E_REVISION`，执行 `python -m pytest jiuwenswarm/extensions/video_duplex/tests/backend/test_vllm_omni_live.py --no-cov -q`，各轮使用独立 basetemp。

| 运行 | 结果 | 日志 |
| --- | --- | --- |
| 最新 main 首轮 | **1 passed，11.95 秒，零 skipped** | e2e-main-20261118.log |
| 显式 workspace 缓存后独立重启复验 | **1 passed，11.49 秒，零 skipped** | e2e-main-workspace-20261118.log |

两轮分别在独立模型进程中执行同一场景，不合计为两个不同用例，也不代表本次重新运行了此前 737 项全后端测试。首轮启动发现 FlashInfer 暖机默认使用家目录缓存；正式复验显式设置 FLASHINFER_WORKSPACE_BASE、FLASHINFER_AUTOTUNE_CACHE_DIR、XDG_CACHE_HOME、CUDA_CACHE_PATH、TORCH_HOME 和 NUMBA_CACHE_DIR 到任务工作区后重新启动，保留原 VLLM/TRITON/TorchInductor 工作区缓存设置，没有清理共享家目录缓存。

独立读取正式复验的 TaskService SQLite、tool-executions.jsonl 和 e2e-evidence.json，确认任务 completed、外部执行一次、JPEG 已传至委托执行端、关联 output 接纳，以及模型回答 `The measured test value is 17 degrees.`；协议记录同时包含完整工具投影、音频完成及播放确认事件。证据位于任务目录 `pytest-main-workspace-20261118/test_real_duplex_model_execute0/`，独立核对摘要为 main-workspace-evidence-20261118.txt。原始日志及 SQLite 等仅存测试工作区和本地忽略/仓库外产物目录，不提交。

两次 GPU 预约均使用 canhazgpu，只占用 GPU 0、1；正式复验后释放预约及模型父/子进程。没有发现本场景需要修复的适配器或上游协议问题。Core 外部执行仍为受控文件写入器，音频为 headless PCM 消费/ACK；不扩大为完整 Core/Gateway、真实 Core LLM、物理麦克风/扬声器、性能或跨平台验收。

## 2026-11-18 develop 合并与回归

用户要求合并 develop 并解决全部冲突。本轮抓取上游 `c18fd2924e7426f6a4658717da80eb9bda0705d3`，采用 merge 保留已有分支历史。8 个冲突文件中，4 个 Settings 文件同时保留 duplex 与上游新增的 multimodal 数据源、独立 provider、字段验证和 RPC 路由；4 个文档索引按新同步工具从源 frontmatter 重新生成，不手工拼接。自动合并的任务查询 metadata、经 AgentServer 路由的对话历史写入，以及已有登录凭据/画面传递均保留。

受测代码对应合并候选 tree `57605f6a612fd2d4c44a7eae069f6452224d487f`；后续仅补充本验证记录。测试服务器从固定 SHA 的官方 develop 归档和已校验 SHA256 的本分支 Git blob 差异装配候选。按合并后 pyproject/lock 更新隔离环境的 E2A 至 `defa6d8d8904eb3272a58fa0ad13f94f7adb4a51`、Gateway 至 `703da5cec987acaaab3557e90007965ca2644c96`，安装 direct_url.json 已核对实际提交，Core pin 保持不变。

| 入口 | 本轮结果 | 证据 |
| --- | --- | --- |
| 视频双工完整后端，启用真实 main 模型 E2E | **743 passed，零 skipped** | merge-backend-20261118.log |
| Settings | **72 passed** | merge-settings-panel-20261118.log |
| 新多模态设置 | **19 passed** | merge-multimodal-settings-20261118.log |
| 实时双工前端 | **150 passed** | merge-qwen-barge-in-20261118.log |
| 配置与国际化 | **11 passed** | merge-i18n-20261118.log |
| Vite 打包 | **passed** | merge-vite-20261118.log |

后端执行 `python -m pytest jiuwenswarm/extensions/video_duplex/tests/backend --no-cov -q`，通过环境启用既有 live 用例，vLLM-Omni 仍为本日已验证的 main `16ad661a496c6466b09e85860b4c8647bce0c7da`。独立核对 SQLite、外部执行文件及协议记录，确认一次执行、completed、委托画面、结果接纳与模型回答 17；原始证据在 `pytest-develop-merge-20261118/`，核对摘要 merge-evidence-20261118.txt。测试范围继续使用受控 Core 写入器和 headless 音频 ACK，不宣称完整产品或物理设备验收。

本轮没有新增实现或测试用例，未运行格式化、TypeScript 静态检查或 diff 静态检查。GPU 0、1 经 canhazgpu 预约，在测试后释放模型进程与预约；运行日志、数据库及同步脚本不入库。
