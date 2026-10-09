"""OpenAI GA Realtime configuration. The browser only receives a local relay URL."""

from dataclasses import dataclass
import os
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

DEFAULT_MODEL = "gpt-realtime-2.1-mini"
DEFAULT_URL = "wss://api.openai.com/v1/realtime"


@dataclass(frozen=True)
class OpenAIRealtimeConfig:
    upstream_url: str
    api_key: str
    model: str
    voice: str

    @classmethod
    def from_environment(cls) -> "OpenAIRealtimeConfig":
        return cls(
            upstream_url=os.getenv("OPENAI_REALTIME_URL", "").strip() or DEFAULT_URL,
            api_key=(os.getenv("OPENAI_REALTIME_API_KEY", "").strip()
                     or os.getenv("OPENAI_API_KEY", "").strip()),
            model=os.getenv("OPENAI_REALTIME_MODEL", "").strip() or DEFAULT_MODEL,
            voice=os.getenv("OPENAI_REALTIME_VOICE", "").strip() or "marin",
        )

    def validate(self) -> None:
        parsed = urlsplit(self.upstream_url)
        if parsed.scheme != "wss" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("OPENAI_REALTIME_URL must be a wss:// URL without credentials or fragment")
        if not self.api_key:
            raise ValueError("Configure OPENAI_REALTIME_API_KEY or OPENAI_API_KEY")

    def upstream_with_model(self) -> str:
        self.validate()
        parsed = urlsplit(self.upstream_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["model"] = self.model
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
