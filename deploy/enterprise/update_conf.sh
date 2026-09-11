#!/bin/bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

SRC_FILE=${PROJECT_DIR}/jiuwenswarm/resources/config.yaml
DEST_FILE=${1:-${PROJECT_DIR}/deploy/enterprise/templates/gateway-config.template.yaml}

cp -f "${SRC_FILE}" "${DEST_FILE}"

yq eval '.gateway.agent_client.type = "jiuwen"' -i "${DEST_FILE}"
yq eval '.gateway.edition = "enterprise"' -i "${DEST_FILE}"
yq eval '.gateway.session_map_scope = "<<GATEWAY_SESSION_MAP_SCOPE>>"' -i "${DEST_FILE}"
yq eval '.channels.feishu = {}' -i "${DEST_FILE}"
yq eval '.react.max_iterations = "<<AGENT_SERVER_REACT_MAX_ITER>>"' -i "${DEST_FILE}"
yq eval '.react.evolution.enabled = false' -i "${DEST_FILE}"
yq eval '.sandbox.enabled = true' -i "${DEST_FILE}"
yq eval '.sandbox.startup_mode = "external"' -i "${DEST_FILE}"
yq eval '.sandbox.url = "http://127.0.0.1:<<JIUWENBOX_PORT>>"' -i "${DEST_FILE}"
yq eval '.sandbox.type = "jiuwenbox"' -i "${DEST_FILE}"
