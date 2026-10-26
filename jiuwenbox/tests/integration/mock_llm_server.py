"""Mock LLM server for testing inference privacy proxy.

Simulates an OpenAI-like API endpoint that accepts chat completion requests.
Supports both HTTP and HTTPS simultaneously on different ports.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import ipaddress
import json
import logging
import ssl
import tempfile
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def handle_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, protocol: str = "HTTP") -> None:
    """Handle incoming HTTP request and return mock LLM response."""
    try:
        request_data = await reader.read(8192)
        if not request_data:
            return

        request_text = request_data.decode(errors="replace")
        logger.info("[%s] Received request:\n%s", protocol, request_text[:500])

        request_json = {}
        extracted_api_key = None
        auth_header = None
        
        try:
            if request_text.startswith("POST") or request_text.startswith("GET"):
                headers_end = request_text.find("\r\n\r\n")
                if headers_end > 0:
                    headers_section = request_text[:headers_end]
                    body = request_text[headers_end + 4:]
                    
                    for line in headers_section.split("\r\n"):
                        if line.lower().startswith("authorization:"):
                            auth_header = line
                            if "bearer " in line.lower():
                                extracted_api_key = line.split("Bearer ")[-1].strip()
                        elif line.lower().startswith("x-api-key:"):
                            extracted_api_key = line.split(":")[-1].strip()
                    
                    if body:
                        request_json = json.loads(body)
                        logger.info("[%s] Parsed JSON body: %s", protocol, request_json)
            else:
                request_json = json.loads(request_text)
                logger.info("[%s] Parsed raw JSON: %s", protocol, request_json)
        except json.JSONDecodeError:
            pass

        model = request_json.get("model", "no model")

        if extracted_api_key and len(extracted_api_key) > 15:
            key_display = extracted_api_key[:15] + "..."
        else:
            key_display = extracted_api_key or "no-key"

        response = {
            "id": "mock-chatcmpl-123",
            "object": "chat.completion",
            "created": 1234567890,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": f"Mock response from '{model}'. Injected API key: {key_display}",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
            "system_fingerprint": "fp_mock_test",
            "endpoint": f"{protocol.lower()}://api.mock-llm.local/v1/chat/completions",
            "injected_api_key": extracted_api_key,
            "auth_header": auth_header,
        }

        response_json = json.dumps(response)
        response_http = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(response_json)}\r\n"
            "Connection: close\r\n"
            "\r\n"
            f"{response_json}"
        )

        writer.write(response_http.encode())
        await writer.drain()
        logger.info("[%s] Sent response for model: '%s'", protocol, model)

    except Exception as e:
        logger.exception("[%s] Error handling request: %s", protocol, e)
        error_response = json.dumps({"error": str(e)})
        writer.write(
            (
                "HTTP/1.1 500 Internal Server Error\r\n"
                "Content-Type: application/json\r\n"
                "\r\n"
                f"{error_response}"
            ).encode()
        )
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


def generate_self_signed_cert() -> tuple[Path, Path]:
    """Generate a self-signed certificate for HTTPS testing."""
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError as exc:
        raise ImportError("cryptography library required for HTTPS mode") from exc

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "mock-llm.local"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Mock LLM Test"),
    ])

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("mock-llm.local"),
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_dir = Path(tempfile.mkdtemp(prefix="mock_llm_cert_"))
    cert_path = cert_dir / "mock_llm.crt"
    key_path = cert_dir / "mock_llm.key"

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))

    logger.info("Generated self-signed cert: %s", cert_path)
    logger.info("Generated self-signed key: %s", key_path)

    return cert_path, key_path


async def start_http_server(port: int) -> asyncio.Server:
    """Start HTTP server."""
    server = await asyncio.start_server(
        lambda r, w: handle_request(r, w, "HTTP"),
        "0.0.0.0",
        port,
    )
    addr = server.sockets[0].getsockname()
    logger.info("HTTP server listening on http://%s:%d", addr[0], addr[1])
    return server


async def start_https_server(port: int) -> asyncio.Server:
    """Start HTTPS server with self-signed certificate."""
    cert_path, key_path = generate_self_signed_cert()
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(str(cert_path), str(key_path))

    server = await asyncio.start_server(
        lambda r, w: handle_request(r, w, "HTTPS"),
        "0.0.0.0",
        port,
        ssl=ssl_ctx,
    )
    addr = server.sockets[0].getsockname()
    logger.info("HTTPS server listening on https://%s:%d", addr[0], addr[1])
    return server


async def main(http_port: int, https_port: int | None) -> None:
    """Start mock LLM servers."""
    servers = []

    http_server = await start_http_server(http_port)
    servers.append(http_server)

    if https_port:
        https_server = await start_https_server(https_port)
        servers.append(https_server)

    logger.info("Mock LLM server ready. Connect via inference_privacy_proxy.")
    logger.info("HTTP endpoint: http://127.0.0.1:%d", http_port)
    if https_port:
        logger.info("HTTPS endpoint: https://127.0.0.1:%d", https_port)

    async def serve_forever(server: asyncio.Server) -> None:
        async with server:
            await server.serve_forever()

    tasks = [asyncio.create_task(serve_forever(s)) for s in servers]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mock LLM server for testing inference_privacy_proxy")
    parser.add_argument("--http-port", type=int, default=9999, help="HTTP port (default: 9999)")
    parser.add_argument("--https-port", type=int, default=None, help="HTTPS port (default: None, set to enable HTTPS)")
    args = parser.parse_args()

    asyncio.run(main(args.http_port, args.https_port))
