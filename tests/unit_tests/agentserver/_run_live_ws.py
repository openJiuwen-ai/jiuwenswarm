# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tests.unit_tests.agentserver.live_ws_harness import LiveWsHarness

HARNESS_DIR = (
    ROOT / "jiuwenswarm" / "channels" / "web" / "frontend" / "tests" / "live_ws"
)
RESULT = HARNESS_DIR / "snapshot.json"


def main() -> int:
    harness = LiveWsHarness(HARNESS_DIR, session_id="session-h")
    harness.start()
    print(f"LIVE_WS_URL={harness.http_url}", flush=True)
    try:
        snapshot = harness.wait_snapshot(timeout=60.0)
        RESULT.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        print(f"LIVE_WS_SNAPSHOT={RESULT}", flush=True)
        print(json.dumps(snapshot), flush=True)
        return 0
    finally:
        harness.close()


if __name__ == "__main__":
    raise SystemExit(main())
