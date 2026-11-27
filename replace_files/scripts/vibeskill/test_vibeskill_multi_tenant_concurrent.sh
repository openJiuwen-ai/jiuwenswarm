#!/bin/bash
set -euo pipefail

API_BASE="http://127.0.0.1:19003"
WS_HOST="127.0.0.1"
WS_PORT=19003

create_session() {
  local out_file="$1"
  local status
  status=$(curl -sS -o "$out_file" -w "%{http_code}" \
    -X POST "$API_BASE/api/v1/session" \
    -H "Content-Type: application/json" \
    -d '{}')
  if [[ "$status" != "200" && "$status" != "201" ]]; then
    echo "ERROR: 创建 session 失败, http=$status, body=$(cat "$out_file")"
    return 1
  fi
  python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['sessionID'])" "$out_file"
}

delete_session_best_effort() {
  local sid="$1"
  if [[ -n "${sid:-}" ]]; then
    curl -sS -X DELETE "$API_BASE/api/v1/session/$sid" >/dev/null || true
  fi
}

echo "========== 0. 预检查 =========="
if ! curl -sS --max-time 2 "$API_BASE/api/v1/session" -o /dev/null; then
  echo "ERROR: 无法访问 $API_BASE ，请先启动 HTTP 服务"
  exit 1
fi
if ! nc -z "$WS_HOST" "$WS_PORT" >/dev/null 2>&1; then
  echo "ERROR: $WS_HOST:$WS_PORT 未监听，请先启动 WebSocket 服务"
  exit 1
fi

echo "========== 1. 创建两个独立 Session =========="
SESSION_A=$(create_session /tmp/vibeskill_session_a.json)
SESSION_B=$(create_session /tmp/vibeskill_session_b.json)
echo "Session A: $SESSION_A"
echo "Session B: $SESSION_B"

if [[ -z "${SESSION_A:-}" || -z "${SESSION_B:-}" || "$SESSION_A" == "$SESSION_B" ]]; then
  echo "ERROR: session 创建结果异常"
  exit 1
fi

cleanup() {
  echo ""
  echo "========== 清理 Session =========="
  delete_session_best_effort "$SESSION_A"
  delete_session_best_effort "$SESSION_B"
  echo "cleanup done"
}
trap cleanup EXIT

echo ""
echo "========== 2. 并发发送两个请求（不同 agent_id） =========="
echo "运行: uv run python scripts/vibeskill/test_ws_multi_tenant_concurrent.py $SESSION_A $SESSION_B"
uv run python scripts/vibeskill/test_ws_multi_tenant_concurrent.py "$SESSION_A" "$SESSION_B"

echo ""
echo "========== 测试完成 =========="
