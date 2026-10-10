"""Classification failures, kept separate from successful delivery decisions."""


class JevError(Exception):
    """Base class for failures handled by the integration's fallback policy."""


class InvalidInputError(JevError):
    """Input has an invalid type or empty message."""


class InsufficientContextError(InvalidInputError):
    """The supplied context is empty."""


class InputTooLargeError(InvalidInputError):
    """Input exceeds the configured limit; no content was truncated."""


class InvalidResponseError(JevError):
    """Provider response does not satisfy the decision contract."""


class JevTimeoutError(JevError):
    """The classification deadline or transport timeout expired."""


class ProviderError(JevError):
    """The provider or HTTP transport failed."""

