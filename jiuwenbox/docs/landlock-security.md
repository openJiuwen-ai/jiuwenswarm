# Linux Landlock 安全与兼容模式

Linux 默认使用 `landlock.compatibility: hard_requirement`，模型默认值、default / enterprise /
java / code-agent 随包策略保持一致。要求内核支持并启用 Landlock ABI >= 3，使当前实现中的读取、执行、
写入、创建、删除、跨目录重命名/硬链接（REFER）和截断（TRUNCATE）限制都能生效。
`no_new_privs`、规则创建、规则添加或 `restrict_self` 任一步骤失败，都不会执行 daemon 或用户命令。

## 配置及迁移

| 部署模式 | 行为 |
| --- | --- |
| `hard_requirement`（默认） | 内核不支持、ABI < 3 或任一初始化步骤失败，沙箱创建失败。 |
| `best_effort`（部署端显式选择） | ABI >= 3 正常保护；ABI 1/2 应用可用的权限但报告部分保护；内核不支持时报告完全未启用。规则安装错误仍拒绝启动。 |
| `disabled`（部署端显式选择） | 明确关闭 Landlock，API 和服务日志提示未启用。其他挂载/权限/Seccomp 边界仍有效。 |

需要兼容旧内核时，由运维在 `JIUWENBOX_POLICY_PATH` 指向的服务端策略中明确配置：

```yaml
landlock:
  compatibility: best_effort
```

沙箱请求不指定模式时继承部署选择；`append` 和 `override` 请求都不能降低服务端要求。
例如服务端为 `hard_requirement` 时，请求 `best_effort` / `disabled` 返回策略校验错误；服务端为
`best_effort` 时，请求不能进一步关闭保护，但可以请求更严格的 `hard_requirement`。
配置损坏回退模型默认值时，默认仍为强制保护。

Windows 小艺 Work 继续使用 Windows runner/ACL，不加载 Landlock，不强制上述 Linux 策略下限。
Windows 的 `landlock_status` 为 null，不产生 Landlock 降级警告。仅推理代理部署不创建沙箱，因此不要求
Landlock。支持 ABI >= 3 的 Linux 上正常 Python、Node、Java、Git 和编译工作负载无需增加降级配置。
旧内核上的启动拒绝是明确的安全变化，需升级内核或由部署方选择兼容模式。

## 精确路径规则

单文件规则直接对该文件执行 `open(O_PATH)`，用 `fstat` 区分文件和目录。文件只授予
EXECUTE / READ_FILE / WRITE_FILE / TRUNCATE 中适用的权限，不再把 `/etc/config` 扩大为 `/etc`。
声明一个可写文件不允许创建、删除或修改旁边的文件；需要目录级创建/重命名时，应明确授予所在目录。
其他显式祖先目录授权仍按策略生效，因此不要同时给予敏感文件父目录的广泛权限。

发行版可选路径（例如不存在的 `/lib64`、`/etc/pki`）可以缺省，不会改为授权其父目录。
打开已存在路径时遇到权限错误、添加规则失败等实际错误会终止启动。
`bind_root_entries` 的实际展开沿用 bubblewrap 对隐藏项、exclude 和非普通对象的过滤，规则只授权实际挂载项。

策略 `read_only: ["/"]` 同时用于 bubblewrap 的根目录只读挂载设置。Landlock 将该项解释为
根目录及目录树的列举权限，不再隐式允许读取全部文件；其他文件读取权限由具体目录/文件和挂载项提供。
随包默认的 `/usr`、`/lib`、`/etc` 等原有具体配置保持不变。

若显式配置根目录 bind 或 `read_write: ["/"]`，Landlock 对当前可见的根目录子项逐一授予权限，
跳过 `/jiuwenbox` 运行目录及指向它的别名，不直接授予根目录递归读写权限。这仍是宽泛的配置，
读取范围由实际挂载和其他权限共同决定；它不是宿主机敏感文件白名单。
根目录自身只可列举，创建新的顶层项应通过声明的 `directories` / `files` 或具体目录授权完成。

## 可见状态和启动验证

launcher 应用完规则后，在内存中将结果交给 daemon。box-server 通过已有控制令牌认证的 IPC 读取结果，
不接受请求或用户命令环境中的状态声明。未收到有效结果、结果与配置不一致，或强制模式未完整应用时，
启动失败并停止、清理该沙箱，不会标记 ready。服务端状态核验最多等待 30 秒后失败。

创建、查询和列表 API 的 `SandboxRef` 新增可选字段，兼容原有客户端：

```json
{
  "landlock_status": {
    "mode": "best_effort",
    "state": "degraded",
    "abi": 0,
    "reason": "Landlock is unavailable; filesystem rules are not enforced"
  },
  "security_warnings": ["Landlock: Landlock is unavailable; filesystem rules are not enforced"]
}
```

`state` 为 `enforced`、`degraded` 或 `disabled`；降级/关闭都会同时写入 box-server 警告日志。
状态持久化在本次运行的沙箱信息中，停止后代表最后一次启动结果，重新启动时重新核验。
`/health` 的 `landlock_supported` 仅表示宿主内核能力，不代表某个沙箱已成功应用规则。
保留已有 API 语义：运行时启动失败返回 `phase: error` 和 `error_message`，客户端应检查 phase，
不能仅凭创建接口返回 HTTP 201 判断沙箱 ready。

## 验收

在已安装 JiuwenBox 源码包的环境执行：

```bash
python -m pytest tests/unit/test_landlock_security.py tests/unit/test_linux_ipc_auth.py -q
# 在 Linux ABI >= 3 环境验证真实 Landlock + Seccomp；安装所需工具链以免跳过。
python -m pytest tests/integration/test_landlock_native.py -q
# 部署默认策略、完成 API Token 配置后验证完整 bwrap/daemon 启动链。
python -m pytest tests/integration/test_server_api_default.py -q
```

便携回归覆盖各失败阶段、单文件 FD 权限、根目录保留区、部署下限、令牌认证状态查询及创建/重启状态。
原生测试只在独立子进程中安装内核规则，验证相邻文件不可读写、缺失文件不扩大权限、继承、目录操作、
Python 线程和子进程，以及 Node、Java、Git 和编译器与 Seccomp 同时启用的兼容性。
Windows 会跳过原生 Linux 用例；模拟和子进程测试不替代实际部署验收。

Landlock 不会增加已有权限，也不能替代挂载隔离、DAC、Capabilities 和 Seccomp；不能仅凭其失效
断言能够读取宿主机完整文件系统。升级后需正常停止并重建已有沙箱，新的规则才会应用。

参考：[内核 Landlock 文档](https://docs.kernel.org/6.16/userspace-api/landlock.html)、
[精确文件规则及结构布局](https://man7.org/linux/man-pages/man2/landlock_add_rule.2.html)、
[文件访问权与 ABI](https://man7.org/linux/man-pages/man7/landlock.7.html)。
