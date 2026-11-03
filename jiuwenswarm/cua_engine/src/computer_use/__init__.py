"""Computer Use's Python implementation.

The native wire protocol and desktop semantics are unchanged. SDK, observation,
MCP and Agent hosts run in Python; native platform drivers retain their languages.
"""

__version__ = "0.0.1"

from .client.computer import App, Computer, Element
from .errors import ComputerUseError
from .types import (
                    ActionResult,
                    AppInfo,
                    ClickOptions,
                    DesktopReadOptions,
                    DragOptions,
                    ElementMatch,
                    ElementPath,
                    ElementReadOptions,
                    FindOptions,
                    Observation,
                    ReadOptions,
                    ReadTextOptions,
                    ScrollDirection,
                    TextPage,
                    TypeTextOptions,
)

__all__ = [
    'Computer', 'App', 'Element', 'ComputerUseError', 'Observation', 'AppInfo', 'ActionResult', 'TextPage',
    'ScrollDirection', 'ClickOptions', 'DragOptions', 'TypeTextOptions', 'ElementMatch', 'ElementPath', 'ReadOptions',
    'DesktopReadOptions', 'ElementReadOptions', 'ReadTextOptions', 'FindOptions'
]
