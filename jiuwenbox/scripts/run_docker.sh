#!/usr/bin/env bash
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CALLER_CWD="$(pwd)"

IMAGE_NAME="${JIUWENBOX_IMAGE_NAME:-jiuwenbox}"
IMAGE_TAG="${JIUWENBOX_IMAGE_TAG:-latest}"
IMAGE_REF="${IMAGE_NAME}:${IMAGE_TAG}"
CONTAINER_NAME="${JIUWENBOX_CONTAINER_NAME:-jiuwenbox}"
HOST_PORT="${JIUWENBOX_HOST_PORT:-8321}"
PROXY_PORT="${JIUWENBOX_PROXY_PORT:-8322}"

# 下面这一段把每个外部 env 拆成"局部短名"承接, 与上面 HOST_PORT /
# PROXY_PORT 的风格一致; 真正"对外"的环境变量名永远是
# ``JIUWENBOX_LISTEN`` / ``JIUWENBOX_UDS_MODE`` / ``JIUWENBOX_UDS_HOST_DIR`` /
# ``JIUWENBOX_UDS_CONTAINER_DIR``, 这些才是用户在 README 里设的那一份。

# 管理 API listen URI: 默认与 Dockerfile 一致走 HTTP; 设成 unix:///abs/path
# 即切到 UDS, 容器内 uvicorn 监听 socket 文件, 由下面 ``-v`` 把宿主目录挂进去。
LISTEN_URI="${JIUWENBOX_LISTEN:-http://0.0.0.0:8321}"
# UDS chmod (lifespan 会读); 默认 0666 便于宿主非 root 直接访问, 多租户场景
# 可显式 ``JIUWENBOX_UDS_MODE=0660`` + ``docker run --user`` 收紧。
UDS_MODE="${JIUWENBOX_UDS_MODE:-}"
# 宿主侧 socket 目录 / 容器侧挂载点; 容器路径必须与 JIUWENBOX_LISTEN 里
# socket 所在目录一致, 否则 uvicorn 在容器内 bind 不到。
UDS_HOST_DIR="${JIUWENBOX_UDS_HOST_DIR:-/tmp/jiuwenbox-sock}"
UDS_CONTAINER_DIR="${JIUWENBOX_UDS_CONTAINER_DIR:-/run/jiuwenbox}"

# 持久化沙箱日志: 设置 JIUWENBOX_SAVE_LOGS_HOST_DIR 即开启, 宿主目录会被 bind
# 进容器, 且容器内 launcher 自动加 ``--save-logs <容器路径>``。容器路径可
# 通过 JIUWENBOX_SAVE_LOGS_CONTAINER_DIR 覆盖, 默认 /var/log/jiuwenbox。
SAVE_LOGS_HOST_DIR="${JIUWENBOX_SAVE_LOGS_HOST_DIR:-}"
SAVE_LOGS_CONTAINER_DIR="${JIUWENBOX_SAVE_LOGS_CONTAINER_DIR:-/var/log/jiuwenbox}"

POLICY_CONFIG=""
CONTAINER_POLICY_PATH="/app/runtime-config/policy.yaml"
# Unset JIUWENBOX_POLICY_PATH → launcher uses bundled ``jiuwenbox/configs/default-policy.yaml``.
DOCKER_ENV_ARGS=()
DOCKER_VOLUME_ARGS=()
DOCKER_PORT_ARGS=()

usage() {
  cat <<'EOF'
Usage: scripts/run_docker.sh [policy-config.yaml] [--save-logs DIR] [docker run args...]

Options:
  --save-logs DIR       Persist the per-sandbox audit JSONL to host
                        directory DIR. The script bind-mounts DIR into
                        the container and starts jiuwenbox with
                        --save-logs pointing at the container-side path.
                        Raw daemon / background-exec stdout/stderr is
                        never persisted (the audit log already carries
                        per-command stdout/stderr). Equivalent to
                        setting JIUWENBOX_SAVE_LOGS_HOST_DIR; the CLI
                        flag wins when both are present.

Examples (HTTP, default):
  scripts/run_docker.sh
  scripts/run_docker.sh src/jiuwenbox/configs/default-policy.yaml
  JIUWENBOX_HOST_PORT=18321 scripts/run_docker.sh

Examples (Unix Domain Socket):
  mkdir -p /tmp/jiuwenbox-sock
  JIUWENBOX_LISTEN=unix:///run/jiuwenbox/jiuwenbox.sock \
  JIUWENBOX_UDS_HOST_DIR=/tmp/jiuwenbox-sock \
    scripts/run_docker.sh src/jiuwenbox/configs/default-policy.yaml

  curl --unix-socket /tmp/jiuwenbox-sock/jiuwenbox.sock http://localhost/health

Examples (persist sandbox audit log to host):
  scripts/run_docker.sh --save-logs /tmp/jiuwenbox-logs
  scripts/run_docker.sh src/jiuwenbox/configs/default-policy.yaml --save-logs /tmp/jiuwenbox-logs
  # ls /tmp/jiuwenbox-logs
  # <id>-20260515T112345.audit.log
EOF
}

# 第一遍扫描: 把脚本自己识别的 long flag (目前只有 --save-logs) 从 ``$@``
# 里抠出来, 剩下的位置参数 + 透传给 ``docker run`` 的尾部都保留原顺序。
# 这样用户既可以写 ``run_docker.sh --save-logs DIR configs/foo.yaml``
# 也可以写 ``run_docker.sh path/to/foo.yaml --save-logs DIR``, 而原来"第一个
# 非 -* 实参当 policy-config" / "尾部任意 docker run 参数透传" 的行为不变。
SAVE_LOGS_CLI=""
REMAINING_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --save-logs=*)
      SAVE_LOGS_CLI="${1#--save-logs=}"
      if [[ -z "$SAVE_LOGS_CLI" ]]; then
        echo "error: --save-logs requires a non-empty directory path" >&2
        exit 2
      fi
      shift
      ;;
    --save-logs)
      if [[ $# -lt 2 || "$2" == --* ]]; then
        echo "error: --save-logs requires a directory path" >&2
        exit 2
      fi
      SAVE_LOGS_CLI="$2"
      shift 2
      ;;
    *)
      REMAINING_ARGS+=("$1")
      shift
      ;;
  esac
done
# 用清洗过的数组覆盖 ``$@``, 让后面所有 ``$1`` / ``"$@"`` 仍按既有顺序工作。
if (( ${#REMAINING_ARGS[@]} > 0 )); then
  set -- "${REMAINING_ARGS[@]}"
else
  set --
fi

# CLI flag 优先于环境变量, 否则继续读 JIUWENBOX_SAVE_LOGS_HOST_DIR (历史接口)。
if [[ -n "$SAVE_LOGS_CLI" ]]; then
  SAVE_LOGS_HOST_DIR="$SAVE_LOGS_CLI"
fi

if [[ $# -gt 0 ]]; then
  case "$1" in
    -*)
      ;;
    *)
      POLICY_CONFIG="$1"
      shift
      ;;
  esac
fi

# 解析 listen URI: http:// 或 unix:///abs/path; 其它形态拒绝。
LISTEN_MODE=""
LISTEN_SOCKET_PATH=""
case "$LISTEN_URI" in
  http://*)
    LISTEN_MODE="http"
    ;;
  unix:///*)
    LISTEN_MODE="uds"
    LISTEN_SOCKET_PATH="${LISTEN_URI#unix://}"
    ;;
  *)
    echo "error: JIUWENBOX_LISTEN must start with http:// or unix:///, got '$LISTEN_URI'" >&2
    exit 1
    ;;
esac

echo "Starting jiuwenbox container:"
echo "  image:     $IMAGE_REF"
echo "  container: $CONTAINER_NAME"
if [[ "$LISTEN_MODE" = "http" ]]; then
  echo "  listen:    http -> http://127.0.0.1:${HOST_PORT}"
  DOCKER_PORT_ARGS+=(-p "${HOST_PORT}:8321")
else
  echo "  listen:    uds -> ${UDS_HOST_DIR}/$(basename "$LISTEN_SOCKET_PATH")"
  # 校验: socket 容器路径必须在容器内挂载点下面, 否则宿主看不见。
  case "$LISTEN_SOCKET_PATH" in
    "${UDS_CONTAINER_DIR}"/*)
      ;;
    *)
      echo "warning: socket path '$LISTEN_SOCKET_PATH' is not under" \
        "container mount dir '$UDS_CONTAINER_DIR'; the host" \
        "won't see the socket file. Adjust JIUWENBOX_LISTEN or" \
        "JIUWENBOX_UDS_CONTAINER_DIR to match." >&2
      ;;
  esac
  mkdir -p "$UDS_HOST_DIR"
  DOCKER_VOLUME_ARGS+=(-v "${UDS_HOST_DIR}:${UDS_CONTAINER_DIR}")
fi
# 代理端口在两种模式下都映射: Inference Privacy Proxy 是独立 TCP listener,
# 与管理 API 传输方式无关; 即便管理面走 UDS, 代理仍可能需要从宿主转出流量。
DOCKER_PORT_ARGS+=(-p "${PROXY_PORT}:8322")
echo "  proxy:     http://127.0.0.1:${PROXY_PORT} (inference privacy proxy)"

# 把 listen / uds-mode 显式 -e 给容器, 让容器 entrypoint 拿到一致的视图。
# 注意: 这里 -e 后面的"键名"必须仍是 JIUWENBOX_LISTEN / JIUWENBOX_UDS_MODE,
# 它们是 launcher / lifespan 真正读取的环境变量, 与外部用户在 README 里设的
# 名字保持一致; 等号右边才是上面解析好的局部变量值。
DOCKER_ENV_ARGS+=(-e "JIUWENBOX_LISTEN=${LISTEN_URI}")
if [[ -n "$UDS_MODE" ]]; then
  DOCKER_ENV_ARGS+=(-e "JIUWENBOX_UDS_MODE=${UDS_MODE}")
fi

# 沙箱日志持久化: 仅在 SAVE_LOGS_HOST_DIR 显式设置时启用, 默认完全不影响
# 现有部署 (服务沿用 ~/.jiuwenbox/{logs,sandbox_logs} 私有缓存)。
if [[ -n "$SAVE_LOGS_HOST_DIR" ]]; then
  mkdir -p "$SAVE_LOGS_HOST_DIR"
  SAVE_LOGS_HOST_DIR_ABS="$(realpath "$SAVE_LOGS_HOST_DIR")"
  DOCKER_VOLUME_ARGS+=(-v "${SAVE_LOGS_HOST_DIR_ABS}:${SAVE_LOGS_CONTAINER_DIR}")
  # 直接给容器内 launcher 设环境变量, 无需改 CMD; lifespan 会读到。
  DOCKER_ENV_ARGS+=(-e "JIUWENBOX_SAVE_LOGS_DIR=${SAVE_LOGS_CONTAINER_DIR}")
  echo "  save-logs: ${SAVE_LOGS_HOST_DIR_ABS} -> ${SAVE_LOGS_CONTAINER_DIR}"
fi

if [[ -n "$POLICY_CONFIG" ]]; then
  POLICY_CONFIG_ABS=""

  if [[ "$POLICY_CONFIG" = /* && -f "$POLICY_CONFIG" ]]; then
    POLICY_CONFIG_ABS="$(realpath "$POLICY_CONFIG")"
  elif [[ -f "$CALLER_CWD/$POLICY_CONFIG" ]]; then
    POLICY_CONFIG_ABS="$(realpath "$CALLER_CWD/$POLICY_CONFIG")"
  elif [[ -f "$PROJECT_DIR/$POLICY_CONFIG" ]]; then
    POLICY_CONFIG_ABS="$(realpath "$PROJECT_DIR/$POLICY_CONFIG")"
  else
    echo "error: policy config not found: $POLICY_CONFIG" >&2
    exit 1
  fi

  DOCKER_ENV_ARGS+=(-e "JIUWENBOX_POLICY_PATH=${CONTAINER_POLICY_PATH}")
  DOCKER_VOLUME_ARGS+=(-v "${POLICY_CONFIG_ABS}:${CONTAINER_POLICY_PATH}:ro")
  echo "  policy:    $POLICY_CONFIG_ABS"
else
  echo "  policy:    bundled default-policy.yaml (wheel)"
fi

echo

docker run -itd \
    --name "$CONTAINER_NAME" \
    --restart=unless-stopped \
    --sysctl net.ipv4.ip_forward=1 \
    --cap-add=SYS_ADMIN \
    --cap-add=NET_ADMIN \
    --security-opt seccomp=unconfined \
    --security-opt apparmor=unconfined \
    --security-opt systempaths=unconfined \
    --cgroupns=host \
    -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
    "${DOCKER_PORT_ARGS[@]}" \
    "${DOCKER_ENV_ARGS[@]}" \
    "${DOCKER_VOLUME_ARGS[@]}" \
    "$@" \
    "$IMAGE_REF"
