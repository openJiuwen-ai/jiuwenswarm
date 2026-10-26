from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .types import Message


def join_messages(messages: Sequence[Message]) -> str:
    parts: list[str] = []
    for message in messages:
        role = str(message.get("role") or "").strip()
        content = str(message.get("content") or "")
        parts.append(f"{role}: {content}".strip())
    return "\n".join(parts).strip()


@dataclass
class CandidateCodeTokenizer:
    tokenizer: object
    _cache: dict[tuple[str, str], int | None] = field(default_factory=dict)

    @classmethod
    def from_pretrained(cls, path: str) -> "CandidateCodeTokenizer":
        try:
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise RuntimeError("transformers is required for candidate code tokenization") from exc
        return cls(tokenizer=AutoTokenizer.from_pretrained(path, trust_remote_code=True))

    def render_generation_prefix(self, messages: Sequence[Message]) -> str | None:
        tokenizer = self.tokenizer
        if not hasattr(tokenizer, "apply_chat_template"):
            return None
        return str(
            tokenizer.apply_chat_template(
                list(messages),
                add_generation_prompt=True,
                tokenize=False,
                enable_thinking=False,
                preserve_thinking=False,
                add_vision_id=False,
            )
        )

    def encode_text(self, text: str) -> list[int]:
        return list(self.tokenizer.encode(text, add_special_tokens=False))

    def encode_single_token(self, code: str, *, messages: Sequence[Message] | None = None) -> int | None:
        text = str(code or "")
        prefix = self.render_generation_prefix(messages) if messages else None
        cache_key = (prefix or "", text)
        if cache_key in self._cache:
            return self._cache[cache_key]
        token_ids = self.encode_text(text)
        value = int(token_ids[0]) if len(token_ids) == 1 else None
        if value is not None and prefix:
            prefix_ids = self.encode_text(prefix)
            output_suffixes = ("", "\n", "\nAA")
            for suffix in output_suffixes:
                suffix_ids = self.encode_text(suffix)
                combined_ids = self.encode_text(prefix + text + suffix)
                if combined_ids != prefix_ids + token_ids + suffix_ids:
                    value = None
                    break
        self._cache[cache_key] = value
        return value

    def encode_many(self, codes: Sequence[str], *, messages: Sequence[Message] | None = None) -> dict[str, int | None]:
        return {str(code): self.encode_single_token(str(code), messages=messages) for code in codes}


__all__ = ["CandidateCodeTokenizer", "join_messages"]
