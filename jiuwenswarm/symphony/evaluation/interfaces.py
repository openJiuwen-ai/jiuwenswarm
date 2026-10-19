"""Protocols for evaluation module integrations."""

from typing import Any, Dict, List, Protocol, Sequence, Union


PromptInput = Union[str, Sequence[str]]
Message = Dict[str, str]
MessageInput = Union[Sequence[Message], Sequence[Sequence[Message]]]


class EvaluationLLM(Protocol):
    """Minimal model interface required by Symphony evaluation."""

    def generate(self, prompts: PromptInput, **kwargs: Any) -> List[str]:
        """Generate text for one or more prompts."""

    def chat(self, messages: MessageInput, **kwargs: Any) -> List[str]:
        """Generate text for one or more chat conversations."""
