"""RSI 服务域错误体系（错误码对齐 web 契约 §3.5 全集）。"""

from __future__ import annotations

from typing import Any


class RsiError(Exception):
    """RSI 服务域统一异常：携带对外错误码（web §3.5）。"""

    def __init__(self, code: str, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

class RsiBadRequest(RsiError):
    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__("BAD_REQUEST", message, detail=detail)


class RsiScenarioNotSupported(RsiError):
    def __init__(self, message: str = "scenario 或 artifact_type 非法") -> None:
        super().__init__("SCENARIO_NOT_SUPPORTED", message)


class RsiDatasetInvalid(RsiError):
    def __init__(self, message: str, *, errors: list[dict[str, str]] | None = None) -> None:
        super().__init__("DATASET_INVALID", message, detail=errors)
        self.errors = errors or []


class RsiPathInvalid(RsiError):
    def __init__(self, message: str = "本地路径不存在或后缀不符") -> None:
        super().__init__("PATH_INVALID", message)


class RsiPathNotAllowed(RsiError):
    """A supplied path is outside the configured RSI input root."""

    def __init__(self, message: str = "路径不在允许的 RSI 根目录内") -> None:
        super().__init__("PATH_NOT_ALLOWED", message)


class RsiInvalidHarness(RsiError):
    """The selected Harness package/config cannot be used for RSI."""

    def __init__(self, message: str = "Harness 配置无效") -> None:
        super().__init__("INVALID_HARNESS", message)


class RsiHarnessNotReady(RsiError):
    """The RSI task has not reached the publishable Harness state."""

    def __init__(self, message: str = "RSI Harness 尚未完成") -> None:
        super().__init__("RSI_HARNESS_NOT_READY", message)


class RsiHarnessNotPublished(RsiError):
    """The RSI engine did not publish a final Harness refs file."""

    def __init__(self, message: str = "RSI Harness 没有可发布版本") -> None:
        super().__init__("RSI_HARNESS_NOT_PUBLISHED", message)


class RsiHarnessInvalid(RsiError):
    """Published refs or package contents violate the RSI install contract."""

    def __init__(self, message: str = "RSI Harness 发布物无效") -> None:
        super().__init__("RSI_HARNESS_INVALID", message)


class RsiHarnessInstallConflict(RsiError):
    """A live Harness replacement could not be rolled back safely."""

    def __init__(self, message: str = "RSI Harness 安装发生资源冲突") -> None:
        super().__init__("RSI_HARNESS_INSTALL_CONFLICT", message)


class RsiHarnessInstallFailed(RsiError):
    """The RSI Harness install failed while preserving the old active version."""

    def __init__(self, message: str = "RSI Harness 安装失败") -> None:
        super().__init__("RSI_HARNESS_INSTALL_FAILED", message)


class RsiModelNotFound(RsiError):
    """A models.list reference did not resolve exactly to one model entry."""

    def __init__(self, message: str = "模型不存在") -> None:
        super().__init__("MODEL_NOT_FOUND", message)


class RsiModelConfigInvalid(RsiError):
    """A selected model entry cannot be materialized for openjiuwen."""

    def __init__(self, message: str = "模型配置无效") -> None:
        super().__init__("MODEL_CONFIG_INVALID", message)


class RsiUnsupportedParameter(RsiError):
    """A public parameter has no supported openjiuwen Validation mapping."""

    def __init__(self, message: str = "参数暂不支持") -> None:
        super().__init__("UNSUPPORTED_PARAMETER", message)


class RsiResumeInputChanged(RsiError):
    """Task-private input/config material changed since create."""

    def __init__(self, message: str = "resume 输入材料已变化") -> None:
        super().__init__("RESUME_INPUT_CHANGED", message)


class RsiTaskNotFound(RsiError):
    def __init__(self, task_id: str) -> None:
        super().__init__("TASK_NOT_FOUND", f"任务不存在: {task_id}")


class RsiTaskStateConflict(RsiError):
    def __init__(self, message: str) -> None:
        super().__init__("TASK_STATE_CONFLICT", message)


class RsiResumeMismatch(RsiError):
    def __init__(self, message: str = "resume fingerprint 校验失败") -> None:
        super().__init__("RESUME_MISMATCH", message)


class RsiArtifactNotFound(RsiError):
    def __init__(self, message: str = "artifact 不存在") -> None:
        super().__init__("ARTIFACT_NOT_FOUND", message)


class RsiNotReady(RsiError):
    """外部依赖未就绪的预留位（⚠️外部 / 中优先级接口骨架）。

    当前不可达或需外部(agent-core 事件化 / 引擎装配)后接线的路径，
    显式抛出而非静默假实现；前端收到 INTERNAL_ERROR 视作“暂不可用”。
    """

    def __init__(self, message: str) -> None:
        super().__init__("INTERNAL_ERROR", message)
