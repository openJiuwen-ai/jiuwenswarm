from .errors import ComputerUseError as ComputerUseError
from .types import (
    ActionResult as ActionResult,
)
from .types import (
    AppInfo as AppInfo,
)
from .types import (
    ClickOptions as ClickOptions,
)
from .types import (
    DesktopReadOptions as DesktopReadOptions,
)
from .types import (
    DragOptions as DragOptions,
)
from .types import (
    ElementMatch as ElementMatch,
)
from .types import (
    ElementPath as ElementPath,
)
from .types import (
    ElementReadOptions as ElementReadOptions,
)
from .types import (
    FindOptions as FindOptions,
)
from .types import (
    Observation as Observation,
)
from .types import (
    ReadOptions as ReadOptions,
)
from .types import (
    ReadTextOptions as ReadTextOptions,
)
from .types import (
    ScrollDirection as ScrollDirection,
)
from .types import (
    TextPage as TextPage,
)
from .types import (
    TypeTextOptions as TypeTextOptions,
)

__version__: str

class Computer:
    """The connected desktop session. Native driver starts on the first system call."""
    def __init__(self) -> None: ...
    def app(self, selector: str) -> App:
        """Bind a working window, or prepare an installed app that the first read may launch."""
        ...
    async def listApps(self) -> list[AppInfo]: ...
    async def read(self, options: DesktopReadOptions = ...) -> Observation: ...
    async def click(self, x: float, y: float, options: ClickOptions = ...) -> ActionResult: ...
    async def scroll(self, x: float, y: float, direction: ScrollDirection, pages: float = 1) -> ActionResult: ...
    async def drag(self, options: DragOptions) -> ActionResult: ...
    async def typeText(self, text: str) -> ActionResult: ...
    async def pressKey(self, key: str) -> ActionResult: ...

class App:
    """One working window and its confirmed interactions. Use computer.app(selector)."""
    def window(self, ref: str) -> App:
        """Select a fixed view from this App's observed window catalogue."""
        ...
    async def read(self, options: ReadOptions = ...) -> Observation:
        """Read full bounded state. full=True forces full model presentation; menuPath reads a named menu."""
        ...
    def element(self, ref: str) -> Element: ...
    def find(self, path: ElementPath, options: FindOptions = ...) -> Element: ...
    async def click(self, x: float, y: float, options: ClickOptions = ...) -> ActionResult: ...
    async def typeText(self, text: str) -> ActionResult: ...
    async def paste(self, text: str) -> ActionResult:
        """Insert plain text at the current selection; tabs and line breaks are allowed."""
        ...
    async def scroll(self, x: float, y: float, direction: ScrollDirection, pages: float = 1) -> ActionResult: ...
    async def drag(self, options: DragOptions) -> ActionResult: ...
    async def pressKey(self, key: str) -> ActionResult: ...

class Element:
    """Observed native target, or a semantic query resolved within its App."""
    async def read(self, options: ElementReadOptions = ...) -> Observation: ...
    async def readText(self, options: ReadTextOptions = ...) -> TextPage:
        """Page the exact captured text. Offsets and lengths are UTF-16 code units."""
        ...
    async def click(self, options: ClickOptions = ...) -> ActionResult: ...
    async def openMenu(self) -> ActionResult: ...
    async def performAction(self, action: str) -> ActionResult:
        """Execute an exact Secondary Actions name reported on this element."""
        ...
    async def pressKey(self, key: str) -> ActionResult: ...
    async def focus(self) -> ActionResult: ...
    async def scroll(self, direction: ScrollDirection, pages: float = 1) -> ActionResult: ...
    async def typeText(self, text: str, options: TypeTextOptions = ...) -> ActionResult:
        """Insert at the caret/selection; replace=True replaces the entire input, without submitting."""
        ...
    async def paste(self, text: str) -> ActionResult: ...
