# SkillDev Resource Sync 变更说明

本文档说明 `jiuwenclaw/agentserver/tools/resource_sync.py` 相关改动的设计目标、目录约定、同步规则和测试关注点。

## 背景

SkillDev 任务会从前端或外部通道接收多类资源：普通参考文件、参考 Skill 包、工具定义、Agent 定义和 CLI 定义。旧逻辑容易把资源当作“追加写入”处理，导致用户删除或替换资源后，工作区中仍残留过期文件。
本次改动把资源同步逻辑收敛到 `jiuwenclaw/agentserver/tools/resource_sync.py`，并由 `skilldev_agent/adapter.py` 统一调用。核心目标是让每次请求携带的资源参数成为工作区资源目录的事实来源。

## 实现
主要关注参考文件、参考 Skill 包、工具定义， 而Agent 定义和 CLI 定义先不用改动，保持原有追加式逻辑。
根据已有的resource_state.json文件和当前轮次传入的param（write_uploaded_resources的参数），param和json取交集得到A文件集合(已经存在的文件，无需重复写入），则应该删掉json - A的内容（另外构建函数完成删除动作），param-A的集合保存到工作区，最后更新json。

## 规范
尽量不改动原有函数，非必要不新增代码。
保持函数单一职责，可读性强，注释清晰。

## 涉及文件

  - `jiuwenclaw/agentserver/tools/resource_sync.py` 在通用 tools 目录。
  - 负责资源写入、增删同步、状态记录、提示构造。

  - directImport 成功后记录直接导入的包名，导入的skill存放与 `skill/` 目录，记录到 `resources/resource_state.json` 中，避免后续重复进入 `ref-skills/` 或 `ref-files/`。

## 工作区布局

一次 SkillDev 任务的资源会落到当前任务工作区：

```text
<task_workspace>/
├── skill/
├── evals/
├── output/
└── resources/
    ├── resource_state.json
    ├── ref-files/
    ├── ref-skills/
    ├── available-tools/
    │   ├── <pluginId>__<toolName>.json
    │   └── tool_usage.json
    ├── agents/
    │   └── available_agents.json
    └── clis/
        └── available_clis.json
```

其中：

- `ref-files/`：普通参考文件。
- `ref-skills/`：参考 Skill 包，支持 `.zip` 和 `.skill`。
- `available-tools/`：工具定义文件以及工具使用目录。
- `agents/available_agents.json`：可用 Agent 定义。
- `clis/available_clis.json`：可用 CLI 定义。
- `resource_state.json`：资源状态表，目前用于记录 directImport 导入过的 Skill 包文件名。

## 输入参数

`write_uploaded_resources(task_workspace, params)` 支持以下资源字段：

```text
files
skill_packages / skillPackages
tool_spec_files / toolSpecFiles
skill_searched
```

普通文件和 Skill 包支持来源：

- URL：`url`

文件名从 `filename` 或 `name` 读取。

## 同步规则

### 普通文件与 Skill 包

新增函数把目标目录同步成当前请求参数描述的状态：

1. 新文件不存在时写入。
2. URL 文件已存在时跳过下载，避免重复拉取。
3. 把文件名和url都作为文件签名保存到`resources/resource_state.json`。

### directImport 去重

directImport 的语义是“把用户给的 Skill 包直接导入到 `skill/` 目录作为当前 Skill”。这类包不应该再次出现在 `resources/ref-skills/` 或 `resources/ref-files/` 中。

流程如下：

1. `adapter.py` 收到 directImport 请求。
2. `collect_skill_packages(params)` 收集包信息。
3. `extract_packages_to_skill_dir(skill_dir, packages)` 解压到 `skill/`。
4. `record_direct_imported_skills(task_workspace, packages)` 把包名写入：

```text
resources/resource_state.json
```

状态示例：

```json
{
  "direct_imported_skills": [
    "example.skill"
  ]
}
```

后续 `write_uploaded_resources()` 会读取该状态，并在同步 `ref-files/` 和 `ref-skills/` 时排除这些文件名。

### 工具定义同步

支持输入：

1. 直接对象：从 `pluginId` / `bundleName` 和 `toolName` 推导工具身份。

每个工具定义写成：

```text
resources/available-tools/<pluginId>__<toolName>.json
```

当前请求中不存在的旧工具定义文件会被删除。

同步规则：

- 参数存在且json文件不包含时写入。
- 参数不存在且旧文件存在时删除旧文件。

## 错误处理

- `resource_state.json` 读取失败或 JSON 损坏时，记录 warning 并回退到空状态。
- `resource_state.json` 写入失败时，记录 warning，不中断主流程。
- Skill 包后缀非法时抛出 `ValueError`。
- URL 资源下载失败会沿下载逻辑向上抛错。

## 设计约束

1. `write_uploaded_resources()` 是同步语义，不是追加语义。
2. `resource_state.json` 只用于资源同步状态，不参与 Skill 运行时逻辑。
3. directImport 导入的包只排除 `ref-files/` 和 `ref-skills/`，不影响工具、Agent、CLI 定义同步。
4. URL 模式目前以“文件存在即跳过下载”为准，不做远端内容哈希比对。
5. 过期文件删除只针对文件。

## 测试关注点

建议覆盖以下场景：

- `resource_state.json` 路径、读写、损坏 JSON 回退。
- directImport 包名记录和去重。
- `ref-files/` 新增、更新、删除过期文件。
- `ref-skills/` 只允许 `.zip` / `.skill`。
- directImport 包不会重复进入 `ref-files/` 或 `ref-skills/`。