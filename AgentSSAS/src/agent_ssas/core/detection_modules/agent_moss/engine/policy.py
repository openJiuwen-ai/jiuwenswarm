# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from .events import EventRecord, PolicyDecision
from .pdg import DataLeakagePDGDetector


_DANGEROUS_SHELL_PATTERNS = [
    (r"\brm\s+(?:-[^\s;|&]*[rR][^\s;|&]*[fF][^\s;|&]*|-[^\s;|&]*[fF][^\s;|&]*[rR][^\s;|&]*)\s+/(?:\s|$)", "recursive force remove at filesystem root", 95),
    (r"\brm\s+(?:-[^\s;|&]*[rR][^\s;|&]*[fF][^\s;|&]*|-[^\s;|&]*[fF][^\s;|&]*[rR][^\s;|&]*)\b", "recursive force remove", 82),
    (r"(?i)\brm\s+-[^\s;|&]*r[^\s;|&]*\s+/(?:etc|var|home|boot|usr|sys|proc)\b", "recursive remove of critical system directory", 90),
    (r"(?i)\brm\b[^\n;|&]*\*[^\n;|&]*", "wildcard delete operation", 82),
    (r"\bdd\b.+\bof=/dev/(?:sd[a-z]|disk\d|nvme)", "raw disk overwrite", 95),
    (r"(?i)\bdd\b(?=.*\bif=/dev/zero\b)(?=.*\bof=)", "zero-fill disk overwrite", 95),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork bomb", 100),
    (r"\bchmod\s+(?:-[^\s]+\s+)*777\b", "world-writable chmod", 82),
    (r"\bchmod\s+-R\s+777\b", "recursive world-writable chmod", 82),
    (r"\b(curl|wget)\b.+\|\s*(sh|bash|zsh|python|python3)\b", "downloaded script execution", 88),
    (r"\bmkfs\.[a-z0-9]+\b", "filesystem creation command", 95),
    (r"(?i)\bgit\s+push\b[^\n;|&]*--force(?:-with-lease)?\b", "force push operation", 82),
    (r"(?i)\bsudo\b", "privilege escalation command", 70),
]

_NETWORK_TRANSFER_PATTERNS = [
    (r"\b(curl|wget|httpie|scp|rsync|ftp|sftp)\b", "external transfer capable command", 45),
    (r"https?://[^\s\"']+", "external URL in operation", 30),
    (r"\b(send_file|upload|webhook|post|put)\b", "possible outbound transfer", 35),
]

_EXFILTRATION_PATTERNS = [
    (r"(?is)\bcurl\b(?=.*\b(?:-d|--data|--data-binary|--data-urlencode|--upload-file|-T|--form|-F)\b)", "curl data upload", 85),
    (r"(?is)\bwget\b(?=.*\b(?:--post-data|--post-file|--method=POST)\b)", "wget data upload", 85),
    (r"(?is)\b(?:curl|wget|scp|rsync)\b(?=.*(?:/etc/shadow|/etc/passwd|/etc/sudoers|\.ssh/id_|api[_-]?key|secret|token|password))", "sensitive data exfiltration command", 90),
    (r"(?i)\bnc\b[^\n;|&]*(?:-e\s|/bin/(?:sh|bash)\b)", "netcat reverse shell", 90),
    (r"(?i)/dev/(?:tcp|udp)/[^\s\"']+", "tcp/udp pseudo-device shell", 86),
]

_SENSITIVE_PATH_PATTERNS = [
    (r"(?i)(?:^|[\s\"'=:@])/(?:etc/(?:shadow|sudoers)|dev/mem|var/run/docker\.sock)(?:[\s\"':/]|$)", "sensitive path access: critical credential or control file", 92),
    (r"(?i)(?:^|[\s\"'=:@])/(?:boot|proc/sys|sys)(?:/|[\s\"':]|$)", "sensitive path access: critical system directory", 88),
    (r"(?i)(?:^|[\s\"'=:@])(?:~|/[^\s\"']*)?/?\.ssh/(?:id_rsa|id_ed25519|id_ecdsa)(?:\b|[\s\"'])", "sensitive path access: ssh private key", 92),
    (r"(?i)(?:^|[\s\"'=:@])/(?:etc/passwd|etc/ssh/sshd_config|etc/crontab|etc/systemd/)(?:[\s\"':/]|$)", "sensitive path access: system configuration", 78),
    (r"(?i)(?:^|[\s\"'=:@])(?:[^\s\"']*/)?(?:\.env|credentials(?:\.json)?|secrets?\.ya?ml|llm_secrets\.json)(?:[\s\"']|$)", "sensitive path access: credential file", 82),
]

_ACCOUNT_MANAGEMENT_PATTERNS = [
    (r"(?i)\|\s*passwd\b", "non-interactive password modification", 88),
    (r"(?i)\bpasswd\s+(?:--stdin|-[^\s;|&]*[dl][^\s;|&]*)\b", "password state modification", 88),
    (r"(?i)\b(?:chpasswd|newusers|lpasswd)\b", "batch account credential modification", 88),
    (r"(?i)\b(?:useradd|usermod|groupmod)\b[^\n;|&]*\s-p\s+\S+", "account password hash injection", 88),
    (r"(?i)\b(?:userdel|deluser)\b", "system user deletion", 86),
    (r"(?i)\bgpasswd\b(?!\s+-(?:a|d|A)\b)", "group password management", 82),
]

_PERSISTENCE_PATTERNS = [
    (r"(?i)\bcrontab\s+(?:-e|-r)\b", "cron table modification", 82),
    (r"(?i)(?:\|\s*crontab\b|>>?\s*/etc/(?:crontab|cron\.d/|cron\.daily/|cron\.hourly/))", "cron persistence modification", 88),
    (r"(?i)(?:>>?|tee\s+-?a?)\s+(?:~|/[^\s\"']*)?/?\.ssh/authorized_keys\b", "ssh authorized_keys persistence", 88),
    (r"(?i)(?:>>?|tee\s+-?a?|chmod\s+\+x|chmod\s+[0-7]{3,4})\s+[^\n;|&]*\.git/hooks/", "git hook persistence modification", 84),
    (r"(?i)(?:>>?|tee\s+-?a?)\s+/etc/systemd/system/[^\s\"']+\.service", "systemd service persistence file write", 88),
    (r"(?i)\bsystemctl\s+enable\b", "systemd service persistence enable", 82),
]

_OBFUSCATED_EXECUTION_PATTERNS = [
    (r"(?i)\bbase64\b[^\n;|&]*(?:-d|--decode)[^\n;|&]*(?:\|\s*(?:sh|bash|zsh|python|python3)\b|\b(?:sh|bash|zsh|python|python3)\s+-c\b)", "base64 decoded execution", 88),
    (r"(?i)\b(?:eval|exec|system)\s*\(", "dynamic code execution", 82),
]

_PROMPT_INJECTION_PATTERNS = [
    (r"(?i)ignore (?:all )?(?:previous|prior|above) instructions", "instruction override attempt", 60),
    (r"忽略(?:之前|以上|上面)的(?:所有)?指令", "instruction override attempt", 60),
    (r"(?i)reveal (?:the )?(?:system|developer) prompt", "prompt exfiltration attempt", 65),
    (r"(?:泄露|输出|展示|打印)(?:系统|开发者)提示", "prompt exfiltration attempt", 65),
    (r"(?i)disable (?:safety|guardrails|tools? restrictions|monitoring)", "safety bypass attempt", 70),
    (r"(?i)(?:bypass|skip|disable).{0,32}(?:audit|security|guard|monitor)", "security monitor bypass attempt", 80),
    (r"(?:绕过|跳过|禁用|关闭).{0,16}(?:审计|安全检查|防护|监控)", "security monitor bypass attempt", 80),
    (r"(?i)you are now (?:in|under) developer mode", "role hijack attempt", 55),
    (r"(?i)\b(?:developer mode|DAN mode|jailbreak)\b", "jailbreak attempt", 70),
    (r"(?i)do not tell (?:the )?user", "hidden instruction attempt", 50),
    (r"(?i)<script\b|javascript:|onerror\s*=", "script injection attempt", 65),
]

_DEFAULT_SENSITIVE_PATTERNS = [
    ("api_key", r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{12,}"),
    ("private_key", r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
    ("credit_card", r"\b(?:\d[ -]*?){13,19}\b"),
]

_ENFORCEMENT_SCOPE_ALIASES = {
    "all": "all",
    "atomic": "all",
    "atomic_and_chain": "all",
    "full": "all",
    "behavior_chain": "behavior_chain",
    "chain": "behavior_chain",
    "chain_only": "behavior_chain",
    "observe": "observe",
    "analysis": "observe",
    "record": "observe",
    "none": "observe",
}


def _normalize_enforcement_scope(value: str) -> str:
    return _ENFORCEMENT_SCOPE_ALIASES.get(value.strip().lower(), "behavior_chain")


def _config_bool(cfg: dict[str, Any], key: str, default: bool) -> bool:
    value = cfg.get(key)
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class PolicyEngine:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.ask_threshold = int(cfg.get("ask_threshold", 45))
        self.block_threshold = int(cfg.get("block_threshold", 80))
        self.default_decision = str(cfg.get("default_decision", "allow"))
        self.enforcement_scope = _normalize_enforcement_scope(
            str(cfg.get("enforcement_scope", "behavior_chain"))
        )
        self.enforce_behavior_chain = _config_bool(
            cfg,
            "enforce_behavior_chain",
            bool(cfg.get("block_on_sensitive_exfiltration", True)),
        )
        self.analyze_behavior_chain = _config_bool(cfg, "analyze_behavior_chain", True)
        self.analyze_destructive_shell = _config_bool(
            cfg,
            "analyze_destructive_shell",
            bool(cfg.get("block_on_destructive_shell", True)),
        )
        self.analyze_sensitive_path_access = _config_bool(
            cfg,
            "analyze_sensitive_path_access",
            bool(cfg.get("block_on_sensitive_path_access", True)),
        )
        self.analyze_data_exfiltration = _config_bool(
            cfg,
            "analyze_data_exfiltration",
            bool(cfg.get("block_on_data_exfiltration", True)),
        )
        self.analyze_privileged_account_ops = _config_bool(
            cfg,
            "analyze_privileged_account_ops",
            bool(cfg.get("block_on_privileged_account_ops", True)),
        )
        self.analyze_persistence_ops = _config_bool(
            cfg,
            "analyze_persistence_ops",
            bool(cfg.get("block_on_persistence_ops", True)),
        )
        self.analyze_obfuscated_execution = _config_bool(
            cfg,
            "analyze_obfuscated_execution",
            bool(cfg.get("block_on_obfuscated_execution", True)),
        )
        self.enforce_pdg_data_leakage = _config_bool(
            cfg,
            "enforce_pdg_data_leakage",
            True,
        )
        self.sensitive_patterns = self._compile_sensitive_patterns(
            cfg.get("sensitive_patterns")
        )
        self.pdg_data_leakage = DataLeakagePDGDetector(cfg)

    def evaluate_event(
        self,
        event_type: str,
        subject: str,
        payload: dict[str, Any],
        history: Iterable[EventRecord],
    ) -> PolicyDecision:
        text = self._payload_text(subject, payload)
        signal_score = 0
        chain_score = 0
        findings: list[str] = []

        if event_type in {"chat_request", "model_output", "model_call"}:
            signal_score = max(
                signal_score,
                self._match_patterns(text, _PROMPT_INJECTION_PATTERNS, findings),
            )

        sensitive_score = self._match_sensitive(text, findings)
        signal_score = max(signal_score, sensitive_score)

        if event_type == "tool_call":
            tool_score = self._evaluate_tool_call(subject, text, findings)
            signal_score = max(signal_score, tool_score)
            chain_score = self._evaluate_chain(subject, text, history, findings)
            pdg_inspection = self.pdg_data_leakage.inspect(
                event_type=event_type,
                subject=subject,
                payload=payload,
                history=history,
            )
            if pdg_inspection.findings:
                findings.extend(pdg_inspection.findings)
                signal_score = max(signal_score, pdg_inspection.risk_score)
                if self.enforce_pdg_data_leakage:
                    chain_score = max(chain_score, pdg_inspection.risk_score)

        score = max(signal_score, chain_score)
        decision = self._decision_for(
            self._enforced_score(signal_score=signal_score, chain_score=chain_score)
        )
        reason = "; ".join(findings[:4])
        return PolicyDecision(decision=decision, risk_score=score, findings=findings, reason=reason)

    def _evaluate_tool_call(self, subject: str, text: str, findings: list[str]) -> int:
        score = 0
        lower_subject = subject.lower()
        shell_like = any(
            name in lower_subject
            for name in ("bash", "shell", "terminal", "run_command", "execute", "cmd")
        )
        if shell_like:
            if self.analyze_destructive_shell:
                score = max(score, self._match_patterns(text, _DANGEROUS_SHELL_PATTERNS, findings))
            if self.analyze_privileged_account_ops:
                score = max(score, self._match_patterns(text, _ACCOUNT_MANAGEMENT_PATTERNS, findings))
            if self.analyze_persistence_ops:
                score = max(score, self._match_patterns(text, _PERSISTENCE_PATTERNS, findings))
            if self.analyze_obfuscated_execution:
                score = max(score, self._match_patterns(text, _OBFUSCATED_EXECUTION_PATTERNS, findings))

        if self.analyze_sensitive_path_access and (
            shell_like or self._is_file_access_tool(lower_subject)
        ):
            score = max(score, self._match_patterns(text, _SENSITIVE_PATH_PATTERNS, findings))

        if self.analyze_data_exfiltration:
            score = max(score, self._match_patterns(text, _EXFILTRATION_PATTERNS, findings))

        score = max(score, self._match_patterns(text, _NETWORK_TRANSFER_PATTERNS, findings))
        return score

    def _evaluate_chain(
        self,
        subject: str,
        text: str,
        history: Iterable[EventRecord],
        findings: list[str],
    ) -> int:
        if not self.analyze_behavior_chain:
            return 0
        recent = list(history)[-30:]
        saw_sensitive = any("sensitive:" in " ".join(item.findings) for item in recent)
        saw_prompt_injection = any("attempt" in " ".join(item.findings) for item in recent)
        outbound = self._match_patterns(text, _NETWORK_TRANSFER_PATTERNS, [])
        if saw_sensitive and outbound:
            findings.append("behavior chain: sensitive data followed by outbound transfer")
            return 90
        if saw_prompt_injection and outbound:
            findings.append("behavior chain: prompt-injection context followed by outbound transfer")
            return 70
        return 0

    def _enforced_score(self, *, signal_score: int, chain_score: int) -> int:
        if self.enforcement_scope == "all":
            return max(signal_score, chain_score if self.enforce_behavior_chain else 0)
        if self.enforcement_scope == "observe":
            return 0
        if not self.enforce_behavior_chain:
            return 0
        return chain_score

    def _decision_for(self, score: int) -> str:
        if score >= self.block_threshold:
            return "block"
        if score >= self.ask_threshold:
            return "ask"
        return self.default_decision

    def _match_sensitive(self, text: str, findings: list[str]) -> int:
        score = 0
        for name, regex in self.sensitive_patterns:
            if regex.search(text):
                findings.append(f"sensitive:{name}")
                score = max(score, 70)
        return score

    @staticmethod
    def _match_patterns(
        text: str,
        patterns: Iterable[tuple[str, str, int]],
        findings: list[str],
    ) -> int:
        score = 0
        for pattern, label, risk in patterns:
            if re.search(pattern, text):
                findings.append(label)
                score = max(score, risk)
        return score

    @staticmethod
    def _is_file_access_tool(lower_subject: str) -> bool:
        return any(
            name in lower_subject
            for name in (
                "cat",
                "file",
                "grep",
                "head",
                "read",
                "tail",
                "write",
                "workspace",
            )
        )

    @staticmethod
    def _payload_text(subject: str, payload: dict[str, Any]) -> str:
        return subject + "\n" + json.dumps(payload, ensure_ascii=False, default=repr)

    @staticmethod
    def _compile_sensitive_patterns(raw: Any) -> list[tuple[str, re.Pattern[str]]]:
        entries = raw if isinstance(raw, list) else []
        compiled: list[tuple[str, re.Pattern[str]]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "custom")
            pattern = entry.get("pattern")
            if not pattern:
                continue
            compiled.append((name, re.compile(str(pattern))))
        if compiled:
            return compiled
        return [(name, re.compile(pattern)) for name, pattern in _DEFAULT_SENSITIVE_PATTERNS]
