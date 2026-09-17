"""OHOS: maturin 平台校验 + pip 子进程工具链 PATH。"""
import platform


def _linux_system() -> str:
    # maturin/setuptools-rust 只认 "Linux"；OHOS 上伪装成 Linux 才能走
    # musl/Linux 工具链分支（真实判定在 ohos_build_env 里做）。
    return "Linux"


platform.system = _linux_system

# 构建环境补丁加载失败时的异常记录（None = 成功）。sitecustomize 在每次
# 解释器启动时执行，绝不能向 stdout/stderr 写任何内容（会污染所有子进程
# 输出/管道），因此把失败挂到模块属性上，排障时：
#   python -c "import sitecustomize; print(sitecustomize.LOAD_ERROR)"
LOAD_ERROR = None
try:
    import ohos_build_env  # noqa: F401
except Exception as _exc:  # noqa: BLE001
    LOAD_ERROR = _exc
