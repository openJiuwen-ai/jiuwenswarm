"""OHOS: maturin 平台校验 + pip 子进程工具链 PATH。"""
import platform


def _linux_system() -> str:
    # maturin/setuptools-rust 只认 "Linux"；OHOS 上伪装成 Linux 才能走
    # musl/Linux 工具链分支（真实判定在 ohos_build_env 里做）。
    return "Linux"


platform.system = _linux_system

try:
    import ohos_build_env  # noqa: F401
except Exception:
    pass
