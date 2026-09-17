#!/usr/bin/env bash
# One-shot helper: copy toolchain + patch an EXISTING compile script.
# Do not treat this file as the project's compile entrypoint; after setup,
# teams keep using their original compile_*.sh / CI command.
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "usage: $0 <skill_root> <build_dir> <compile_script>"
  echo "example: $0 jiuwenswarm/resources/agent/workspace/skills/ascend-moe-optimizer-auto-trace build/cam/comm_operator build/cam/comm_operator/compile_ascend_proj.sh"
  exit 1
fi

SKILL_ROOT="$1"
BUILD_DIR="$2"
COMPILE_SCRIPT="$3"

echo "=== Step 1: Deploy toolchain scripts ==="
python3 "${SKILL_ROOT}/scripts/bootstrap_trace_toolchain.py" --build-dir "${BUILD_DIR}"

# NOTE: The --preprocessor-cmd below is an ILLUSTRATIVE placeholder. Your real compile script
# likely uses different variable names (e.g. dirname(BASH_SOURCE) for script dir, MODULE_BUILD_PATH).
# Edit this line to match the working hook in your repo's compile_ascend_proj.sh (see UMDK example).
echo "=== Step 2: Patch compile script ==="
python3 "${SKILL_ROOT}/scripts/patch_build_pipeline.py" \
  --compile-script "${COMPILE_SCRIPT}" \
  --preprocessor-cmd "python3 \$SCRIPTS_PATH/comm_operator/trace_preprocessor.py \"./\${proj_name}\" \$BUILD_OUT_PATH/ --modify"

echo "=== Step 3: Verify scaffold ==="
python3 "${SKILL_ROOT}/scripts/verify_trace_scaffold.py" \
  --build-dir "${BUILD_DIR}" \
  --compile-script "${COMPILE_SCRIPT}"

echo "trace scaffold applied successfully"
