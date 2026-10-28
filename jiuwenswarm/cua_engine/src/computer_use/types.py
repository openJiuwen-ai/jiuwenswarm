"""Public structural result types and dictionary options; no native identities."""
from typing import Literal, Protocol, TypeAlias, TypedDict

ScrollDirection: TypeAlias = Literal["up", "down", "left", "right"]


class Observation(Protocol):

    @property
    def text(self) -> str:
        ...

    @property
    def details(self) -> str:
        ...

    @property
    def screenshot(self) -> bytearray | None:
        ...

    @property
    def overview(self) -> bytearray | None:
        ...


class AppInfo(Protocol):

    @property
    def id(self) -> str:
        ...

    @property
    # Retain the established Agent API spelling.
    # pylint: disable-next=huawei-invalid-name
    def displayName(self) -> str:
        ...

    @property
    # Retain the established Agent API spelling.
    # pylint: disable-next=huawei-invalid-name
    def isRunning(self) -> bool:
        ...


class ActionResult(Protocol):

    @property
    def status(self) -> Literal["dispatched", "verified"]:
        ...

    @property
    def detail(self) -> str:
        ...


class TextPage(Protocol):

    @property
    def text(self) -> str:
        ...

    @property
    def offset(self) -> int:
        ...

    @property
    def total(self) -> int:
        ...

    @property
    def next(self) -> int | None:
        ...


class ClickOptions(TypedDict, total=False):
    button: Literal["left", "right", "middle"]
    count: Literal[1, 2]


class DragOptions(TypedDict):
    fromX: float
    fromY: float
    toX: float
    toY: float


class TypeTextOptions(TypedDict, total=False):
    replace: bool


class ElementMatch(TypedDict, total=False):
    role: str
    name: str
    identifier: str


ElementPath: TypeAlias = list[str | ElementMatch]


class ReadOptions(TypedDict, total=False):
    full: bool
    menuPath: list[str]


class DesktopReadOptions(TypedDict, total=False):
    full: bool


class ElementReadOptions(TypedDict, total=False):
    offset: int
    cursor: str


class ReadTextOptions(TypedDict, total=False):
    offset: int
    length: int


class FindOptions(TypedDict, total=False):
    timeoutMs: int
