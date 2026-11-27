#!/usr/bin/env bash
set -euo pipefail

DOCKER_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$DOCKER_DIR/.." && pwd)"

IMAGE_NAME="${JIUWENBOX_IMAGE_NAME:-jiuwenbox}"
IMAGE_TAG="${JIUWENBOX_IMAGE_TAG:-latest}"
IMAGE_REF="${IMAGE_NAME}:${IMAGE_TAG}"

docker build -f "$DOCKER_DIR/Dockerfile" --no-cache -t "$IMAGE_REF" "$PROJECT_DIR" "$@"
