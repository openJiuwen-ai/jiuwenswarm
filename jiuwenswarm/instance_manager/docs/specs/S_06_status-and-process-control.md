# 状态查询与进程控制

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_06 / status-and-process-control |
| 关联模块 | jiuwenswarm/instance_manager/status.py |
| 最近一次修订日期 | 2026-10-14 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管**实例状态的判定**（具名实例 PID 判定、default 实例端口探测兜底）、**实例列表与
配置装载**（yaml 条目 → InstanceConfig）、**展示格式化**与**进程停止**。PID 文件与存活
探测的原语语义归 [[S_05]]；端口可用性探测原语归 [[S_03]]；谁调用停止（CLI stop 命令
编排）在 start_services / CLI 层。

## 不变量

- **N1 具名实例：PID 文件是唯一判定源**：`get_instance_status` = `read_pid_file` →
  `is_process_alive`。PID 文件缺失或进程已死 → `running=False` 且 `pid`/`started_at`
  归 None（[[S_02]] N5）。**不做端口兜底探测**——具名实例的端口可能被回退改过
  （[[S_03]] N7），端口占用不证明本实例在跑。
- **N2 default 实例：端口探测兜底**：default 可能以不经 PID 管理的方式启动（历史路径），
  因此先走 PID 文件（若有），不 running 再按 `agent_server` / `gateway` / `frontend`
  三个端口（注意：不含 web / http_api）任一被占 → `running=True` 并
  `_find_pid_by_port` 反查 PID。**端口探测路径 started_at 恒为 None**（netstat/lsof
  拿不到启动时间）。default 的 workspace 恒为 `get_user_workspace_dir()`、端口恒为
  index 0 组（[[S_03]] N2）。
- **N3 配置装载的端口补全规则**：`get_instance_config` / `load_all_instance_configs`
  对每条目逐 PORT_TYPES 键取值——显式声明的用声明值，缺的按 index（声明顺序 +1，
  [[S_04]] N4）自动补（[[S_03]]）。**返回的 ports 恒为五键完备 dict**；实例条目不存在
  返回 None（get_instance_config）。
- **N4 查询只读、停止负责清理**：状态查询函数**绝不写 / 删 PID 文件**；`.instance.pid`
  的清理只发生在停止路径（`stop_instance_process`）——即使进程已死，也由停止方删文件
  （死实例 + 残留 PID 文件由下一次 stop 清理，而不是查询时"顺手"清理，避免查询方与
  启动方的写竞争）。
- **N5 列表 fail-open**：`list_all_instances` 读 yaml 失败只 warning，default 仍列出；
  单个实例状态异常不影响其它条目。
- **N6 停止的降级序列**：Windows = `taskkill /PID <pid> /T /F`（杀整棵进程树）；
  Unix = SIGTERM → sleep 2s → 仍活则 SIGKILL。之后 0.5s 间隔轮询存活至 timeout
  （默认 10s）。**`stop_process_by_pid` 无论结果恒返回 True**（尽力而为语义——调用方
  不依据返回值判定死透，真正的裁决在下一次状态查询 N1）。
- **N7 PID 反查的命令形状**：Windows = `netstat -ano -p tcp`，扫 LISTENING 行、匹配
  `0.0.0.0:<port>` 或 `127.0.0.1:<port>`、取行尾最后一列为 PID；Unix = `lsof -i :<port>
  -t` 取首个 PID。找不到 / 命令失败 → None（不阻塞 running 判定本身）。

## 接口契约

```python
# status.py
def get_instance_status(config: InstanceConfig) -> InstanceStatus        # N1
def get_default_instance_status() -> InstanceStatus                      # N2
def list_all_instances(include_default: bool = True) -> List[InstanceStatus]  # N5
def format_status_line(status: InstanceStatus) -> str
def get_instance_config(name: str) -> Optional[InstanceConfig]           # N3；不存在 → None
def load_all_instance_configs(path: Optional[Path] = None) -> Dict[str, InstanceConfig]
def stop_process_by_pid(pid: int, timeout: float = 10.0) -> bool         # N6 恒 True
def stop_instance_process(config: InstanceConfig, timeout: float = 10.0) -> bool
```

`format_status_line` 输出（CLI 表格行）：
`{name:<12} {state:<8} {pid:<7} {workspace:<40} {ports}`，state ∈ running/stopped、
pid 缺省 `-`、ports 为五类端口按 PORT_TYPES 顺序以 `/` 连接（缺的跳过）。

`stop_instance_process` 前置短路：无 PID 文件 → True（本就停了）；PID 无效或进程已死
→ 删 PID 文件 → True。正常路径杀完也删 PID 文件（N4）。

## 执行路径

P1（jiuwenswarm status 列表）：`list_all_instances` → default（N2）+ yaml 逐条
（`load_all_instance_configs` → `get_instance_status`）→ `format_status_line` 渲染。

P2（jiuwenswarm stop）：`get_instance_config(name)` → `stop_instance_process` →
（编排层可能追加 `stop_process_by_pid` 清理残余子进程）。

P3（default 运行判定细节）：PID 文件命中且活 → done；否则逐端口
`is_port_available("127.0.0.1", port)`——**注意此处"不可用（被占）"才意味着在跑**，
与 [[S_03]] 的"可用才可分配"语义相反，是同一条原语的两个读法。

## 边界 case 清单

| 场景 | 现行为 | 测试覆盖 |
|---|---|---|
| 具名实例 PID 文件在、进程死 | running=False，pid/started_at=None（N1） | TestInstanceStatus |
| 具名实例端口被占但无 PID 文件 | running=False（不做端口兜底，N1） | TestInstanceStatus |
| default 三个探测端口全空闲 | running=False | TestListAllInstances |
| default gateway 端口被无关进程占 | running=True + 反查到的无关 PID（已知误报面，换来兼容无 PID 管理的历史启动） | — |
| default 端口探测路径 | started_at=None（N2） | TestInstanceStatus |
| yaml 损坏时列表 | warning + 仅 default（N5） | TestListAllInstances |
| 条目只声明部分端口 | 缺的按 index 自动补全，五键完备（N3） | TestGetInstanceConfig |
| 条目名不在 yaml | get_instance_config → None | TestGetInstanceConfig |
| stop 时进程已死 | 删 PID 文件，返回 True | TestPidFileManagement |
| Unix SIGTERM 后 2s 仍活 | 升级 SIGKILL（N6） | — |
| 停止超时进程仍活 | 仍返回 True（N6 尽力而为） | — |

## 已知遗留

- default 端口探测只查 `agent_server` / `gateway` / `frontend` 三类——web / http_api
  不参与，避免只起了部分组件时的误报；若 default 启动形态变化（如仅 web 存活）需
  重估探测集。
- `stop_process_by_pid` 恒 True 的返回值语义弱（N6）——调用方若需要"确认死透"必须
  回读 `is_process_alive`。

## 修改检查单

- 改 default 探测端口集 → 本 spec N2 同步 + 评估 [[S_03]] 端口组变化的影响。
- 改 `format_status_line` 列宽 / 顺序 → CLI 展示与文档示例同步（对齐宽度是 CLI 契约）。
- 改停止序列（信号 / 超时）→ N6 + [[S_05]] 的 is_process_alive 消费一致性。
- 改 `get_instance_config` 装载规则 → [[S_04]] N5（写入端）与 N3（读取端）双向对齐。
- 改 `list_all_instances` 顺序（default 优先）→ CLI 输出顺序即用户心智，慎重。

## 与其它 spec 的关系

- PID 文件 / 存活原语：[[S_05]]
- 端口可用性原语与 index 分配：[[S_03]]
- yaml 形状与原子写：[[S_04]]
- 状态渲染进 CLI 的编排：start_services（模块外）
