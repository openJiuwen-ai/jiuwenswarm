#!/usr/bin/env bash
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

IMAGE_NAME="${JIUWENBOX_IMAGE_NAME:-jiuwenbox}"
IMAGE_TAG="${JIUWENBOX_IMAGE_TAG:-latest}"
IMAGE_REF="${IMAGE_NAME}:${IMAGE_TAG}"

usage() {
  cat <<'EOF'
Usage: scripts/build_docker.sh [docker build args...]

Build the jiuwenbox image only. The policy file is selected later when
running the container via scripts/run_docker.sh.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

docker build -f "$PROJECT_DIR/docker/Dockerfile" -t "$IMAGE_REF" "$PROJECT_DIR" "$@"
