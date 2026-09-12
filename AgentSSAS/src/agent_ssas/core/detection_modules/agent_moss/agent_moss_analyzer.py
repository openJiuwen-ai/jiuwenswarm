# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentMoss runtime behavior analysis plugin for AgentSSAS."""

from __future__ import annotations

from dataclasses import asdict
import logging
import threading
from typing import Any

from .engine.events import EventRecord
from .engine.policy import PolicyEngine
from .event_adapter import MODEL_TYPE, adapt_event_desc

logger = logging.getLogger(__name__)

_RISK_THRESHOLD_SCORES = {
    "safe": 0,
    "low": 1,
    "medium": 45,
    "high": 70,
    "critical": 90,
}


class AgentMossAnalyzer:
    """Run AgentMoss policy and PDG analysis over correlated SSAS events.

    The analyzer keeps bounded per-session history because AgentMoss behavior
    chain and PDG rules are stateful. The AgentSSAS module is subscribed in
    notify mode, so decisions are advisory reports and do not gate execution.
    """

    name: str = "AgentMossAnalyzer"
    expected_model_type: str = MODEL_TYPE

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._analysis_methods = self._config.get(
            "analysis_methods", ["rule", "behavior_chain", "pdg"]
        )
        self._risk_threshold = self._config.get("risk_threshold", "low")
        policy_config = self._config.get("policy", self._config)
        if not isinstance(policy_config, dict):
            policy_config = {}
        self._policy = PolicyEngine(policy_config)
        self._max_history_events = max(
            5, int(self._config.get("max_history_events", 200))
        )
        self._include_pdg_graph = _config_bool(
            self._config, "include_pdg_graph", True
        )
        self._history: dict[str, list[EventRecord]] = {}
        self._report_cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    async def analyze(self, model_data: Any) -> dict[str, Any]:
        """Analyze one adapted event and return an AgentSSAS threat report."""
        if not isinstance(model_data, dict):
            logger.warning(
                "AgentMossAnalyzer 收到非 dict 类型的 model_data: %s",
                type(model_data).__name__,
            )
            return self._empty_report(
                analysis_status="invalid_model_data",
                confidence=0.0,
            )

        if not isinstance(model_data.get("agentmoss_event"), dict):
            # Backward compatibility for callers that still pass event_desc.
            model_data = adapt_event_desc(model_data)

        event = model_data.get("agentmoss_event", {})
        if not model_data.get("supported") or not event.get("event_type"):
            return self._empty_report(
                analysis_status="unsupported_event",
                confidence=0.0,
                evidence={
                    "source_event_type": model_data.get("event_type", ""),
                    "adapter_version": model_data.get("adapter_version", ""),
                },
            )

        event_id = str(event.get("event_id") or "")
        with self._lock:
            cached = self._report_cache.get(event_id)
            if cached is not None:
                return _copy_report(cached)

            history_key = self._history_key(event)
            complete_history = list(self._history.get(history_key, []))
            timestamp = _float(event.get("timestamp"))
            history = [
                item
                for item in complete_history
                if (item.timestamp, item.event_id) < (timestamp, event_id)
            ]
            payload = event.get("payload", {})
            if not isinstance(payload, dict):
                payload = {"value": payload}
            decision = self._policy.evaluate_event(
                event_type=str(event.get("event_type") or ""),
                subject=str(event.get("subject") or ""),
                payload=payload,
                history=history,
            )
            pdg_evidence = self._pdg_evidence(
                event=event,
                payload=payload,
                history=history,
                findings=decision.findings,
            )
            record = EventRecord(
                event_type=str(event.get("event_type") or ""),
                subject=str(event.get("subject") or ""),
                payload=payload,
                session_id=str(event.get("session_id") or ""),
                request_id=str(event.get("request_id") or ""),
                agent_name=str(event.get("agent_name") or ""),
                source=str(event.get("source") or "agent_ssas"),
                event_id=event_id,
                timestamp=timestamp,
                risk_score=decision.risk_score,
                decision=decision.decision,
                findings=list(decision.findings),
                prev_event_ids=[item.event_id for item in history[-5:]],
            )
            complete_history.append(record)
            complete_history.sort(key=lambda item: (item.timestamp, item.event_id))
            self._history[history_key] = complete_history[-self._max_history_events :]

            report = self._build_report(
                model_data=model_data,
                decision=decision,
                pdg_evidence=pdg_evidence,
                history_size=len(history),
            )
            self._report_cache[event_id] = _copy_report(report)
            self._trim_report_cache()

        logger.debug(
            "AgentMoss 分析完成: event=%s score=%s decision=%s findings=%s",
            event.get("event_type"),
            decision.risk_score,
            decision.decision,
            len(decision.findings),
        )
        return report

    def _build_report(
        self,
        *,
        model_data: dict[str, Any],
        decision: Any,
        pdg_evidence: dict[str, Any],
        history_size: int,
    ) -> dict[str, Any]:
        score = int(decision.risk_score)
        risk_level = _risk_level(score)
        threats = _threat_types(decision.findings)
        threshold_score = _threshold_score(self._risk_threshold)
        has_risk = score > 0 and score >= threshold_score
        correlation = model_data.get("correlation", {})
        evidence: dict[str, Any] = {
            "analysis_status": "analyzed",
            "decision": decision.decision,
            "decision_mode": "advisory_notify",
            "findings": list(decision.findings),
            "event_mapping": {
                "source_event_type": model_data.get("event_type", ""),
                "agentmoss_event_type": model_data.get("agentmoss_event", {}).get(
                    "event_type", ""
                ),
                "adapter_version": model_data.get("adapter_version", ""),
            },
            "correlation": correlation if isinstance(correlation, dict) else {},
            "history_events_analyzed": history_size,
        }
        if pdg_evidence:
            evidence["pdg"] = pdg_evidence

        if decision.findings:
            description = (
                f"AgentMoss detected {len(decision.findings)} behavior signal(s); "
                f"decision={decision.decision}"
            )
        else:
            description = "AgentMoss found no behavior risk"

        return {
            "has_risk": has_risk,
            "risk_level": risk_level,
            "risk_type": threats[0] if threats else "",
            "risk_score": float(score),
            "confidence": 1.0,
            "detected_threats": threats,
            "recommended_actions": _recommended_actions(
                decision.decision, has_risk
            ),
            "analytic_name": "AgentMoss Runtime Behavior Analysis",
            "description": description,
            "evidence": evidence,
            "module_name": "agent_moss",
        }

    def _pdg_evidence(
        self,
        *,
        event: dict[str, Any],
        payload: dict[str, Any],
        history: list[EventRecord],
        findings: list[str],
    ) -> dict[str, Any]:
        if not any(item.startswith("pdg:") for item in findings):
            return {}
        inspection = self._policy.pdg_data_leakage.inspect(
            event_type=str(event.get("event_type") or ""),
            subject=str(event.get("subject") or ""),
            payload=payload,
            history=history,
        )
        evidence: dict[str, Any] = {
            "violations": [asdict(item) for item in inspection.violations],
        }
        if self._include_pdg_graph:
            evidence["graph"] = inspection.graph.to_dict()
        return evidence

    @staticmethod
    def _history_key(event: dict[str, Any]) -> str:
        session_id = str(event.get("session_id") or "")
        if session_id:
            return f"session:{session_id}"
        request_id = str(event.get("request_id") or "")
        if request_id:
            return f"request:{request_id}"
        # Missing correlation must not merge unrelated agents into global history.
        return f"uncorrelated:{event.get('event_id', '')}"

    def _trim_report_cache(self) -> None:
        limit = self._max_history_events * 4
        while len(self._report_cache) > limit:
            self._report_cache.pop(next(iter(self._report_cache)))

    @staticmethod
    def _empty_report(
        *,
        analysis_status: str = "analyzed",
        confidence: float = 1.0,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        report_evidence = {"analysis_status": analysis_status}
        if evidence:
            report_evidence.update(evidence)
        return {
            "has_risk": False,
            "risk_level": "safe",
            "risk_type": "",
            "risk_score": 0.0,
            "confidence": confidence,
            "detected_threats": [],
            "recommended_actions": ["log"],
            "analytic_name": "AgentMoss Runtime Behavior Analysis",
            "description": "AgentMoss event was not analyzed",
            "evidence": report_evidence,
            "module_name": "agent_moss",
        }


def _risk_level(score: int) -> str:
    if score <= 0:
        return "safe"
    if score < 45:
        return "low"
    if score < 70:
        return "medium"
    if score < 90:
        return "high"
    return "critical"


def _threshold_score(value: Any) -> int:
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return _RISK_THRESHOLD_SCORES.get(str(value).strip().lower(), 1)


def _threat_types(findings: list[str]) -> list[str]:
    types: list[str] = []
    text = "\n".join(findings).lower()
    candidates = (
        ("data_leakage", "pdg:data-leak"),
        ("behavior_chain", "behavior chain:"),
        ("prompt_injection", "instruction override"),
        ("prompt_injection", "prompt exfiltration"),
        ("security_bypass", "bypass attempt"),
        ("sensitive_data_exposure", "sensitive:"),
        ("sensitive_resource_access", "sensitive path access"),
        ("data_exfiltration", "exfiltration"),
        ("dangerous_tool_operation", "remove"),
        ("dangerous_tool_operation", "overwrite"),
        ("persistence", "persistence"),
        ("persistence", "cron table modification"),
        ("privileged_account_operation", "account credential modification"),
        ("privileged_account_operation", "account password"),
        ("privileged_account_operation", "password modification"),
        ("privileged_account_operation", "system user deletion"),
        ("privilege_escalation", "privilege escalation"),
        ("obfuscated_execution", "decoded execution"),
        ("obfuscated_execution", "dynamic code execution"),
        ("outbound_transfer", "external transfer"),
    )
    for threat_type, marker in candidates:
        if marker in text and threat_type not in types:
            types.append(threat_type)
    if findings and not types:
        types.append("agent_behavior_risk")
    return types


def _recommended_actions(decision: str, has_risk: bool) -> list[str]:
    if decision == "block":
        return ["block", "investigate"]
    if decision == "ask":
        return ["review", "ask_user"]
    if has_risk:
        return ["investigate", "log"]
    return ["log"]


def _copy_report(report: dict[str, Any]) -> dict[str, Any]:
    # Reports contain only JSON-like values; copy nested mutable containers.
    import copy

    return copy.deepcopy(report)


def _config_bool(config: dict[str, Any], key: str, default: bool) -> bool:
    value = config.get(key)
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
