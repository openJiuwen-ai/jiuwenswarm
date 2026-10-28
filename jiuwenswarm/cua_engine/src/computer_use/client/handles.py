"""Host-only handle lifecycle events; metadata never grants input authority.

Observers may retain evidence for a handle, but must keep the handle weakly.
The controller remains responsible for validating the original opaque binding.
"""

_handler = None


def set_handle_handler(handler):
    global _handler
    _handler = handler


def handle_created(handle, metadata):
    if _handler is not None:
        _handler(handle, metadata)
