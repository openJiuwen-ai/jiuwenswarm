"""Exercise ingress authentication over a real HTTP listener and SDK routes."""

import httpx
import pytest
import time

from jiuwenswarm.common.schema.message import EventType, Message

from jiuwenswarm.gateway.channel_manager.protocol.a2a.a2a_connect import (
    A2AChannel,
    A2AChannelConfig,
)
from jiuwenswarm.gateway.channel_manager.protocol.a2a.security import hash_credential


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_type", ["bearer", "api_key"])
@pytest.mark.parametrize("protect_card", [False, True])
async def test_authentication_protects_rpc_and_card_routes(auth_type, protect_card):
    secret = "test-credential-with-32-characters"
    channel = A2AChannel(
        A2AChannelConfig(
            enabled=True,
            port=0,
            auth_type=auth_type,
            api_key_header="X-Custom-Key",
            card_auth_required=protect_card,
            credential_hash=hash_credential(secret),
        ),
        object(),
    )
    dispatched = []

    async def reply(message):
        dispatched.append(message)
        await channel.send(
            Message(
                id=message.id,
                type="event",
                channel_id="a2a",
                session_id=message.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"content": "authenticated reply", "is_complete": True},
                event_type=EventType.CHAT_FINAL,
            )
        )

    channel.on_message(reply)
    await channel.start()
    try:
        port = channel._listen_socket.getsockname()[1]
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", headers={"A2A-Version": "1.0"}
        ) as client:
            header = "Authorization" if auth_type == "bearer" else "X-Custom-Key"
            value = f"bEaReR {secret}" if auth_type == "bearer" else secret
            for method in [
                "SendMessage",
                "SendStreamingMessage",
                "GetTask",
                "CancelTask",
                "SubscribeToTask",
            ]:
                response = await client.post(
                    "/a2a",
                    json={"jsonrpc": "2.0", "id": "1", "method": method, "params": {}},
                )
                assert response.status_code == 401
                assert "www-authenticate" in response.headers
                assert secret not in response.text
            for headers in [
                {header: "wrong"},
                [(header, value), (header, value)],
                [
                    (
                        header.encode(),
                        b"Bearer \xff" if auth_type == "bearer" else b"\xff",
                    )
                ],
            ]:
                assert (
                    await client.post("/a2a", headers=headers, content=b"not json")
                ).status_code == 401
            assert (
                await client.get(channel.config.extended_card_path)
            ).status_code == 401
            assert (await client.get(channel.config.card_path)).status_code == (
                401 if protect_card else 200
            )
            response = await client.get(
                channel.config.card_path, headers={header: value}
            )
            assert response.status_code == 200
            card = response.json()
            assert secret not in response.text
            assert channel.config.credential_hash not in response.text
            assert card["securityRequirements"]
            scheme = card["securitySchemes"]["ingress"]
            if auth_type == "bearer":
                assert scheme["httpAuthSecurityScheme"]["scheme"] == "bearer"
            else:
                assert scheme["apiKeySecurityScheme"]["name"] == "X-Custom-Key"
            assert (
                await client.get(
                    channel.config.extended_card_path, headers={header: value}
                )
            ).status_code == 200
            # Correct credentials reach the SDK, even for a nonexistent task.
            response = await client.post(
                "/a2a",
                headers={header: value},
                json={
                    "jsonrpc": "2.0",
                    "id": "1",
                    "method": "GetTask",
                    "params": {"id": "missing"},
                },
            )
            assert response.status_code != 401
            assert response.json()["id"] == "1"
            assert response.json()["error"]["code"] != -32601
            assert not dispatched
            response = await client.post(
                "/a2a",
                headers={header: value},
                json={
                    "jsonrpc": "2.0",
                    "id": "stream",
                    "method": "SendStreamingMessage",
                    "params": {
                        "message": {
                            "messageId": "auth-test",
                            "role": "ROLE_USER",
                            "parts": [{"text": "hello"}],
                        }
                    },
                },
            )
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]
            assert "authenticated reply" in response.text
            assert len(dispatched) == 1
    finally:
        await channel.stop()
