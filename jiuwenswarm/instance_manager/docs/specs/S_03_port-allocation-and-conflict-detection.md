# 端口分配与冲突探测

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_03 / port-allocation-and-conflict-detection |
| 关联模块 | jiuwenswarm/instance_manager/config.py |
| 最近一次修订日期 | 2026-10-14 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管**端口号的计算（分配公式、环境变量覆盖）、可用性探测（bind+listen 语义）、
冲突检查与整组回退扫描、端口向 .env 的持久化**。instances.yaml 里 ports 字段的结构校验
归 [[S_04]]；bootstrap .env 的整体生成归 [[S_07]]；谁在启动时调用本组函数（编排时序）
归 start_services（仓内 `jiuwenswarm/start_services.py`），不在本包。

## 不变量

- **N1 分配公式唯一**：实例端口 = `_resolved_base_port(port_type) + index * 1000`。
  基础端口表 `BASE_PORTS` 恰好 5 项：agent_server=18092 / web=19000 / gateway=19001 /
  frontend=5173 / http_api=8766；`PORT_TYPES` 是同 5 键的元组。改公式 / 改表 = 全量
  实例端口漂移，必须走 spec 修订。
- **N2 index 0 保留给 default**：具名实例 index 从 1 起（声明顺序，见 [[S_04]]）。
  `index * 1000` 的步长保证同 index 的 5 个端口整体平移一个千位段，不同实例不交叠。
- **N3 环境变量覆盖只改 base**：`PORT_ENV_OVERRIDES`（`JIUWENSWARM_<TYPE>_PORT` 五个
  变量）覆盖的是 `_resolved_base_port` 的**起点**，不改变步长公式；未设置时行为与历史
  固定默认完全一致（向后兼容）。**坏值（非 int）静默忽略**，只记 debug 日志——一个
  手滑的 typo 不允许弄坏启动。
- **N4 探测必须 bind+listen 且不设 SO_REUSEADDR**：`is_port_available` 通过真实
  `bind()`+`listen(1)` 后立即关闭来探测。**刻意不设 SO_REUSEADDR**：Windows 上它会
  允许多个 socket 绑同一端口，掩盖占用并复现 10048 崩溃；真实服务（AgentServer /
  Gateway / Web / Frontend）也不设。禁止把探测改成 connect() 探测——connect 探测会把
  "活着但不 accept 的僵死监听"误判为可用，历史上正是它导致回退逻辑选中被死 socket
  占住的端口、真服务自己 bind 时崩 10048。
- **N5 IPv4 回环必须补探 ::1**：host 为 `127.0.0.1` / `0.0.0.0` / `""` / `localhost`
  时，IPv4 探测通过后还要探 `[::1]`——Vite / Node 在 Windows 常只听 `[::1]`，IPv4-only
  探测会误报可用，进而跳过整组回退，最后 `strictPort: true` 报 "Port 5173 is already
  in use"。
- **N6 地址族不可用 ≠ 端口占用**：bind 抛 EADDRNOTAVAIL / EAFNOSUPPORT /
  EPROTONOSUPPORT（POSIX）或 WinError 10022 / 10049（Windows，IPv6 被禁）视为
  "该地址族在本机不可用"（探测返回 None），调用方忽略；显式 IPv6 host 且栈不可用时
  回退 IPv4 视图判定。
- **N7 整组回退语义**：`find_available_ports` 从 `base_index` 起向上扫 `scan_range` 个
  连续 index，**每组 5 个端口全部可用且不在 exclude 集才算整组可用**，返回首个可用组
  `(ports_dict, actual_index)`；`scan_range=0` 立即返回 None（不再静默 clamp 成 1——
  否则 start_services 的"扫描区间耗尽"错误消息描述的区间与实际不符）。
- **N8 端口全量下发**：`PORT_ENV_NAMES` 五键（agent_server→AGENT_SERVER_PORT、
  web→WEB_PORT、gateway→GATEWAY_PORT、frontend→FRONTEND_PORT、
  http_api→AGENT_HTTP_PORT）。http_api 参与分配与冲突扫描，**必须一并下发**——否则
  实例预留了端口却收不到，开启 HTTP 时回落默认 8766 造成隐性冲突；是否真正监听由
  config.yaml `http_server.enabled` 决定，与本端口的下发无关。
- **N9 collect_all_ports fail-open**：读 instances.yaml 失败只记 debug 日志并按已收集
  部分返回（default 端口 + 已成功读取的条目）——冲突收集是尽力而为，不得因 yaml 损坏
  阻断启动路径。

## 接口契约

```python
# config.py
BASE_PORTS: Dict[str, int]          # 5 项（N1）
PORT_TYPES: tuple                    # 5 键（N1）
PORT_ENV_OVERRIDES: Dict[str, str]   # port_type → JIUWENSWARM_<TYPE>_PORT（N3）
PORT_ENV_NAMES: Dict[str, str]       # port_type → .env 变量名（N8）

def compute_auto_port(port_type: str, index: int) -> int
def calculate_instance_ports(index: int) -> Dict[str, int]   # 全 5 键整组
def is_port_available(host: str, port: int) -> bool          # bind+listen（N4-N6）
def check_port_conflicts(
    ports: Dict[str, int],
    host: str = "127.0.0.1",
    existing_ports: Optional[Sequence[int]] = None,
) -> List[int]                                              # 返回冲突端口列表（空=无冲突）
def collect_all_ports(exclude_name: Optional[str] = None) -> List[int]
def find_available_ports(
    base_index: int = 0,
    host: str = "127.0.0.1",
    scan_range: int = 10,
    exclude_ports: Optional[Sequence[int]] = None,
) -> Optional[tuple]     # (ports_dict, actual_index) 或 None（N7）

# 包内私有、跨模块豁免（[[S_01]] N4）
def _upsert_env_ports(env_path: Path, ports: Dict[str, int]) -> None
def _format_url_hint(ports: Dict[str, int]) -> str
def _get_system_executable(name: str) -> str
```

`_resolved_base_port`：env 覆盖优先（N3），未知 port_type 兜底 10000。

## 数据结构 / 持久化

| 落点 | 内容 | 谁写 | 生命周期 |
|---|---|---|---|
| `~/.jiuwenswarm/config/.env` | 五个 PORT_ENV_NAMES 变量行 | `_upsert_env_ports`（start_services 对 **default 实例** 调用） | 幂等 upsert：已有 `KEY=...` 行原位替换，否则追加；**其它行（API key、模型配置）一律原样保留**；文件不存在则创建 |
| instances.yaml `ports:` | 具名实例端口表 | `update_instances_yaml`（[[S_04]]） | 持久 |
| bootstrap `<workspace>/.env` | 同五变量 + DATA_DIR / INSTANCE | `create_bootstrap_env`（[[S_07]]） | 持久，实例停止不清理 |

`_format_url_hint`：端口回退后打印的 TUI/CLI 连接提示（`jiuwenswarm-tui --url
ws://127.0.0.1:<gateway>/tui` + `jiuwenswarm chat`），显式 URL 形式保证 TUI 读不到
持久化 GATEWAY_PORT 时也能连。

## 执行路径（启动期端口决策序）

1. init：`update_instances_yaml` 按 index 自动分配（[[S_04]]）或用户显式声明 ports。
2. start：start_services `get_instance_config` 取端口 → `collect_all_ports(exclude_name)`
   收集他实例端口 → `is_port_available` / `check_port_conflicts` 检查本组。
3. 冲突：`find_available_ports(base_index=本实例 index, exclude_ports=他实例端口)`
   整组上扫回退 → 回退成功后 default 走 `_upsert_env_ports` 持久化、具名走
   `create_bootstrap_env`（[[S_07]]）→ `_format_url_hint` 提示。

## 边界 case 清单

| 场景 | 现行为 | 测试覆盖 |
|---|---|---|
| 端口被活服务监听 | bind 失败 → 占用 | TestPortAvailability |
| 端口被僵死 LISTEN socket 占（不 accept） | bind 仍失败 → 占用（N4 的核心动机） | TestPortAvailability（注释载明历史事故） |
| Vite 只听 `[::1]:5173` | IPv4 探测通过后补探 ::1 失败 → 占用 | TestPortAvailability |
| IPv6 整机禁用 | 探测返回 None（族不可用），不误判占用 | TestPortAvailability |
| `JIUWENSWARM_GATEWAY_PORT=abc` | 忽略 + debug 日志，用默认 19001 | TestEnvVarOverride |
| `scan_range=0` | 立即 None，不扫任何 index（N7） | TestPortFallback |
| instances.yaml 损坏时收集端口 | fail-open（N9） | TestCollectAllPorts |
| .env 已有 `GATEWAY_PORT=29001` 行 | 原位替换，不产生重复行 | TestUpsertEnvPorts |

## 已知遗留

- `find_available_ports` docstring 写 "full 4-port group"——http_api 加入后实际是 **5 端口
  整组**（代码遍历 `calculate_instance_ports` 全部值，行为正确），docstring 措辞滞后。
- 早期引导路径 `_create_basic_bootstrap_env`（[[S_07]]）使用**静态 BASE_PORTS**，不感知
  N3 的环境变量覆盖——Docker base-port 覆盖下，早期兜底生成的 .env 与正式路径可能短暂
  不一致，随后被 `create_bootstrap_env` 重写。

## 修改检查单

- 改 `BASE_PORTS` / 步长 1000 / `PORT_TYPES` → 同步：本 spec N1、[[S_04]] 模板注释、
  `TestPortAllocation`、start_services 的提示文案；评估已存量实例的端口漂移。
- 改探测方式 → 禁止脱离 bind+listen 语义（N4/N5 是两起线上事故的结晶）；必须补
  "僵死监听"与"::1-only"两个回归场景。
- 新增端口类型 → BASE_PORTS + PORT_TYPES + PORT_ENV_OVERRIDES + PORT_ENV_NAMES 四表
  同步（漏 PORT_ENV_NAMES 即违反 N8），并检查 [[S_07]] bootstrap 两处映射。
- 改 `_upsert_env_ports` → 保证幂等与非端口行保真（TestUpsertEnvPorts）。
- 改 `find_available_ports` 返回形状 → start_services 解包点同步。

## 与其它 spec 的关系

- index 的来源（yaml 声明顺序）：[[S_04]]
- 端口写进 bootstrap .env 的整体格式：[[S_07]]
- default 实例按端口占用反推 running：[[S_06]]
