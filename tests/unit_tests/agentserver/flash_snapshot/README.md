# Flash 提示词离线快照（M0 S1）

本目录是 flash 提示词重构的**离线组装 + golden 快照基线**：用真实 Deep 提示词构建函数
+ flash 转换 rail 的纯函数/常量，离线重建 flash 自有静态段，落盘 golden JSON/text，
CI 用 `test_flash_prompt_offline.py` 逐级比对 hash，锁定任何上游内容或 flash 改写逻辑的漂移。

## 文件

| 文件 | 作用 |
|------|------|
| `flash_prompt.{cn,en}.json` | golden 快照（段级/区级/整段 SHA256 + 正文） |
| `flash_prompt.{cn,en}.txt` | golden 正文（人读，逐段带 hash 头） |
| `_generate.py` | 本地（重）生成 golden 的脚本（CI 不需要） |

组装逻辑在 `jiuwenswarm/agents/harness/flash/prompt/offline.py`，hash/序列化在
`jiuwenswarm/agents/harness/flash/prompt/snapshot.py`。

## v2 覆盖与排除

**覆盖（确定性、平台无关）**：五域，共 14 段：

- safety：`build_safety_section` → `_transform_safety` → 030/100/110/120
- response：`_THINKING_CN/EN`（410 三矛盾修正正文，修复.md ①-⑤）+ flash 改写常量 500/510/520/540
- tools：300/330/340（content.tools 六份 CN/EN 目标正文）
- todo：430（`_TODO_DISCIPLINE_*` + `NO_MODEL_SELECTION_PROMPT`）
- subagent：440（`FLASH_TASK_SECTION`）

**排除（各自原因）**：

- **persona**：identity 源正文依赖 `sys.platform` / shell 环境 / 配置目录 / 沙箱开关，
  跨 dev/CI 不可复现；其 flash 自有常量（020/200/220）已由 `test_flash_persona_rail` 锁定。
  待 `offline.py` 支持显式平台注入后纳入。
- **relay_merge / skill**：归 relay（relay TS 内容）或内容动态，留待后续重构步纳入。

## 本地（重新）生成

本地缺 `agent_shield`，flash rail 的 `DeepAgentRail` 基类 import 会失败；`_generate.py`
在 import 前打叶模块桩绕过（仅基类/包占位，不影响组装结果）。要求 `agent-core`
（dev-stable openjiuwen）与本仓同级，可用 `AGENT_CORE` 环境变量覆盖：

```bash
cd <jiuwenswarm-1102>
python tests/unit_tests/agentserver/flash_snapshot/_generate.py
```

## CI 验证

`tests/unit_tests/agentserver/test_flash_prompt_offline.py` 在 CI（`agent_shield` 已装、
`openjiuwen` 为 dev-stable）下直接 `import offline`，重新组装并与 golden 比对：

- 段序（段号即 priority）与 `sections.py` 领号一致；
- 组装确定性（两次 hash 相等）；
- `full_hash` / `static_hash` / `band_hashes` / `section_hashes` 与 golden 一致。

**漂移即失败**：上游安全正文、思考纪律红线措辞、或任何 flash 拆分/改写逻辑改动后，
golden 比对失配，提示更新 golden 前先走「是否刻意」裁决。
