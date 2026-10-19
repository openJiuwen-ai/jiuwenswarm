# 实例锁与 PID 管理

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_05 / lock-and-pid |
| 关联模块 | jiuwenswarm/instance_manager/lock.py |
| 最近一次修订日期 | 2026-10-14 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管**同实例启动的互斥**（InstanceLock 跨平台文件锁）与**实例进程的身份凭证**
（PID 文件的写 / 读 / 删 / 存活探测）。状态如何消费 PID 文件判定 running 归 [[S_06]]；
锁与 PID 文件的路径由 `InstanceConfig` 派生（[[S_02]]）。

## 不变量

- **N1 锁先于 PID 操作**：对同一实例的"写 PID / 读改写 instances.yaml"必须在持有
  `InstanceLock` 的临界区内完成。锁是并发启动的串行化器；绕过锁直接写 PID 文件会
  让两个进程都认为自己是实例属主。
- **N2 双平台锁实现、语义一致**：Unix = `fcntl.flock(LOCK_EX | LOCK_NB)` 非阻塞抢锁
  + 0.1s 间隔轮询直到超时；Windows = `open(..., 'x')` 排他创建锁文件 + **mtime 过期
  检测**（age > `STALE_LOCK_TIMEOUT`=30.0s 判 stale → 删除 → 重试）。两者对外统一为
  `acquire(timeout=5.0) -> bool`；拿不到锁返回 False，**不抛异常**。
- **N3 Windows 锁必须删除文件**：release 时 Windows 路径要 close **并 unlink** 锁文件
  ——锁的存在性即互斥语义；只 close 不删 = 永久死锁。Unix 的 flock 随 fd 关闭自动
  释放，文件残留无害。
- **N4 锁文件含调试信息**：两种平台抢到锁后都写入 `<pid>\n<timestamp>\n` 两行——
  排查"谁在锁着"时直接 cat。
- **N5 PID 文件三字段 JSON**：`{"pid": int, "started_at": float, "name": str}`，落盘
  在 `workspace/.instance.pid`。写入原子（temp `.tmp` + rename；Windows rename 前先
  unlink 旧文件）。读取**宽容**：文件不存在 / JSON 损坏 / 非 dict → 一律返回 None，
  不抛错——PID 文件是凭证不是账本，损坏等价于"无凭证"。
- **N6 存活探测双实现**：`is_process_alive(pid)`——Windows 用 `tasklist /FI "PID eq
  <pid>" /NH`（5s 超时；输出含 PID 且不含 "INFO:" 行才算活），Unix 用
  `os.kill(pid, 0)`（信号 0 仅探测不发送）。`pid <= 0` 直接 False。任何异常吞掉
  返回 False（fail-closed：探测失败按不存活处理，状态层据此清理）。
- **N7 过期阈值是 30 秒**：`STALE_LOCK_TIMEOUT = 30.0`。这是 Windows 锁的**孤儿锁
  回收窗口**——持有者崩溃后最多 30s 内新启动者可抢回。调小会增加误删活锁的风险
  （持有者只是慢），调大延长崩溃后实例不可重启的时间。
- **N8 legacy 接口冻结**：`check_instance_running(workspace)` 直接读 workspace 下 PID
  文件（不经 InstanceConfig），仅存量调用方使用；新代码一律走 [[S_06]]
  `get_instance_status`。

## 接口契约

```python
# lock.py
LOCK_FILENAME: str = ".instance.lock"
STALE_LOCK_TIMEOUT: float = 30.0

class InstanceLock:
    def __init__(self, config: InstanceConfig)   # lock_path = workspace / LOCK_FILENAME
    def acquire(self, timeout: float = 5.0) -> bool   # N2；False = 超时未获得
    def release(self) -> None                        # N3；幂等（未持有时静默）
    def __enter__(self) -> "InstanceLock"            # acquire() 默认超时；不检查返回值！
    def __exit__(self, ...) -> None                  # release()

def write_pid_file(config: InstanceConfig, pid: int, started_at: Optional[float] = None) -> None
    # started_at 缺省 time.time()；原子写（N5）
def read_pid_file(config: InstanceConfig) -> Optional[Dict[str, Any]]
    # 宽容读（N5）：缺/坏/非 dict → None
def delete_pid_file(config: InstanceConfig) -> bool   # True=删了，False=本就不存在

def is_process_alive(pid: int) -> bool                # N6
def check_instance_running(workspace: Path) -> bool   # legacy（N8）
```

**使用模板（start_services 的实际形态）**：

```python
lock = InstanceLock(config)
if not lock.acquire(timeout=5.0):
    # "实例启动进行中"，放弃
    return
try:
    write_pid_file(config, os.getpid())
    # ... yaml 读改写 / 服务启动
finally:
    lock.release()
```

注意 `with InstanceLock(config):` 形态不检查 acquire 返回值——拿不到锁也进入临界区，
只适合"锁不住也无害"的场景；互斥语义必须显式 `if not lock.acquire(...)`。

## 数据结构 / 生命周期

| 文件 | 位置 | 谁写 | 谁读 | 何时清理 |
|---|---|---|---|---|
| `.instance.lock` | workspace/ | InstanceLock（抢到即写 pid+ts） | 人（排查） | release（Windows unlink / Unix 随 fd）；Windows 崩溃残留 → 30s 后 stale 回收（N7） |
| `.instance.pid` | workspace/ | `write_pid_file`（启动期，持锁） | `read_pid_file`（状态查询 / 停止，[[S_06]]） | `stop_instance_process` 停止成功后；PID 无效时停止路径也会清理（[[S_06]]） |

PID 文件在实例停止后由停止方删除（不是状态查询方——查询只读，见 [[S_06]] N4）。

## 执行路径

P1（启动互斥）：start_services → `InstanceLock.acquire` → 临界区内
`update_instances_yaml`（[[S_04]]）+ `write_pid_file` → finally `release`。

P2（崩溃恢复）：属主崩溃 → 锁文件（Windows）残留 → 下次启动 `_is_stale_lock`
（mtime age > 30s）→ `_remove_stale_lock` → 重试 acquire。Unix 无此步（flock 随进程
消失）。

P3（状态查询）：`read_pid_file` → `is_process_alive`（详见 [[S_06]]）。

## 边界 case 清单

| 场景 | 现行为 | 测试覆盖 |
|---|---|---|
| 两进程并发启动同实例 | 后者 acquire 超时 False（默认 5s），打印"启动进行中"退出 | TestInstanceLock |
| Windows 持锁者崩溃 | 30s 内新启动等待；超龄锁被删、重试抢锁 | TestInstanceLock |
| Unix 持锁者崩溃 | flock 自动释放，立即可抢 | TestInstanceLock |
| PID 文件半写 / 手改成垃圾 | read → None，按未运行处理（N5） | TestPidFileManagement |
| PID 复用（旧 PID 归了别的进程） | is_process_alive 误报活——已知局限，PID 文件含 started_at/name 供人工核对 | — |
| tasklist / lsof 不存在或超时 | 异常吞掉 → False（N6 fail-closed） | TestIsProcessAlive |
| release 被调两次 | 第二次静默（`_lock_file is None` 短路） | TestInstanceLock |
| `with InstanceLock(...)` 拿不到锁 | **仍进入 with 体**（__enter__ 忽略返回值） | — |

## 修改检查单

- 改 `STALE_LOCK_TIMEOUT` → 评估 N7 两端风险 + 本 spec 同步。
- 改锁文件名 / PID 文件名 → [[S_02]] 的 `get_pid_file_path` 与 lock.py 内
  `workspace / LOCK_FILENAME` 两处派生点 + 存量实例 workspace 内旧文件兼容。
- 改 `is_process_alive` → [[S_06]] 的 default 端口探测（依赖"占用 ⇒ 有进程"的间接
  判定）与停止路径都消费它；Windows 分支的 "INFO:" 判别式是 tasklist 无匹配时的
  输出特征，改动前确认新命令的空输出形状。
- 新增跨平台原语 → 过 [[S_01]] N6（平台分支成对登记）。
- 改 PID 文件 JSON 形状 → read 的宽容语义（N5）不得收紧；`get_instance_status` /
  `get_default_instance_status` / `stop_instance_process` 三个解析点同步。

## 与其它 spec 的关系

- 锁所保护的 yaml 读改写：[[S_04]] N3
- PID 文件的状态消费与停止时清理：[[S_06]]
- 文件路径派生（get_pid_file_path）：[[S_02]]
