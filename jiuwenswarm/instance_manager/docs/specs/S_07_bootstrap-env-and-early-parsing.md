# bootstrap .env 与早期引导

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_07 / bootstrap-env-and-early-parsing |
| 关联模块 | jiuwenswarm/instance_manager/bootstrap.py |
| 最近一次修订日期 | 2026-10-14 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管**实例 bootstrap .env 的三种生成/装载路径**：常规生成（create_bootstrap_env）、
早期兜底生成（_create_basic_bootstrap_env，零依赖）、argparse 后按名装载
（load_instance_bootstrap_by_name）。端口数值语义归 [[S_03]]；实例存在性判定归
[[S_06]]；早期 argparse / dotenv 解析框架本身（dotenv_early.py）在模块外，但
**它对本包的调用契约由本 spec 承载**。

## 不变量

- **N1 bootstrap .env 的固定形状**：`workspace/.env`，行序固定——首行注释
  `# Bootstrap .env for instance: <name>`，随后
  `JIUWENSWARM_DATA_DIR=<workspace>` / `JIUWENSWARM_INSTANCE=<name>`，再按
  agent_server / web / gateway / frontend / http_api 顺序追加存在的端口变量
  （AGENT_SERVER_PORT / WEB_PORT / GATEWAY_PORT / FRONTEND_PORT / AGENT_HTTP_PORT，
  映射表见 [[S_03]] N8）。
- **N2 两处映射表必须逐键同步**：`config.PORT_ENV_NAMES` 与 bootstrap.py 内的局部
  `port_env_mapping` 是**有意的两份拷贝**（bootstrap 常规路径本可 import，历史形态
  保留）。改任一处的键 / 变量名，另一处必须同步——不同步的直接后果是某端口类
  "分配了却没下发"（[[S_03]] N8 已点名 http_api 的历史教训）。config.py 源码注释
  "Must match jiuwenswarm/instance_manager/bootstrap.py" 即指此约束。
- **N3 早期路径零 jiuwenswarm 依赖**：`_create_basic_bootstrap_env` 在
  dotenv_early.py 的早期解析阶段被调用——**任何 jiuwenswarm 模块导入之前**。因此它
  不得 import 本包任何子模块（config / yaml / status 全禁止），所需能力全部就地
  实现：home 解析（`JIUWENSWARM_HOME` env 或 `Path.home()`）、instances.yaml 直读
  （`yaml.safe_load`，异常兜底 index=1）、端口计算（**静态 BASE_PORTS + index×1000**，
  不走 [[S_03]] N3 的 env 覆盖）。日志用自带 early logger（`jiuwenswarm.early`，
  WARNING 级别直接挂 stderr）。
- **N4 早期路径只写四变量**：早期 bootstrap 只下发 AGENT_SERVER_PORT / WEB_PORT /
  GATEWAY_PORT / FRONTEND_PORT（无 AGENT_HTTP_PORT）——它是"实例建了但 .env 丢了"
  的**兜底**，后续正式路径 `create_bootstrap_env` / `load_instance_bootstrap_by_name`
  会补全五变量并覆盖重写。新增端口类型不得只扩早期路径。
- **N5 按名装载的失败契约**：`load_instance_bootstrap_by_name` 三种失败（名称非法 /
  不在 instances.yaml / workspace 不存在）一律 **logger.error + 返回 None**，错误消息
  带 `jiuwenswarm-init --name <name>` 修复指引；**调用方（CLI main）必须以
  SystemExit(1) 响应 None**——这是本包与 CLI 之间的硬契约，不能静默继续（缺
  DATA_DIR / 端口变量的进程会跑在错误的实例上）。
- **N6 装载用 override=True**：`load_dotenv(env_path, override=True)`——bootstrap .env
  是实例身份的**权威源**，必须压过继承自父 shell 的同名变量（否则从 default 环境
  shell 里启动具名实例会串端口）。已在环境里的变量被 .env 覆盖是**预期行为**。
- **N7 幂等创建**：`.env` 不存在才生成（`load_instance_bootstrap_by_name` 内判断）；
  `create_bootstrap_env` 本身是整文件重写，对已存在的 .env 直接覆盖（bootstrap 段
  不保存用户手改内容——用户级配置应在 config.yaml / 用户 .env，不在此文件）。

## 接口契约

```python
# bootstrap.py
def create_bootstrap_env(config: InstanceConfig) -> Path
    # 常规生成：五变量全量（N1）；返回 .env 路径
def create_bootstrap_env_for_name(name: str, workspace: Path) -> Path
    # legacy：get_instance_index → calculate_instance_ports → create_bootstrap_env
def _create_basic_bootstrap_env(name: str, workspace: Path, component_name: str) -> None
    # 早期兜底：零依赖（N3）、四变量（N4）；早期 logger 输出带 component_name 前缀
def load_instance_bootstrap_by_name(name: str) -> Path | None
    # argparse 后按名装载：校验 → 取配置 → 验 workspace → 缺 .env 则建 →
    # load_dotenv(override=True)（N6）；失败 None（N5）
```

CLI 接入模板（dotenv_early.py docstring 载明的契约）：

```python
if args.name:
    env_path = load_instance_bootstrap_by_name(args.name)
    if env_path is None:
        raise SystemExit(1)   # N5：不得静默继续
```

## 执行路径

P1（init 全量）：`jiuwenswarm-init --name X` → yaml 注册（[[S_04]]）→
`create_bootstrap_env`（五变量，权威版）。

P2（早期兜底）：CLI 组件早期解析 `--name` + `--dotenv` → 发现 .env 缺失 →
dotenv_early.py 调 `_create_basic_bootstrap_env`（四变量，N3/N4）→ 进程继续启动，
后续如有正式路径再覆盖为五变量版。

P3（argparse 后补装载）：CLI main 解析出 `--name` 但早期未装载 →
`load_instance_bootstrap_by_name` → 失败 SystemExit(1)（N5）。

## 数据结构

bootstrap .env（P1 权威版全量示例，index=1 的实例）：

```ini
# Bootstrap .env for instance: alice
JIUWENSWARM_DATA_DIR=/home/u/.jiuwenswarm-instances/alice
JIUWENSWARM_INSTANCE=alice
AGENT_SERVER_PORT=19092
WEB_PORT=20000
GATEWAY_PORT=20001
FRONTEND_PORT=6173
AGENT_HTTP_PORT=9766
```

消费方：`load_dotenv` 之后，`JIUWENSWARM_DATA_DIR` 由 common 的路径中枢
（utils.get_* 族）读取决定实例数据根；端口变量由各服务启动时读取。

## 边界 case 清单

| 场景 | 现行为 | 备注 |
|---|---|---|
| 早期解析时 instances.yaml 损坏 / 缺失 | index 兜底 1，按静态 base+1000 生成（N3） | 与正式分配可能短暂不一致，正式路径覆盖修正 |
| Docker 下设了 JIUWENSWARM_GATEWAY_PORT | 早期兜底**不感知**（静态 BASE_PORTS）；正式路径感知 | [[S_03]] 遗留已点名 |
| shell 里预置 GATEWAY_PORT=19001 再启动 alice | .env 的 20001 覆盖之（N6 override=True） | 预期行为 |
| `--name` 给了保留名 / 未知名 / workspace 被删 | error + None → SystemExit(1)（N5） | 消息含 init 指引 |
| .env 已存在且用户手改过 | create_bootstrap_env 整文件覆盖（N7） | 手改内容不保留 |
| 环境无 JIUWENSWARM_HOME | Path.home() 兜底（N3） | |

## 修改检查单

- 改 bootstrap .env 行序 / 变量集 → N1 + [[S_03]] N8（PORT_ENV_NAMES）+
  bootstrap.py 局部映射（N2）+ dotenv_early / app 层的读取点；评估存量 .env 兼容。
- 改 `_create_basic_bootstrap_env` → 重新过 N3（零依赖清单：json/logging/os/pathlib/
  yaml + sys 允许，其余全禁）；补四变量与五变量的差异说明（N4）。
- 改 `load_instance_bootstrap_by_name` 失败语义 → N5 是 CLI 硬契约：grep 调用方
  （dotenv_early 的 deprecated 包装 + CLI main）确认 None 分支全部 SystemExit。
- 新增端口类型 → 早期路径**可以**继续缺省（N4），但正式路径必须全量（N1）。

## 与其它 spec 的关系

- 端口变量映射与全量下发约束：[[S_03]] N8
- index 来源（yaml 声明顺序）：[[S_04]] N4
- 按名装载依赖的配置查询：[[S_06]] get_instance_config
- 早期零依赖的包级登记：[[S_01]] N2
