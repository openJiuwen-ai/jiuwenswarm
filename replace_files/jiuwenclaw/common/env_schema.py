# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Environment variable synchronization schema between Gateway and AgentServer.

Security classification based on Kubernetes and container runtime requirements:
- Read-only system variables (K8s injected)
- Sidecar/framework variables (runtime injected)
- Application config variables (safe to modify)
"""

from typing import Set

# 允许从 GW 同步到 AgentServer 的环境配置变量
ALLOWED_ENV_KEYS: Set[str] = {
    # 提示词
    "SKILLDEV_AGENT_SYSTEM_PROMPT",
    # 日志配置
    "LOG_ROOT_PATH",
    "INTERFACE_LOG_PATH",
    # LLM 大模型配置
    "LLM_API_BASE",
    "LLM_MODEL_NAME",
    "LLM_MODEL_PROVIDER",
    "LLM_RETRY_MAX_ATTEMPTS",
    "LLM_RETRY_MAX_BACKOFF",
    # 沙箱配置
    "SANDBOX_INIT_DATA_PATH",
    "SANDBOX_OSMS_SLB_URL",
    "SANDBOX_ENABLE",
    # OBS 传输配置
    "OBS_TRANSFER_USE_EDGE",
    "OBS_TRANSFER_PREVIEW_SUPPORTED_FILE_FORMAT",
    # AgentServer 连接配置
    "AGENTSERVER_TO_OA_WS_URL",
    # Skill 服务配置
    "SKILL_SEARCH_URL",
    "SKILL_SCAN_URL",
    # Agent runtime
    "AGENT_RUNTIME_MCP_RUN"
}

# 禁止覆盖的系统/基础设施变量
PROTECTED_ENV_KEYS: Set[str] = {
    # K8s 系统变量
    "KUBERNETES_SERVICE_HOST",
    "KUBERNETES_SERVICE_PORT",
    "KUBERNETES_PORT",
    "KUBERNETES_PORT_443_TCP",
    "KUBERNETES_PORT_443_TCP_ADDR",
    "KUBERNETES_PORT_443_TCP_PORT",
    "KUBERNETES_PORT_443_TCP_PROTO",
    # K8s 服务账号相关
    "KUBERNETES_SERVICE_ACCOUNT_TOKEN",
    "KUBERNETES_SERVICE_ACCOUNT_NAME",
    # 容器运行时变量
    "HOSTNAME",
    "HOME",
    "USER_NAME",
    "USER_UID",
    "GROUP_ID",
    "PATH",
    "PWD",
    "OLDPWD",
    "TERM",
    "_",
    "SHLVL",
    "SANDBOX_WORKSPACE",
    # Sidecar/框架变量
    "USE_SIDECAR",
    "PICOD_AUTH_MODE",
    "PICOD_PUBLIC_KEY",
    "PICOD_DEFAULT_TTL",
    "PICOD_SERVICE_NAME",
    "PAAS_POD_ID",
    "GROUP_NAME",
    "PAAS_PROJECT_ID",
    "PAAS_CLUSTER_ID",
    "PAAS_NAMESPACE",
    "PAAS_POD_NAME",
    "PAAS_APP_NAME",
    "PAAS_APP_VERSION",
    "PAAS_CONTAINER_NAME",
    "PAAS_NODE_NAME",
    "PAAS_NODE_IP",
    "PAAS_POD_IP",
    "PAAS_SERVICE_ACCOUNT"
}
