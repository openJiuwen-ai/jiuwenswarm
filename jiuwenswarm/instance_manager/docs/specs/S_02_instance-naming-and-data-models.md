# 实例命名与核心数据模型

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_02 / instance-naming-and-data-models |
| 关联模块 | jiuwenswarm/instance_manager/config.py |
| 最近一次修订日期 | 2026-10-14 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管**实例名称的合法性规则**与**全包共享的三个数据形状**（InstanceConfig /
InstanceStatus / InstancesYamlError）。端口字段的取值算法归 [[S_03]]；instances.yaml 的
文件结构归 [[S_04]]。

## 不变量

- **N1 名称模式唯一真源**：名称合法性 = `INSTANCE_NAME_PATTERN`
  （`^[a-zA-Z0-9_-]+$`）**且**长度 1–64 **且**不以 `.` 开头 **且** 小写后不在
  `RESERVED_NAMES`。四个条件全部由 `validate_instance_name` 判定，其它任何地方不得
  重新实现名称校验。
- **N2 保留名集合是 5 个**：`default` / `config` / `tmp` / `jiuwenswarm` / `all`
  （frozenset）。比较用 `name.lower()`——大小写不敏感（`Default` 同样拒绝）。扩集合 =
  改 `RESERVED_NAMES` 并同步 yaml 模板注释。
- **N3 `default` 名义内建**：index 0 与基础端口组保留给 default 实例（见 [[S_03]] N2）；
  default 实例**不落 instances.yaml**，其 workspace 是 `get_user_workspace_dir()`
  （~/.jiuwenswarm），状态判定走端口探测路径（见 [[S_06]]）。
- **N4 InstanceConfig 构造即规范化**：`__post_init__` 对 workspace 做
  `expanduser().resolve()`——构造后 `config.workspace` 一定是绝对规范路径；消费方
  不得假设传入的原样路径存活。`ports` 默认空 dict，端口完备性由装配方（[[S_04]]
  / [[S_06]]）保证。
- **N5 InstanceStatus 的 None 语义**：`running=False` 时 `pid` 与 `started_at` 必为
  `None`（不是 0 / 旧值）；`running=True` 时 `pid` 必为正整数。UI / 调用方按此三元组
  判定，不得读 0 当"已停止"。
- **N6 错误消息前缀统一**：`InstancesYamlError` 构造即拼接 `"[instances.yaml] "` 前缀；
  消息体必须带**修复建议**（路径 + 怎么改）。新增错误分支沿用此形状。

## 接口契约

```python
# config.py
INSTANCE_NAME_PATTERN: re.Pattern[str]        # ^[a-zA-Z0-9_-]+$
RESERVED_NAMES: frozenset[str]                # 5 个保留名（N2）
PID_FILENAME: str                             # ".instance.pid"（workspace 内相对名）

def validate_instance_name(name: str) -> Optional[str]
    # 合法 → None；非法 → 一句话英文错误消息（不抛异常）
    # 检查顺序：非空字符串 → 长度 1-64 → 模式 → 无前导点 → 非保留名

def is_valid_instance_name(name: str) -> bool  # validate_instance_name is None

@dataclass
class InstanceConfig:
    name: str
    workspace: Path                            # __post_init__: expanduser+resolve（N4）
    ports: Dict[str, int] = field(default_factory=dict)
    def get_pid_file_path(self) -> Path        # workspace / ".instance.pid"
    def get_bootstrap_env_path(self) -> Path   # workspace / ".env"

@dataclass
class InstanceStatus:
    name: str
    running: bool
    pid: Optional[int]
    workspace: Path
    ports: Dict[str, int]
    started_at: Optional[float] = None         # Unix 时间戳；N5 None 语义

class InstancesYamlError(Exception)            # 消息前缀 "[instances.yaml] "（N6）
```

## 数据结构

| 结构 | 谁写 | 谁读 | 生命周期 |
|---|---|---|---|
| `InstanceConfig` | init_workspace（创建时）、status 装载（[[S_06]]）、start_services（编排时） | 锁 / PID / bootstrap / 状态全链路 | 进程内临时对象，不持久化；持久化投影是 yaml 条目（[[S_04]]） |
| `InstanceStatus` | status.py 查询函数 | CLI 展示（format_status_line）、start_services | 查询瞬时快照 |
| `InstancesYamlError` | yaml.py 校验层（[[S_04]]） | init_workspace / CLI 顶层捕获 | 异常即终结 |

## 边界 case 清单

| 场景 | 现行为 | 备注 |
|---|---|---|
| 名称 `"alice-01"` | 合法 | 模式含连字符 |
| 名称 `"数据"`（非 ASCII） | 拒绝：模式不匹配 | 错误消息提示允许字符集 |
| 名称 `"Default"` | 拒绝：保留名 | 大小写不敏感（N2） |
| 名称 `""` / 非 str | 拒绝："must be a non-empty string" | None 输入同样走到这 |
| 名称 65 字符 | 拒绝：1-64 超限 | |
| 名称 `".hidden"` | 模式先拒（`.` 不在字符集）；前导点检查是防御性冗余 | |
| workspace 传相对路径 `~/x` | 构造后变绝对规范路径 | N4；`~` 展开 |
| `InstanceStatus(running=False, pid=5, ...)` | 数据类本身不拦截 | 生产代码只经 status.py 构造（[[S_06]] 保证 N5）；测试可直接构造 |

## 修改检查单

- 改 `INSTANCE_NAME_PATTERN` / `RESERVED_NAMES` → 同步：yaml 模板注释（[[S_04]]）、
  init_workspace 的用户提示、本 spec N1/N2、`tests/unit_tests/test_instance_manager.py`
  的 `TestInstanceNameValidation`。
- 改 `InstanceConfig` 字段 → grep 全部构造点（status.get_instance_config /
  load_all_instance_configs / get_default_instance_status、bootstrap 两个工厂、
  start_services）+ 过 [[S_01]] N5。
- 改 `InstanceStatus` 字段 → 同步 `format_status_line` 与 start_services 的消费点。
- 改 `PID_FILENAME` → 检查 lock.py 的 `check_instance_running`（直接拼同名字符串的
  legacy 路径）与全部 workspace 扫描点。
- 改 `InstancesYamlError` 前缀 → grep CLI 层是否有按前缀匹配的展示逻辑。

## 与其它 spec 的关系

- 端口字段的计算与冲突语义：[[S_03]]
- instances.yaml 条目 ↔ InstanceConfig 的装载投影：[[S_04]] / [[S_06]]
- PID 文件 / 锁文件路径由本 spec 的两个 `get_*_path` 派生：[[S_05]]
