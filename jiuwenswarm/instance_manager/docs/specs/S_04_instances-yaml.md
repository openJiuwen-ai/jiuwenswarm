# instances.yaml 配置管理

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_04 / instances-yaml |
| 关联模块 | jiuwenswarm/instance_manager/yaml.py |
| 最近一次修订日期 | 2026-10-14 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管**具名实例注册表 instances.yaml**：路径布局、读取校验链、原子写、模板创建、
条目更新与 index 计算。条目里 ports 的数值语义（自动分配 / 冲突）归 [[S_03]]；从 yaml
条目装载回 `InstanceConfig` 归 [[S_06]]；名称合法性规则本体归 [[S_02]]。
default 实例**不进本文件**（[[S_02]] N3）。

## 不变量

- **N1 路径三件套**：`get_instances_yaml_path()` = `~/.jiuwenswarm/instances.yaml`
  （经 common.utils `get_user_home()`）；`get_instances_dir()` =
  `~/.jiuwenswarm-instances/`；`get_instance_workspace_path(name)` =
  `~/.jiuwenswarm-instances/<name>/`。这三处是唯一落点真源，禁止在别处拼同一路径
  （早期引导路径 [[S_07]] N2 的就地重复是受控例外，见该 spec）。
- **N2 读取宽进严校**：文件不存在 / 内容为空 → 返回 `{"instances": {}}`（合法空态，
  不报错）；一旦有内容，则必须：顶层是 dict、有 `instances` 键（值 None 归一为空
  dict）、每个条目过 `_validate_instance_entry`（名称规则 [[S_02]] N1、data 为 dict
  或 null、ports 为 dict 且键 ∈ PORT_TYPES 且值为 1–65535 整数、workspace 为 str）。
  任何一条不过即抛 `InstancesYamlError`（消息带路径 + 修复建议，[[S_02]] N6）——
  **不做静默跳过**：坏条目静默跳过会让实例"消失"，比启动失败更难排查。
- **N3 写入必原子**：`save_instances_yaml` 一律写**同目录** temp 文件（`tempfile.mkstemp`，
  前缀 `.instances.` 后缀 `.tmp`）再 `os.replace` 换入。保证读者永远看不到半写文件
  （并发启动两个具名实例时都走 update 路径）；任何失败路径必须清理孤儿 temp。
  注意：原子换入只保证**不损坏**，不保证**不丢更新**——两个并发 updater 交错
  读-改-写仍可能丢失一方的更新，该竞争由每实例 `InstanceLock`（[[S_05]]）串行化。
- **N4 index = 声明顺序**：`get_instance_index(name)` = 条目在 `instances` 键下的
  位置 + 1（**从 1 起，0 保留给 default**，[[S_03]] N2）；不在 yaml 中的新名字返回
  `len(instances) + 1`（即将追加的位置）。**yaml 无序列表语义**——Python dict 保插入
  序，手编 yaml 改顺序 = 改端口。
- **N5 update 写全量条目**：`update_instances_yaml` 落盘的条目固定为
  `{"workspace": str(workspace), "ports": <dict>}`；ports 未提供时按 N4 的 index 自动
  整组分配（[[S_03]]）。已存在的同名条目被整体覆盖（幂等再注册）。
- **N6 模板只建一次**：`create_instances_yaml_template` 仅在文件不存在时写入带示例注释
  的模板；已存在即原样返回，**永不覆盖**用户文件。

## 接口契约

```python
# yaml.py
def get_instances_yaml_path() -> Path              # ~/.jiuwenswarm/instances.yaml（N1）
def get_instances_dir() -> Path                    # ~/.jiuwenswarm-instances/（N1）
def get_instance_workspace_path(name: str) -> Path # ~/.jiuwenswarm-instances/<name>/（N1）

def load_instances_yaml() -> dict
    # 返回 {"instances": {name: {workspace, ports} | None}}
    # 不存在/空 → {"instances": {}}；结构或条目非法 → InstancesYamlError（N2）

def save_instances_yaml(data: dict) -> None        # 原子写（N3）
def create_instances_yaml_template() -> Path       # 不存在才建（N6）
def update_instances_yaml(name: str, workspace: Path, ports: dict | None = None) -> None
def get_instance_index(name: str) -> int           # 声明顺序 +1；新名字 len+1（N4）
```

## 数据结构

instances.yaml 形状（模板示例即文档）：

```yaml
instances:
  alice:
    # workspace: /custom/path/alice   # 可省，默认 ~/.jiuwenswarm-instances/alice
    ports:
      agent_server: 28092
      web: 29000
      gateway: 29001
      frontend: 15173
  bob: {}                             # 最小声明，全部自动分配
```

| 键 | 类型 | 必填 | 语义 |
|---|---|---|---|
| `instances` | dict | 是（顶层唯一必需键） | 具名实例注册表；default 永不出现 |
| `<name>` | dict 或 null | — | null = 最小声明，workspace / ports 全默认 |
| `workspace` | str | 否 | 显式 workspace；省略走 N1 默认路径 |
| `ports` | dict | 否 | 显式端口；省略按 index 整组自动分配（[[S_03]]） |

## 执行路径

1. `jiuwenswarm-init --name X`：校验名称（[[S_02]]）→ 建 workspace →
   `update_instances_yaml`（N5，auto ports）→ `create_bootstrap_env`（[[S_07]]）。
2. `jiuwenswarm-start --name X`：`load_instances_yaml` → `get_instance_config`
   （[[S_06]]）→ 端口检查 / 回退（[[S_03]]）→ 端口有变则再 `update_instances_yaml`
   持久化回退结果。
3. 并发启动：`InstanceLock`（[[S_05]]）先拿锁，串行化 2 的读-改-写（N3）。

## 边界 case 清单

| 场景 | 现行为 | 测试覆盖 |
|---|---|---|
| 文件不存在 | 返回空态，不建文件（建文件是 template 的职责） | TestInstancesYaml |
| 顶层是列表 / 字符串 | InstancesYamlError："expected dict" + "应以 instances: 开头" | TestInstancesYaml |
| 缺 `instances` 键 | InstancesYamlError + "Add 'instances: {}'" 建议 | TestInstancesYaml |
| `instances:` 值为 null | 归一为空 dict，合法 | TestInstancesYaml |
| 条目值 null（`bob:`） | 合法最小声明 | TestInstancesYaml |
| ports 里未知键（`http: 1234`） | InstancesYamlError，列出合法 PORT_TYPES | TestInstancesYaml |
| 端口 0 / 70000 / 字符串 | InstancesYamlError："must be 1-65535 / integer" | TestInstancesYaml |
| workspace 是数字 | InstancesYamlError："must be a string path" | TestInstancesYaml |
| YAML 语法错（缩进/重复键） | InstancesYamlError + "run 'jiuwenswarm-init' to recreate" | TestInstancesYamlError |
| 保留名条目（`all:`） | InstancesYamlError（名称校验复用 [[S_02]]） | TestInstanceNameValidation |
| 写入中断 / 失败 | temp 清理，原文件不动（N3） | TestInstancesYaml |

## 已知遗留

- 模板注释中保留名只列 `default, config, tmp`（实际 5 个，见 [[S_02]] N2）、index 0
  端口示例只列 4 个（实际 5 个，http_api=8766 缺席）——注释滞后，修注释时同步
  [[S_01]] 遗留清单。

## 修改检查单

- 改 yaml 形状（新增条目键等）→ 同步 N2 校验链、`get_instance_config` /
  `load_all_instance_configs` 装载投影（[[S_06]]）、模板注释、`TestInstancesYaml`。
- 改 `get_instance_index` 语义 → 全部 index 消费点（[[S_03]] collect_all_ports 的
  就地计算、[[S_07]] 早期路径的就地计算）同步——那两处是**复制逻辑而非调用**，漏改
  即分叉。
- 改路径三件套 → grep `~/.jiuwenswarm-instances` / `.jiuwenswarm/instances.yaml`
  字面量（含 [[S_07]] 早期路径与 dotenv_early.py），并评估存量用户目录迁移。
- 改 `save_instances_yaml` → 保持原子语义（N3）与孤儿 temp 清理；不得退化为直接
  write_text。

## 与其它 spec 的关系

- 端口数值的分配与冲突：[[S_03]]
- 名称规则：[[S_02]]
- 写入竞争的锁串行化：[[S_05]]
- yaml → InstanceConfig 装载：[[S_06]]
- 早期引导对 yaml 的就地（零依赖）读取：[[S_07]]
