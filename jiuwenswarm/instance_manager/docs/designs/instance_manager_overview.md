# instance_manager 功能概览

> 特性文档：本模块的功能、原理与能力边界（概念层面）。架构图 / 时序图 / 决策表见
> [architecture_cn.md](architecture_cn.md)；改代码前的契约与检查单见 `../specs/`。

## 一句话定位

**多实例隔离工具层**：让一台机器同时跑多个互不干扰的 jiuwenswarm 实例——一台机器，
N 套独立端口、独立数据目录、独立进程身份。

## 解决什么问题

不开多实例时，一切简单：固定端口（18092/19000/19001/5173/8766）、固定数据目录
（~/.jiuwenswarm）。但当用户想同时跑"工作"和"实验"两套环境时，问题全来了：

| 冲突点 | 不做隔离会发生什么 | 本模块的解法 |
|---|---|---|
| 端口 | 第二个实例 bind 失败，直接崩（10048） | 千位段平移分配 + 冲突探测 + 整组回退 |
| 数据目录 | 两个实例读写同一 ~/.jiuwenswarm，会话/配置互相污染 | 每实例独立 workspace + bootstrap .env 钉死 DATA_DIR |
| 并发启动 | 两个进程同时注册/启动同名实例，双属主 | 文件锁 + 原子写注册表 |
| 进程身份 | 不知道哪个 PID 属于哪个实例，stop 误杀 | workspace 内 PID 文件（JSON 含 pid/started_at/name） |
| 历史兼容 | default 用法（不带 --name）行为变化 | default 永占 index 0，不进注册表，行为不变 |

## 核心概念

```
default 实例（index 0）                具名实例（index 1, 2, 3…）
─────────────────────                ─────────────────────────
无 --name 启动，历史用法               --name alice 启动
workspace: ~/.jiuwenswarm/            workspace: ~/.jiuwenswarm-instances/alice/
端口: 基础端口组                        端口: 基础端口 + index×1000
不进 instances.yaml                    注册在 ~/.jiuwenswarm/instances.yaml
状态: PID 文件 + 端口探测兜底           状态: 只认 PID 文件
```

### 关键术语

| 术语 | 含义 |
|---|---|
| **实例（instance）** | 一套独立的 jiuwenswarm 运行环境 = 独立 workspace + 独立端口组 + 独立进程树 |
| **default 实例** | 不带 `--name` 启动的隐式实例，占 index 0 与基础端口，兼容历史行为 |
| **具名实例** | `jiuwenswarm-init --name <name>` 创建、`--name <name>` 启动的显式实例 |
| **index** | 实例的端口分配序号：default 恒 0；具名实例 = instances.yaml 声明顺序 + 1 |
| **instances.yaml** | 具名实例注册表（`~/.jiuwenswarm/instances.yaml`）：名字 → workspace + 端口 |
| **workspace** | 实例的数据根：default 在 `~/.jiuwenswarm/`，具名在 `~/.jiuwenswarm-instances/<name>/` |
| **bootstrap .env** | workspace 内的 `.env`：实例启动时注入 DATA_DIR / INSTANCE / 五个端口变量（override=True） |
| **端口组** | 一个实例的 5 个端口（agent_server / web / gateway / frontend / http_api）整体 |
| **整组回退** | 冲突时向上扫连续 index，5 端口全部可用才整组换——不做单端口回退 |
| **实例锁** | workspace 内 `.instance.lock`：同实例启动互斥（Unix flock / Windows 排他创建 + 30s 过期回收） |
| **PID 文件** | workspace 内 `.instance.pid`：JSON `{pid, started_at, name}`，实例属主凭证 |
| **千位段平移** | 端口分配公式 base + index×1000：每实例五端口整体平移一个千位段，天然不交叠 |

## 功能清单

| 能力 | 入口 | 一句话说明 |
|---|---|---|
| 实例创建 | `jiuwenswarm-init --name X` | 校验名称 → 建 workspace → 注册 yaml → 生成 bootstrap .env |
| 实例启动 | `jiuwenswarm-start --name X` | 装载 .env → 取配置 → 端口检查/回退 → 持锁写 PID → 起服务 |
| 状态查询 | `list_all_instances` / `get_instance_status` | default 优先 + yaml 全量；具名认 PID，default 有端口探测兜底 |
| 实例停止 | `stop_instance_process` | Windows taskkill /T /F；Unix SIGTERM→SIGKILL；停后清 PID 文件 |
| 端口管理 | `calculate_instance_ports` / `find_available_ports` | 自动分配、冲突检查、整组回退 |
| 端口自定义 | `JIUWENSWARM_<TYPE>_PORT` 环境变量 | 覆盖基础端口（Docker 友好）；坏值静默忽略 |
| 并发保护 | `InstanceLock` | 同实例启动串行化；Windows 崩溃残留锁 30s 自动回收 |

## 工作原理（三层防线）

1. **规划层（启动前）**：从 instances.yaml 读声明顺序得 index → 按公式算出端口组 →
   bind+listen 探测（复刻真实服务取端口方式，能识别僵死监听）→ 冲突则整组上扫回退
   并持久化。
2. **隔离层（启动时）**：bootstrap .env 把 `JIUWENSWARM_DATA_DIR`（数据根）与五个端口
   变量以 override=True 注入进程环境——下游所有服务从环境变量取值，天然各归各实例。
3. **治理层（运行中）**：PID 文件记录属主；实例锁防并发启动双属主；注册表原子写防
   半写损坏；状态查询 / 停止围绕 PID 文件闭环。

## 能力边界（不做什么）

- **不启动任何服务**——只提供能力，编排属 start_services / CLI。
- **不管跨机器**——锁、注册表、探测全是本机文件系统语义，无网络分发。
- **不管资源配额**——不限制实例数量（理论上限 65535 端口空间）、不限内存/CPU。
- **不管服务内部配置**——实例内 config.yaml / 模型配置等由 common 层管理，本模块只
  钉住"数据根 + 端口"这两个隔离坐标。
- **不追踪子进程树**——PID 文件只记主进程；进程树清理由 taskkill /T / SIGTERM 信号
  组语义承担。

## 核心优势

1. **向后兼容零成本**：不带 `--name` 的所有历史用法原样工作（default 隐式存在）。
2. **两起真实事故淬炼的探测语义**：bind+listen 不设 SO_REUSEADDR + ::1 双栈补探，
  僵死监听与 Vite IPv6 场景都能正确识别（历史 10048 崩溃与 strictPort 失败的根治）。
3. **并发安全不靠运气**：锁消除丢更新，原子写消除半读，宽容读消除凭证损坏雪崩。
4. **跨平台等价**：锁 / 存活探测 / 停止 / PID 反查四组原语均有 Windows 与 Unix 双
  实现，CLI 行为一致。
5. **Docker 友好**：五个基础端口全部支持环境变量覆盖，容器化部署改 base 即改全套。

## 快速上手（用户视角）

```powershell
# 创建并启动具名实例
jiuwenswarm-init --name alice
jiuwenswarm-start --name alice

# 查看全部实例（default 永远第一行）
jiuwenswarm status

# 停止
jiuwenswarm stop --name alice
```

术语速查见上表；机制细节与设计理由见 [architecture_cn.md](architecture_cn.md)。
