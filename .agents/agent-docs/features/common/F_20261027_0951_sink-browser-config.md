# browser_config 下沉到 common

## 关联依据

- 设计：无独立设计编号。分层规则见 `scripts/check_layering.py` 的 R3，原白名单注明待 #8083 稳定后下沉。
- spec：无对应 spec 变更。对外请求仍是 `path.get` / `path.set`，读写字段不变。
- 日期：2026-10-27。
- 实际实施范围：把 `browser_config` 从 `jiuwenswarm.agents.harness.common` 移到 `jiuwenswarm.common`，并改完全部仓内 import。gateway 本地回退与 AgentServer `config_adapter` 继续调用同一对函数。

## 实际实现与开发决策

模块级依赖只有 `common.browser_settings` 和 `common.config.resolve_env_vars`，`openjiuwen` 的 Jev 校验留在函数内部。因此整文件下沉，而不是把校验写进只做投影的 `browser_settings`。

`resolve_chrome_path` 与 `apply_browser_decision_config` 随文件一起移动，避免读写投影和运行时套用分成两处。gateway 的非 legacy 路径仍然只代理到 AgentServer，不在这次删除本地回退。

## 与设计的偏差

无。分层脚本里的预定收法就是下沉 common，并缩掉 R3 白名单中的 `agents.harness.common.browser_config`。

## 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 / 复现步骤 / 记录链接 | 实际结果 | 未覆盖项 / 失败原因 |
| --- | --- | --- | --- | --- |
| 2026-10-27 分层规则 | 基于 upstream/develop `06c89c7a7`，本机 `.venv` Python 3.13 | `.venv/bin/python -m pytest tests/unit_tests/test_layering.py -q`；`scripts/check_layering.py --list-deps` | 分层测试通过。gateway 直接外部依赖从 21 降到 20，agents 不再出现 | 未跑完整 `make test` |
| 2026-10-27 浏览器配置行为用例 | 同上 | `tests/unit_tests/common/test_browser_decision_config.py`、`tests/agents/swarm/test_electron_browser_fallback.py` | 未能收集 | 本机已安装的 openjiuwen 没有 `openjiuwen.harness.subagents.cua_agent`，这两个文件在收集阶段导入 `code_subagents` 失败。该导入在移动前就存在 |

## 验证结论

- 已证明范围：R1/R2/R3 无新增违规，gateway 不再 import `agents.harness.common.browser_config`。
- 未覆盖或未通过的承诺：浏览器决策与 Electron 回退用例未在本机跑通，原因是依赖缺失，不是本次 import 路径本身。
- 可支持的交付判断：分层边界已按预定收法落地；行为用例需要在带 Jev/CUA 的 openjiuwen 环境复跑。

## 已知遗留

| 遗留项 | 未处理原因 / 性质 | 影响与现有处置 | 后续入口 / 条件及所需验证 |
| --- | --- | --- | --- |
| gateway 仍通过 `server_store_bridge` 延迟 import server 存储 | 范围外；拆仓时改为 HTTP | 同机部署继续可用 | 删除 `gateway/server_store_bridge.py` 后重跑 `scripts/check_layering.py` |
| extensions、observability、diagnosis、`dotenv_early` 仍被 gateway 直接引用 | 范围外 | 不影响本次 R3 的 agents 边 | 拆仓前按依赖清单逐项处理 |
