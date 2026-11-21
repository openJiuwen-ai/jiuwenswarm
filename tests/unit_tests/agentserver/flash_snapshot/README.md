# Flash 提示词离线快照（M0 S1）

本目录用真实 Deep 提示词构建函数与 Flash 转换 rail 的纯函数和常量，生成可逐段比较的中英文快照。
仓内 JSON 和 TXT 记录历史输出。普通文案修改只需更新提示词源码。

## 文件

| 文件 | 作用 |
|------|------|
| `flash_prompt.{cn,en}.json` | 历史快照（段级/区级/整段 SHA256 + 正文） |
| `flash_prompt.{cn,en}.txt` | 历史正文（人读，逐段带 hash 头） |
| `_generate.py` | 将当前快照写入指定目录 |

组装逻辑在 `jiuwenswarm/agents/harness/flash/prompt/offline.py`，hash/序列化在
`jiuwenswarm/agents/harness/flash/prompt/snapshot.py`。

## v2 覆盖与排除

**覆盖（确定性、平台无关）**：默认上下文覆盖五域，包括 14 个静态段和一个按工具面启用的动态段：

- safety：`build_safety_section` → `_transform_safety` → 030/100/110/120
- response：`_THINKING_CN/EN`（410 三矛盾修正正文，修复.md ①-⑤）+ flash 改写常量 500/510/520/540
- tools：300/330/340，另有按工具面启用的 browser_routing（345）
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
python tests/unit_tests/agentserver/flash_snapshot/_generate.py <output-dir>
```

## CI 验证

默认单测检查双语段序、注册信息、载体、正文非空、工具门控及组装确定性。导出测试逐段比较当前源码的组装结果与 JSON 的段名、优先级和正文。

需要审查文案变化时，分别用目标版本和候选版本锁定的依赖生成快照，比较同一语言的逐段文本。要求逐字保持输出的重构可用该差异作为合入条件。
