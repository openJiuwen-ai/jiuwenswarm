#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

IMAGE_NAME="${JIUWENBOX_IMAGE_NAME:-jiuwenbox}"
IMAGE_TAG="${JIUWENBOX_IMAGE_TAG:-latest}"
IMAGE_REF="${IMAGE_NAME}:${IMAGE_TAG}"
CONTAINER_NAME="${JIUWENBOX_CONTAINER_NAME:-jiuwenbox}"
HOST_PORT="${JIUWENBOX_HOST_PORT:-8321}"

echo "Starting jiuwenbox container:"
echo "  image:     $IMAGE_REF"
echo "  container: $CONTAINER_NAME"
echo "  url:       http://127.0.0.1:${HOST_PORT}"
echo

docker run -it \
    --name "$CONTAINER_NAME" \
    --privileged \
    -p "${HOST_PORT}:8321" \
    "$IMAGE_REF" \
    "$@"