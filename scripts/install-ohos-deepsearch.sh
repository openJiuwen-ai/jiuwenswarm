#!/bin/sh
# Incrementally add the required DeepSearch runtime to an existing
# HarmonyOS JiuwenClaw virtual environment without reinstalling AgentCore.

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)

export CREATE_VENV=${CREATE_VENV:-1}
export RECREATE_VENV=0
export SKIP_PHASE0=1
export SKIP_PHASE1=1
export SKIP_PHASE2=1
export SKIP_PHASE3=1
export SKIP_PHASE4=1
export SKIP_DEEPSEARCH=0
# DeepSearch's pypdfium2 dependency has no OHOS wheel. The base Jiuwen
# installation already supplies the supported common dependencies.
export DEEPSEARCH_NO_DEPS=${DEEPSEARCH_NO_DEPS:-1}

exec sh "$SCRIPT_DIR/install-ohos-agentserver.sh"
