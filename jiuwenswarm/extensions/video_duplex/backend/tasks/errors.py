"""Internal conflict types; human-readable wording is not a control protocol."""


class QueueVersionConflict(ValueError):
    """The waiting queue changed after the caller observed it."""


class TaskRevisionConflict(ValueError):
    """The task revision changed or already has a linked successor."""


class TaskNotFound(ValueError):
    """The requested invocation is absent from the authenticated conversation."""
