import asyncio
import sys
import websockets

async def check_ws_ready():
    port = sys.argv[1] if len(sys.argv) > 1 else "18092"
    url = f"ws://127.0.0.1:{port}/?K8s-Readiness-Probe"
    try:
        async with websockets.connect(url, open_timeout=2.0) as ws:
            await asyncio.wait_for(ws.recv(), timeout=2.0)
            sys.exit(0)
    except Exception as e:
        print(f"[Probe Failed] WebSocket 探测失败: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(check_ws_ready())

