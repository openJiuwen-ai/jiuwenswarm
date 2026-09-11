"""OHOS: maturin 平台校验 + pip 子进程工具链 PATH。"""
import platform

platform.system = lambda: "Linux"

try:
    import ohos_build_env  # noqa: F401
except Exception:
    pass
