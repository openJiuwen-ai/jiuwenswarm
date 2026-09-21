# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""诊断日志隔离：_is_diagnosis_record 判定 + diagnosis.log 路由。"""
import logging

from jiuwenswarm.common import utils
from jiuwenswarm.common.utils import _is_diagnosis_record, setup_logger


def _record(name: str, request_id: str = "") -> logging.LogRecord:
    record = logging.LogRecord(
        name=name, level=logging.INFO, pathname="p", lineno=1,
        msg="m", args=(), exc_info=None,
    )
    record.request_id = request_id
    return record


def test_diagnosis_record_by_request_id_prefix() -> None:
    # AgentServer 侧 logger 与主流程同名，靠 request_id 前缀识别
    assert _is_diagnosis_record(
        _record("jiuwenswarm.server.runtime.agent_adapter.interface", "diagnosis-diagnosis_abc")
    )


def test_diagnosis_record_by_logger_name() -> None:
    # 落盘/配置类日志无 request_id，靠 logger 名识别
    assert _is_diagnosis_record(_record("jiuwenswarm.observability.diagnosis.upload"))


def test_normal_record_not_diagnosis() -> None:
    assert not _is_diagnosis_record(
        _record("jiuwenswarm.server.runtime.agent_adapter.interface", "chat-123")
    )
    assert not _is_diagnosis_record(_record("jiuwenswarm.gateway.routing"))
    # request_id 缺失的 record（未经过 factory 注入）
    record = logging.LogRecord(
        name="jiuwenswarm.server.app", level=logging.INFO, pathname="p",
        lineno=1, msg="m", args=(), exc_info=None,
    )
    assert not _is_diagnosis_record(record)


def test_diagnosis_log_file_created(monkeypatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_LOG_FORMAT", "text")
    setup_logger()
    names = [
        h.baseFilename.rsplit("/", 1)[-1]
        for h in utils._iter_log_output_handlers()
        if hasattr(h, "baseFilename")
    ]
    assert "diagnosis.log" in names


def test_diagnosis_routed_to_own_file(monkeypatch, tmp_path) -> None:
    """端到端：diagnosis request_id 的日志只落 diagnosis.log，不落主日志。"""
    monkeypatch.setenv("JIUWENSWARM_LOG_FORMAT", "text")
    monkeypatch.setenv("LOG_ROOT_PATH", str(tmp_path))
    setup_logger()

    def _read(name: str) -> str:
        return (tmp_path / name).read_text(encoding="utf-8")

    # 诊断日志：request_id 前缀（server 侧 logger）
    srv_logger = logging.getLogger("jiuwenswarm.server.runtime.agent_adapter.interface")
    record = logging.LogRecord(
        name=srv_logger.name, level=logging.INFO, pathname="p", lineno=1,
        msg="diagnosis-round", args=(), exc_info=None,
    )
    record.request_id = "diagnosis-diagnosis_abc"
    srv_logger.handle(record)
    # 诊断日志：observability logger 名
    logging.getLogger("jiuwenswarm.observability.diagnosis.upload").info("upload-done")
    # 主流程日志
    logging.getLogger("jiuwenswarm.gateway.routing").info("main-flow")
    utils.flush_queued_logs()

    assert "diagnosis-round" in _read("diagnosis.log")
    assert "upload-done" in _read("diagnosis.log")
    assert "main-flow" not in _read("diagnosis.log")
    for name in ("gateway.log", "agent_server.log", "full.log"):
        assert "diagnosis-round" not in _read(name), name
        assert "upload-done" not in _read(name), name
    # 主流程日志不受影响
    assert "main-flow" in _read("gateway.log")
    assert "main-flow" in _read("full.log")
