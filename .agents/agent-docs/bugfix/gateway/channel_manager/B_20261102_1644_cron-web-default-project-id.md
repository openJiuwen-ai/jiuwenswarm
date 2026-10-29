---
id: B_20261102_1644_cron-web-default-project-id
title: 前端未选项目创建定时任务落库 project_id 回退为空串
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - gateway/channel_manager/web
files:
  - gateway/channel_manager/web/app_web_handlers.py
  - jiuwenswarm/channels/web/frontend/src/components/CronPanel/cronProjectDisplay.ts
  - tests/unit_tests/gateway/test_app_web_handlers.py
designs: []
design_note: 恢复 4b00671f6（#5713）已实现的行为，属于对该修复在企业版合入后被回退的局部回归修复，无独立设计。
---

# 前端未选项目创建定时任务落库 project_id 回退为空串

## 现象与影响

基线为 develop 的 01c69908f。通过 web 前端定时任务抽屉创建任务、项目下拉框不选
（使用默认项目）时，落库的 `project_id` 为空串 `""`，而 #5713 之后的约定是由后端
物化为 `default`（work 模式）/ `default_code`（code 模式），落库自描述。

影响范围：

- 侧栏定时任务分组依赖 `project_id` 精确匹配（`jobBelongsToProject`），物化值缺失后
  只能靠前端把空串兜底成 work 默认项目，code 模式的任务会被归错组。
- 与 TUI 创建（入口无条件经 AgentServer 解析，物化正常）、agent 工具链
  （server 侧 `resolve_cron_project_binding`，物化正常）行为不一致，仅 web 手动
  创建链路回归。

回归窗口：`0722806d2`（2026-10-30 企业版特性握手）合入之后创建的未选项目任务，
落库均为 `""`（本机 `~/.jiuwenswarm/agent/home/cron_jobs.json` 中 10-26/27 的任务
仍为 `default`，10-30 后无未选项目任务，与窗口吻合）。

## 关联设计与当前契约

无独立 D。行为契约来自 #5713（4b00671f6）确立的口径：

- server 侧 `jiuwenswarm/server/runtime/session/project_store.py`
  `resolve_cron_project_binding`：未选项目归属调用方 work_mode 的默认项目，
  落库 `project_id` 自描述，侧栏按 `project_id` 精确分组，不依赖空串兜底。
- 前端 `cronProjectDisplay.ts`：空串仅作为存量数据兼容，新任务物化
  `default`/`default_code`。

gateway 侧 A3g（f953922da）之后的约束：controller 把绑定结果当不透明值持久化，
gateway 本地项目表反查已删除；真实 `project_id`/`project_dir` 必须先经目标
AgentServer 解析。本次修复不触碰该约束：空编号物化只做 work_mode → 保留编号的
常数映射，不查项目表。

## 根因与修复决策

提交时间线（develop）：

1. `f953922da` A3g（10-20）：删除 gateway 本地 project_store 反查，绑定统一经
   目标 AgentServer（`PROJECT_CRON_RESOLVE_BINDING`）解析。
2. `4b00671f6` #5713（10-26）：server 侧解析把空值映射为 default/default_code；
   web 入口 `_cron_job_create` **无条件**调用解析并把结果写回 `params["project_id"]`
   ——未选项目由此物化落库。
3. `0722806d2` 企业版特性握手（10-30）：新增 `_cron_requests_real_project` 门控
   （"选了具体项目才需要问 Agent。空编号和默认项目不用问"），空项目跳过解析且
   删除了结果写回；controller `resolved_project_id = raw_project_id` 透传空串落库。

根因：`0722806d2` 为省掉空项目的 AgentServer 往返而跳过整段解析时，把 #5713 挂在
该往返上实现的默认项目物化一并去掉了——"能不能不问 Agent"的优化判断没有识别出
这一步还承载着落库自描述的行为契约。

修复决策：在 web 入口 `_cron_job_create` 的非真实项目分支**本地**物化——
`resolve_request_work_mode(params, "web")` 解析 work_mode 后经
`resolve_default_project_id` 写入 `params["project_id"]`。既恢复 #5713 的落库行为，
又保留企业版"空项目不问 Agent"的优化；物化规则与 server 侧
`resolve_cron_project_binding` 的空值分支一致。

备选与拒绝原因：

- 恢复 #5713 原机制（入口无条件调 AgentServer 解析）：重新引入企业版合入想避免的
  往返，与 0722806d2 的方向正面冲突。
- 在 gateway controller 统一物化空编号：会顺带改变 proactive 等系统路径的落库
  契约（`test_create_job_without_marker_allows_default_project_and_no_fields` 明确
  断言系统路径落 `""`），超出本次回归范围。

边界处理：显式 `default`/`default_code` 编号原样透传不覆盖；非法 `work_mode`
不物化，留给 controller 按原有规则拒绝；`project_dir` 空串保持不动（controller
本就按缺失处理）。

## 回归证据与适用范围

候选：分支 `fix/cron-web-default-project-materialize`（基于 develop 01c69908f）。
环境：本机 Windows + 仓库 `.venv`。

- `tests/unit_tests/gateway/test_app_web_handlers.py` 全文件：309 passed /
  14 failed；14 个失败均为 a4p RPC 与 external CLI 探测用例，在改动前基线
  （stash 后复跑）上同一批失败，属既有环境性失败。
- 定向验证（全文件顺序）：
  - `test_agentos_cron_create_does_not_read_gateway_session_metadata[web|feishu:im-app]`
    扩展断言 `project_id == "default"`，通过；
  - 新增 `test_cron_create_without_project_materializes_default_id`
    （work→default / code→default_code / 未传 work_mode→default，payload 带
    `project_dir: ""` 模拟前端真实形状，且断言不问 Agent、不打 marker），通过；
  - 新增 `test_cron_create_keeps_explicit_default_project_id`（显式
    `default_code` 原样保留），通过；
  - 既有 `test_cron_create_with_project_asks_the_agent`（真实项目走 Agent 解析）
    不受影响，通过。
- `tests/unit_tests/gateway/test_cron_controller_project_binding.py` 全部通过
  （controller 契约未动）。

未验证/限制：

- 单跑或 `-k` 定向跑 `test_app_web_handlers.py` 中 cron 用例时，会命中既有的
  `ReqMethod.WORKSPACE_TREE` 枚举加载顺序问题（基线单跑同样失败），与本修复无关；
  验证须以全文件顺序为准。
- 未做端到端验证（未起调试服务实际建任务核对落库）；逻辑行为由上述入口级单测
  覆盖，落库物化值与 `cron_jobs.json` 实际数据的核对待联调环境确认。

## 防错模式与资料同步

- 模式：入口跳过某个链路步骤做优化前，盘点该步骤附带的**行为副作用**（本例：
  AgentServer 解析往返同时承担默认项目物化），副作用需在跳过点本地补偿。
- 已同步：`cronProjectDisplay.ts` 顶部注释补上回归窗口的存量说明（"仅 #5713
  之前的存量"不准确，10-30 后的回归窗口内同样产生空串数据）。
- 未新增独立 spec：物化口径仍由 #5713 既有约定与本文档承载，无契约形状变化。

## 遗留与后续

- 回归窗口（10-30 ~ 本修复合入）内已落库为 `""` 的任务不迁移，与 #5713
  "不迁移存量数据"口径一致；前端 `jobBelongsToProject` 空串兜底继续兼容。
- `jobBelongsToProject` 把空串一律归 work 默认项目，code 模式存量 `""` 任务在
  侧栏会归到 work 默认组——#5713 时代即存在的既有行为，本次未扩大范围处理；
  若后续需要按 work_mode 区分兜底，另立修复。
