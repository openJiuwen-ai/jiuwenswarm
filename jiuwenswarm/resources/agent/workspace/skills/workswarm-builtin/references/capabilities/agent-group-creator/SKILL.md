---
name: agent-group-creator
description: 从自然语言创建可被 JiuwenSwarm 加载和安装的专家团（agent_group）包，包含 Leader、专家成员、协作指令及可选共享技能。用户提出“创建专家团”“组建专家团队”“做一个多专家协作团队”时使用；创建单个专家使用 agent-creator，执行已有专家团的业务任务不使用本技能。
---

# 专家团创建器

将用户需求转化为可复用的专家团资产，而不只是本次对话中的临时分工。沿用专家创建的“明确需求 → 填充包 → 校验 → 注册”流程。

## 1. 明确目标和分工

从用户描述中提取名称、擅长领域、典型任务、交付物和约束。信息足够时直接设计；只询问会影响成员选择或工作边界的缺失信息，不要求用户填写英文名称或技术字段。

设计一位 `leader` 和完成任务所需的专家成员，避免职责重复。明确各成员接收什么输入、负责什么、向 Leader 返回什么，以及 Leader 如何处理分歧和验收成果。已有专家满足需求时可复用其模板；缺少角色时直接在本团包内编写成员模板，无须将每个成员另行注册为独立专家。

本技能只创建新资产。遇到同名包 ID 时保留现有内容并选择新 ID。注册时若提示展示名称重复，根据用户原始需求选一个不重复且贴切的展示名称，更新包内对应内容后重试，并在交付时告知最终名称；不要覆盖已有专家团。若用户要求替换已有专家团，先明确替换范围，不通过删除旧包绕过重名保护。

## 2. 填充专家团包

读取 [references/package-spec.md](references/package-spec.md)，在当前任务的可写目录创建 `<group-id>/`。ID 使用小写 kebab-case，目录名必须等于顶层 manifest 的 `name`。

包内包括根 `manifest.json`、`README.md`，以及每个角色的 `manifest.json` 和 persona。Leader 另有 `AGENT.md`，普通成员不得有 `AGENT.md`。共享技能只在实际需要时添加。

不要直接写安装目录或 marketplace.json，不改内置资产。专家团与单专家路径不同：最终由运行时导入至 `<JiuwenSwarm 用户工作区>/.agent_teams/agent_groups/local/<group-id>/`。由运行环境确定用户工作区，不硬编码用户路径或借用 agent-creator 的注册脚本。

复用已有模板时复制其实际资源并检查相对路径；不使用符号链接，不凭名称声明并不存在的技能。只复用用户授权的能力，不自动生成 MCP、模型密钥或连接器配置。

## 3. 校验

使用当前 JiuwenSwarm 会话的运行时执行脚本，不判断源码、虚拟环境或 EXE 等启动形态，也不要凭 PATH 猜测 `python3`/`python`。宿主会提供可执行当前脚本的运行时（冻结应用通过 `JIUWENSWARM_EXECUTABLE` 提供；源码运行时使用当前会话已经使用的 Python）。如果运行时支持脚本参数，直接传入脚本路径；不要对冻结应用使用 `-c`。

`<runtime>` 表示当前会话的 JiuwenSwarm 运行时，不是用户机器上随意搜索出来的 Python。`<skill_dir>` 为本技能所在目录，包路径使用绝对路径：

```bash
<runtime> <skill_dir>/scripts/register_group.py /absolute/path/<group-id> --validate-only
```

注册步骤使用完全相同的运行时，只移除 `--validate-only`：

```text
<runtime> <skill_dir>/scripts/register_group.py /absolute/path/<group-id>
```

脚本调用真实专家团加载器，验证每个成员模板、persona、Leader 规则和共享技能。出现 `RESULT: PASS` 后才进入注册。失败时修正具体报错；缺少运行依赖时说明阻塞，不跳过校验或宣称已创建成功。

同时检查语义质量：成员职责是否互补、共享指令是否与 persona 一致、是否覆盖用户要求、是否清楚说明交付物及验收标准。加载成功不代表这些内容自动合格。

## 4. 注册

```bash
<runtime> <skill_dir>/scripts/register_group.py /absolute/path/<group-id>
```

注册会再次校验，复用产品导入和安装流程复制包，并登记为可直接使用的本地专家团；重名会报错，不覆盖现有资产。只有同时输出 `REGISTERED:` 和 `INSTALLED:` 才表示注册完成。勿用直接写 marketplace 或跳过错误的方式伪造成功。

## 5. 交付

简要告知专家团名称、Leader 与成员分工、已注册并安装的 ID 和最终包位置，并给出 2–3 条推荐提问。提示用户到“专家 → 我的专家 → 专家团”找到新资产并直接使用，不要替用户执行专家团的业务任务。
