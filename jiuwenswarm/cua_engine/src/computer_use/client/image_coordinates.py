"""An immutable transform from delivered image pixels to retained capture pixels."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .diagnostics import local_failure

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any


@dataclass(frozen=True)
class ImageCoordinates:
    """Bind actual encoded output dimensions to one original PNG's dimensions.

    Both spaces use a top-left origin and continuous image-pixel coordinates.
    Each axis uses its actual integer size after resizing, including rounding.
    """

    source_size: tuple[int, int]
    delivered_size: tuple[int, int]

    def __post_init__(self) -> None:
        if any(len(size) != 2 for size in (self.source_size, self.delivered_size)) or any(
            type(value) is not int or value <= 0 for size in (self.source_size, self.delivered_size) for value in size
        ):
            raise ValueError("Image dimensions must be positive integers.")

    def source_point(self, point: tuple[float, float]) -> tuple[float, float]:
        """Validate a delivered-image point and return retained PNG pixels."""
        if any(not math.isfinite(value) or not 0 <= value < bound for value, bound in zip(point, self.delivered_size)):
            raise local_failure(
                "coordinates are outside the delivered image",
                "choose a point inside the screenshot returned for this App/Window or desktop",
            )
        x, y = point
        return x * self.source_size[0] / self.delivered_size[0], y * self.source_size[1] / self.delivered_size[1]

    def source_parameters(self, operation: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
        """Convert pointer positions once; retain every non-coordinate parameter."""
        result = dict(parameters)
        pairs = (("fromX", "fromY"), ("toX", "toY")) if operation == "drag" else (("x", "y"),)
        for x, y in pairs:
            result[x], result[y] = self.source_point((result[x], result[y]))
        return result

    def delivered_rect(self, rect: Mapping[str, float]) -> dict[str, float]:
        """Express a source-image annotation rectangle in delivered pixels."""
        x_scale, y_scale = (delivered / source for source, delivered in zip(self.source_size, self.delivered_size))
        return {
            "x": rect["x"] * x_scale,
            "y": rect["y"] * y_scale,
            "width": rect["width"] * x_scale,
            "height": rect["height"] * y_scale,
        }
