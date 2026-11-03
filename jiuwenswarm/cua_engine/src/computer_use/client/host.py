from .actions import set_action_handler
from .diagnostics import set_diagnostic_handler
from .execution import set_execution_guard
from .handles import set_handle_handler
from .observations import set_observation_handler
from .preview import set_preview_handler
from .result_presentation import set_text_page_handler

__all__ = [
    'set_action_handler', 'set_observation_handler', 'set_preview_handler', 'set_diagnostic_handler',
    'set_execution_guard', 'set_handle_handler', 'set_text_page_handler'
]
