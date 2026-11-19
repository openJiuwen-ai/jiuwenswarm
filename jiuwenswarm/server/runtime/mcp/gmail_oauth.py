"""Local Google OAuth files used by the Gmail stdio connector."""

import hashlib
import json
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import httpx

from jiuwenswarm.common.utils import get_workspace_dir
from jiuwenswarm.server.runtime.mcp.credential import CredentialStore

CLIENT_PATH_KEY = "GMAIL_OAUTH_PATH"
PROXY_PORT_KEY = "GMAIL_PROXY_PORT"
_PROXY_ENV_KEYS = {"http_proxy", "https_proxy", "no_proxy"}
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_URL = "https://gmail.googleapis.com/"
SERVER_ARGS = ["-y", "@artymclabin/gmail-mcp@1.2.4"]
SCOPES = ("https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send")


class GmailNetworkError(ValueError):
    """Google authorization could not be checked from the current network."""


def is_gmail_oauth_package(package) -> bool:
    return bool(
        package and package.integration_type == "stdio-mcp"
        and package.credentials_type == "token"
        and (package.server_config or {}).get("command") == "npx"
        and (package.server_config or {}).get("args") == SERVER_ARGS
        and (package.server_config or {}).get("env", {}).get(CLIENT_PATH_KEY) == "${GMAIL_OAUTH_PATH}"
    )


def default_client_path() -> Path:
    return Path.home() / ".gmail-mcp" / "gcp-oauth.keys.json"


def parse_proxy_port(value: str) -> str:
    port = value.strip()
    if not port:
        return ""
    if not (port.isascii() and port.isdecimal() and len(port) <= 5 and 1 <= int(port) <= 65535):
        raise ValueError("Enter a local proxy port between 1 and 65535, or leave it blank.")
    return str(int(port))


def configured_proxy_port(name: str) -> str | None:
    port = CredentialStore().get_token(name, PROXY_PORT_KEY)
    if port is not None:
        return port
    # Migrate the locally configured HTTP proxy into the displayed setting.
    from jiuwenswarm.server.runtime.mcp.registry import resolve_package

    package = resolve_package(name)
    if not is_gmail_oauth_package(package):
        return None
    return proxy_port_in_env(package.server_config.get("env", {}))


def proxy_port_in_env(env: dict) -> str | None:
    url = env.get("HTTPS_PROXY") or env.get("https_proxy") or ""
    match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})/?", str(url))
    if match and 1 <= int(match[1]) <= 65535:
        return match[1]
    return None


def proxy_url(port: str | None) -> str | None:
    parsed = parse_proxy_port(port or "")
    return f"http://127.0.0.1:{parsed}" if parsed else None


def apply_proxy_env(env: dict, port: str | None) -> dict:
    if port is None:
        return dict(env)
    result = {key: value for key, value in env.items() if key.lower() not in _PROXY_ENV_KEYS}
    url = proxy_url(port)
    if url:
        result.update(HTTP_PROXY=url, HTTPS_PROXY=url, NO_PROXY="localhost,127.0.0.1")
    return result


@lru_cache(maxsize=8)
def _probe_direct(url: str, time_window: int) -> bool:
    try:
        httpx.head(url, timeout=3, trust_env=False)
    except httpx.RequestError:
        return False
    return True


def direct_reachable(url: str) -> bool:
    """Probe Google without credentials or inherited proxy settings."""
    return _probe_direct(url, int(time.monotonic()) // 30)


def oauth_proxy(port: str | None) -> tuple[str | None, bool]:
    """Return the fallback proxy and whether to bypass inherited proxies."""
    url = proxy_url(port)
    if not url:
        return None, False
    if direct_reachable(TOKEN_URL):
        return None, True
    return url, False


def stdio_proxy_env(env: dict) -> dict:
    """Resolve Gmail routing on a connection or proxy update worker thread."""
    port = proxy_port_in_env(env)
    if not port:
        return env
    with ThreadPoolExecutor(max_workers=2) as pool:
        token_ok, gmail_ok = pool.map(direct_reachable, (TOKEN_URL, GMAIL_URL))
    bypass = ["localhost", "127.0.0.1"]
    if token_ok:
        bypass.append("oauth2.googleapis.com")
    if gmail_ok:
        bypass.append("gmail.googleapis.com")
    # Keep the configured proxy only when a Google endpoint needs it.
    result = apply_proxy_env(env, "") if token_ok and gmail_ok else dict(env)
    return {**result, "NO_PROXY": ",".join(bypass)}


def paths(name: str) -> tuple[Path, Path]:
    default = default_client_path()
    store = CredentialStore()
    env = store.get_all(name)
    raw = (env.get(CLIENT_PATH_KEY) or os.environ.get(CLIENT_PATH_KEY) or "").strip()
    if not raw:
        return paths_for_default(default)
    client = Path(raw)
    if not client.is_absolute():
        raise ValueError("Enter the absolute path to the Google OAuth client JSON file.")
    client = client.resolve()
    if client == default.resolve():
        return paths_for_default(default)
    digest = hashlib.sha256(os.fsencode(client)).hexdigest()
    return client, get_workspace_dir() / "mcp" / "gmail-oauth" / digest / "credentials.json"


def paths_for_default(client: Path) -> tuple[Path, Path]:
    return client, Path(os.environ.get("GMAIL_CREDENTIALS_PATH") or client.with_name("credentials.json"))


def read_client(path: Path) -> tuple[str, str]:
    try:
        if not path.is_file() or path.stat().st_size > 64 * 1024:
            raise ValueError
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        client = data["installed"]
        client_id, secret = client["client_id"], client["client_secret"]
        if not isinstance(client_id, str) or not client_id.endswith(".apps.googleusercontent.com"):
            raise ValueError
        if not isinstance(secret, str) or not secret:
            raise ValueError
        return client_id, secret
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("Select a readable Google Cloud Desktop app OAuth client JSON file.") from exc


def read_tokens(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tokens = data["tokens"]
        scopes = {scope.rsplit("/", 1)[-1] for scope in data["scopes"]}
        if isinstance(tokens, dict) and {"gmail.readonly", "gmail.send"} <= scopes:
            return tokens
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return {}


def write_tokens(path: Path, body: dict, *, previous: dict | None = None) -> None:
    tokens = {**(previous or {}), **body}
    tokens["expiry_date"] = int((time.time() + int(body["expires_in"])) * 1000)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8") as stream:
        temporary = Path(stream.name)
        try:
            json.dump({"tokens": tokens, "scopes": [scope.rsplit("/", 1)[-1] for scope in SCOPES]}, stream)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def has_valid_authorization(client_path: Path, token_path: Path, *, proxy_port: str | None = None) -> bool:
    tokens = read_tokens(token_path)
    if not tokens.get("refresh_token"):
        return False
    client_id, secret = read_client(client_path)
    proxy, direct = oauth_proxy(proxy_port)
    try:
        options = {"proxy": proxy} if proxy else {}
        if direct:
            options["trust_env"] = False
        response = httpx.post(TOKEN_URL, timeout=httpx.Timeout(15, connect=4), data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client_id,
            "client_secret": secret,
        }, **options)
    except httpx.RequestError:
        raise GmailNetworkError("Could not reach Google to verify Gmail authorization.") from None
    if response.status_code == 400:
        try:
            if response.json().get("error") == "invalid_grant":
                return False
        except (ValueError, AttributeError):
            pass
    if not response.is_success:
        raise ValueError(f"Google could not verify Gmail authorization (HTTP {response.status_code}).")
    body = response.json()
    if not body.get("access_token") or not body.get("expires_in"):
        raise ValueError("Google returned an incomplete Gmail refresh response.")
    if body.get("scope") and not set(SCOPES) <= set(body["scope"].split()):
        return False
    write_tokens(token_path, body, previous=tokens)
    return True
