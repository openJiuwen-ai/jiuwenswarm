"""Publish one confirmed application through the existing WebChannel protocol."""
import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit
import uuid

from jiuwenswarm.channels.cli.gateway_client import GatewayClient


async def publish(entry, session_id, gateway_url):
    parsed = urlsplit(gateway_url)
    if parsed.scheme not in {"ws", "wss"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Use the current local WebChannel Gateway URL")
    client = GatewayClient(gateway_url)
    try:
        await asyncio.wait_for(client.connect(), 15)
        request_id = "app-publish-" + uuid.uuid4().hex
        await client.send_request({"type": "req", "id": request_id, "method": "app_market.publish", "params": {"entry_path": str(Path(entry).resolve()), "source_session": session_id}})
        async def result():
            while True:
                response = await client.recv()
                if response.get("id") == request_id and response.get("type") == "res":
                    if not response.get("ok"):
                        raise RuntimeError(str(response.get("error") or "Application publish failed"))
                    return response.get("payload", {})
        return await asyncio.wait_for(result(), 60)
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--gateway-url", required=True, help="Actual WebChannel URL, e.g. ws://127.0.0.1:<web-port>/ws")
    parser.add_argument("--confirmed", action="store_true", help="User has authorized this application publication")
    args = parser.parse_args()
    if not args.confirmed:
        parser.error("Obtain confirmation through the existing conversation before publishing")
    print(json.dumps(asyncio.run(publish(args.entry, args.session_id, args.gateway_url)), ensure_ascii=False))


if __name__ == "__main__":
    main()
