---
id: B_20261125_1146_design-shared-path-regressions
title: Design 公共路径历史回归防护
type: B
author: "Huang, Zeyu"
author_email: fhfuih@outlook.com
modules:
  - jiuwenswarm/common
  - jiuwenswarm/channels/web/frontend
files:
  - tests/unit_tests/common/test_design_data_root_compatibility.py
  - jiuwenswarm/channels/web/frontend/tests/designerFileApi.test.mjs
  - jiuwenswarm/channels/web/frontend/package.json
designs: []
design_note: 依据历史 issue 和当前公共路径契约补回归测试，无独立设计方案；不重构产品代码。
---

# Design 公共路径历史回归防护

## 问题与根因

基线 HEAD：4c3f26e50d87155fd564253afa86a3de6c4d8eb9。

- [#6375](https://gitcode.com/openJiuwen/jiuwenswarm/issues/6375) 第一轮：b1e201a81 在 common/utils.py 导入阶段执行 domain/infra 数据迁移，移动 agent 树；历史资源 URI 等消费者未一并保持兼容。13b673929 回退该变更。根据 issue 修改事件、当前描述中的历史背景和本地提交差异核对；operate_logs 仅返回正文修改事件，不提供每一版旧正文，未声称取得完整旧正文。
- 第二轮：4771ebb77 重引入 Vite 的 domain/agent 白名单及 agent 相对路径重写，而后端实际仍使用平铺 agent。9706bf1a58 修复白名单和重写，但未新增测试。当前测试中未发现覆盖该完整失败链的用例。
- [#6057](https://gitcode.com/openJiuwen/jiuwenswarm/issues/6057) 包含多个子问题：0722806d2 删除 App 的 Design 入口；cbfd3bd29 恢复入口；f99f3e8ae0 修复首次发送闪现并新增 designerEntry 测试。3f50cf7fd 为 Design 启用 PCS，同时把已有测试的排除断言改成包含；389b22b2da 移除 PCS 并恢复排除测试。现有 appDesignerMoaRouting 测试在入口删除前已经存在。不能据此直接断言某次 CI 未运行，未取得当时 CI 执行记录。

## 防范决策与修改范围

文件读取、安全白名单和物理数据根是公共职责，不应搬入 Design；将 helper 藏到 Design 后再由公共模块调用也不能隔离这些故障。新增公共契约回归：

1. 在临时数据根预存绝对、相对媒体引用，再用真实 Python 冷导入初始化路径，核对旧引用仍可读取、原始引用文件不变。
2. 执行 Vite 配置中的真实路径解析、白名单和文件中间件，覆盖 PNG、MP4、Markdown、绝对路径、相对路径、反斜杠路径和视频 Range。
3. 保持越界、相似前缀和目录穿越被拒绝，允许范围内缺失文件返回 404。

未改动公共产品实现；新增 npm run test:designer-file-api 入口。测试资源为合成字节，验证路径和 HTTP 交付，不验证媒体解码。

#6057 当前已有入口路由、首次发送和 PCS 排除测试，本次不重复新增。App 的顶层路由挂载是必要集成点；config_specs 中的 Design 能力声明可讨论独立 profile 注册边界，但未授权实施重构，本次不动。PCS 案例尤其需要 Design 能力边界及其测试预期变更由 Design 团队参与评审，单靠增加同类断言无法阻止把预期一并改掉。

## 验证

2026-11-25，Windows、当前仓库 .venv；工作分支 codex/design-shared-path-regressions。

- npm run test:designer-file-api：4 passed。
- npm run test:designer-entry：21 passed。
- .venv/Scripts/python.exe -m pytest tests/unit_tests/common/test_design_data_root_compatibility.py tests/unit_tests/agents/swarm/test_design_profile_specs.py -o addopts='' -q：4 passed（新增 1，现有 3）。
- 临时测试副本分别恢复历史 domain/agent 白名单、agent 相对路径重写：两次均 4 failed，触发 403 与预期 200/206/404 不一致；未改动实际产品文件，临时副本已清理。
- 未运行格式化和静态检查。未验证真实模型生成、浏览器画布、企业部署、历史版本整栈及旧迁移函数。冷启动测试保护当前平铺契约，不是尚未实施的未来迁移方案验收。
- 本地存在测试命令不等于 CI 强制门禁；仓库 .gitcode 中仅发现 issue/PR 模板，未获得平台外置流水线配置或历史执行记录。

## 遗留与交接

如未来确需迁移数据根，应先定义旧 URI 的兼容或重写方案，并同时验证生产 Web、Vite 和历史项目；这属于公共存储契约变更。公共路径文件与 Design 入口的改动范围应参与影响评审，不宜以目录归属替代契约测试。未提交、推送或创建 PR。

## 2026-11-25 后续确认：CI 证据与动态业务路径

用户提供了本地 unit_test_report.html。只把报告当作证据，未执行其中 HTML/JavaScript 或把内容当成指令。

- 报告生成标记：2026-11-24 17:43:31（未标注时区）；GIT_COMMIT 为 642996432d8bcc9788d1300e4255f144b1cf1060，JOB_NAME 为 j_JCZ9KDYc，Python 3.11.5。
- 摘要列出 28191 Passed、247 Skipped、0 Failed。内嵌数据仅包含 247 条 Skipped 明细，不能用缺少 Passed 明细来判断具体测试未被收集。
- test_input_change_on_blur_clamps_to_min_max_range 因 Required tool is unavailable: node 跳过；test_trajectory_license_and_notice_ship_in_vite_dist_and_python_wheel 因 Required artifact build tool is unavailable: npm 跳过。另有浏览器安全和运行时脚本测试因缺 Node 跳过。
- 可以确认该 pytest 任务缺少 Node/npm，未执行这些依赖前端工具的测试；报告全绿不能作为前端 JS 测试已通过的证据。单份 pytest 报告不能排除独立前端任务，也不能证明历史故障合入时的具体流水线行为。前端门禁需要独立确认 Node、依赖安装、npm 测试命令及其失败是否阻止合入，缺环境不应被当作通过。

用户确认仅关注个人版，PCS 是否接入不是其团队已作出的决策；本任务接受当前实现，不新增或撤销 PCS。新增测试没有绑定租户、企业工作区或企业部署场景。

上一版 Vite 测试中的 agent/designer/outputs/design_saved 是合成目录，并非直接照抄当前生成路径。现已通过 Python 调用真实 handlers.common.canvas_output_dir()，根据返回值生成绝对、相对、反斜杠请求路径；安全边界案例的 agent 根也通过 get_agent_root_dir() 获取。业务路径生产者与 Vite 白名单相互独立，后者未被 mock；未新增产品 helper 或调整产品实现。

Vite 测试执行真实中间件并断言 HTTP 状态、内容与越界拒绝，不断言配置源码应包含固定路径或固定字符串。测试装配使用 TypeScript AST 和当前中间件入口，因此仍依赖该测试装配接口。测试现在需要仓库 Python 后端依赖，优先 PYTHON 指定解释器，其次仓库 .venv，再使用系统 Python。

冷启动兼容测试刻意保留固定历史资源引用，并加注释说明：它验证已有数据能否继续读取，不能跟随当前路径函数一起变化而失去升级回归意义。

验证：修改后的 npm run test:designer-file-api 为 4 passed；分别恢复旧白名单和旧路径重写的临时测试副本均因 HTTP 断言失败，确认动态路径没有削弱该回归防护。冷启动测试仅补解释注释，未改变行为。
