# Linux Seccomp 安全基线

Linux 沙箱默认且强制安装 Seccomp BPF。规则为**代码内置最低封禁集与当前架构 `syscall.*.blocked` 的并集**。
省略 `syscall`、设置空列表或基底 YAML 读取失败，都不会清空最低基线。`blocked` 配置只用于追加封禁；
策略 API 展示的列表是配置值，代码基线始终额外生效。

## 基线与兼容性

最低封禁集沿用既有 `default-policy.yaml`，不额外限制常规 Python、Node、Java、Git 和编译器的调用：

```text
kexec_load kexec_file_load reboot pivot_root swapon swapoff nfsservctl lookup_dcookie
ptrace mount umount2 unshare setns userfaultfd perf_event_open bpf
add_key request_key keyctl io_uring_setup io_uring_enter io_uring_register
open_by_handle_at name_to_handle_at init_module finit_module delete_module acct
```

命中的调用返回 `EPERM`。其余原生调用继续放行；保留 `clone` / `clone3`、`futex`、文件 I/O、
网络、内存映射及 JIT 所需操作。若配置额外封禁 `clone3`，返回 `ENOSYS` 以兼容 libc 的旧调用回退。
原有针对 daemon PID 1/2 的 kill-family 保护保持生效，普通子进程信号不受影响。

调试器、性能分析、内核密钥环、io_uring 和嵌套容器等依赖上述调用的功能仍受限制，和原默认策略一致。
当前没有清空基线的配置或 API；这类特殊负载应使用另行评估的专用运行环境。
默认放行其余调用是兼容性选择，并不意味着新内核增加的调用已经过安全审计。

支持小端、64 位原生 x86_64（amd64）和 ARM64（aarch64）。Windows 小艺 Work 仍走 Windows runner，
不构建或加载 Seccomp，不需要修改桌面配置。仅推理代理模式不创建 Linux 沙箱，也不要求 Seccomp。

## 架构与 ABI

系统调用编号随包内置于 `supervisor/syscall_numbers.py`，不会读取宿主机 `/usr/include`。
映射来源和固定内核版本列于文件头；维护新增配置调用时，需同时更新相应架构映射及测试。
未知、拼写错误或当前架构映射尚未收录的调用名会使沙箱创建失败，不再静默跳过。
不同架构的配置列表独立生效。

BPF 首先校验 `seccomp_data.arch`，错误架构会终止调用进程。x86_64 额外拒绝带 x32 标志位的调用，
以及旧内核的 512–547 x32 编号范围（返回 `ENOSYS`）。不支持 32 位和 x32 应用；这项限制无需
改变常规 64 位工作负载。未知 CPU 架构、非小端或 32 位服务解释器会拒绝创建 Linux 沙箱。

参考：[Linux x86 syscall 表](https://github.com/torvalds/linux/blob/v6.12/arch/x86/entry/syscalls/syscall_64.tbl)、
[ARM64 使用的通用编号表](https://github.com/torvalds/linux/blob/v6.6/include/uapi/asm-generic/unistd.h)、
[Seccomp ABI 注意事项](https://man7.org/linux/man-pages/man2/seccomp.2.html)。

## 启动失败与部署

过滤器构建、memfd 创建或写入失败，沙箱创建返回明确错误并释放控制 socket、令牌缓存、网络及其他
已分配的沙箱资源。不会退回无 Seccomp 执行。bubblewrap 安装过滤器失败并退出时，沙箱不会标记 ready，
错误包含已有启动日志。部署需提供支持 Seccomp filter 的 Linux 内核和支持 `--seccomp` 的 bubblewrap。

遇到 `Seccomp is required ... refusing to start` 应检查异常详情、CPU/解释器 ABI、配置名称及 memfd
支持；遇到 bubblewrap 加载失败应检查内核与外层容器策略。通过修正部署恢复服务，不应删掉过滤器重试。
已有运行中沙箱不会热替换过滤器；升级后应通过正常停止、重建流程让新保护生效。

Seccomp 用于缩小内核攻击面，不能单独证明没有逃逸风险；命名空间、Capabilities、Landlock 和宿主机
内核补丁仍是隔离的一部分。

## 验收

在安装源码包的 JiuwenBox 环境运行：

```bash
python -m pytest tests/unit/test_seccomp_security.py -q
# 在 Linux x86_64 / ARM64 上执行；Node、JDK、Git、cc 需安装以覆盖对应工作负载。
python -m pytest tests/integration/test_seccomp_native.py -q
```

便携测试执行实际 BPF 字节码，覆盖双架构基线、错误 ABI、x32、额外配置、信号保护、FD 失败清理及
Windows 创建分流。原生测试仅在独立子进程中安装真实内核过滤器，验证继承、危险调用与正常文件、线程、
网络、子进程、Node、Java、Git、编译器操作。缺少对应运行时会跳过相应项。

完整部署还应运行既有 `tests/integration/test_server_api_default.py` 中的 Seccomp 启动和调用拦截用例，
验证实际 bubblewrap、namespace 和 Landlock 组合；便携模拟和原生子进程测试不替代该部署验收。
