#!/usr/bin/env bash
# Point the current JiuwenSwarm environment at a local agent-core checkout.
# Does not change pyproject.toml / uv.lock (those stay on the published git source).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
AGENT_CORE="${AGENT_CORE_ROOT:-$(cd "$ROOT/../agent-core" && pwd)}"

if [[ ! -d "$AGENT_CORE/openjiuwen" ]]; then
  echo "local agent-core not found: $AGENT_CORE" >&2
  echo "Set AGENT_CORE_ROOT to the checkout that contains openjiuwen/" >&2
  exit 1
fi

cd "$ROOT"
echo "editable-install: $AGENT_CORE" >&2
uv pip install -e "${AGENT_CORE}[claude,codex,code-graph]"

# `uv run` re-syncs from the lockfile and would restore the published git package.
PYTHON="${ROOT}/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "expected venv python at $PYTHON" >&2
  exit 1
fi
"$PYTHON" -c "import openjiuwen, openjiuwen.core.retrieval.code_graph as g; print('openjiuwen', openjiuwen.__file__); print('code_graph', g.__file__)"
echo >&2
echo "Keep this checkout: do not run plain \`uv run\` / \`uv sync\` afterwards." >&2
echo "They reinstall published openjiuwen from uv.lock. Use one of:" >&2
echo "  UV_NO_SYNC=1 uv run python ..." >&2
echo "  $PYTHON ..." >&2
echo "Eval scripts (scripts/eval/run_*.py) prepend ../agent-core even after a sync." >&2
