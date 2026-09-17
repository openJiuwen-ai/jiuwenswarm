from __future__ import annotations

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
    from rich.tree import Tree as RichTree
except ModuleNotFoundError:

    class Console:
        @staticmethod
        def print(*args, **kwargs) -> None:
            return None

    class Panel:
        def __init__(self, renderable=None, *args, **kwargs) -> None:
            self.renderable = renderable

        @classmethod
        def fit(cls, renderable=None, *args, **kwargs):
            return cls(renderable, *args, **kwargs)

    class Progress:
        def __init__(self, *args, **kwargs) -> None:
            self._next_task_id = 0

        def __enter__(self):
            return self

        @staticmethod
        def __exit__(exc_type, exc, tb) -> None:
            return None

        def add_task(self, *args, **kwargs) -> int:
            task_id = self._next_task_id
            self._next_task_id += 1
            return task_id

        @staticmethod
        def update(*args, **kwargs) -> None:
            return None

    class SpinnerColumn:
        def __init__(self, *args, **kwargs) -> None:
            return None

    class TextColumn:
        def __init__(self, *args, **kwargs) -> None:
            return None

    class BarColumn:
        def __init__(self, *args, **kwargs) -> None:
            return None

    class TaskProgressColumn:
        def __init__(self, *args, **kwargs) -> None:
            return None

    class RichTree:
        def __init__(self, label, *args, **kwargs) -> None:
            self.label = label
            self.children = []

        def add(self, label, *args, **kwargs):
            child = RichTree(label, *args, **kwargs)
            self.children.append(child)
            return child


__all__ = [
    "BarColumn",
    "Console",
    "Panel",
    "Progress",
    "RichTree",
    "SpinnerColumn",
    "TaskProgressColumn",
    "TextColumn",
]
