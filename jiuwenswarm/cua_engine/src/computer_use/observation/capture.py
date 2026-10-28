"""Explicit window pixels, independent of accessibility nodes and tree rendering."""
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from ..execution.native import Binding
from ..protocol.captures import capture_metadata
from ..protocol.png import PNG
from ..protocol.primitives import frozen


@dataclass(frozen=True)
class WindowImage:
    target: Binding
    image: PNG
    observed: Mapping[str, Any]
    coordinates: Mapping[str, Any]


class NativeImages:

    def __init__(self, port):
        self.port = port

    async def window(self, target, *, max_dimension=2048, timeout_ms=30000):
        if target.instance is None or target.surface is None:
            raise ValueError('Window capture requires an observed working-window binding')
        if type(max_dimension) is not int or not 1 <= max_dimension <= 4096:
            raise ValueError('Image dimension budget must be an integer in 1..4096')
        # A new observation creates its own image binding. It never updates an
        # existing Element, capture, or operation's coordinate target in place.
        target = replace(target, node=None, capture=None)
        async with self.port.calls.open(target, timeout_ms) as read:
            return await self.collect(read, max_dimension=max_dimension)

    async def collect(self, read, *, max_dimension=2048):
        """Capture the exact window of an existing window read operation."""
        target = read.target
        invalid_capture_scope = (
            target.instance is None or target.surface is None or target.node is not None or target.capture is not None
        )
        if invalid_capture_scope:
            raise ValueError('Image collection requires an un-narrowed window read operation')
        if type(max_dimension) is not int or not 1 <= max_dimension <= 4096:
            raise ValueError('Image dimension budget must be an integer in 1..4096')
        result = await self.port.capture(read.token, target, max_dimension, read.remaining())
        image = result['image']
        value = WindowImage(replace(target, capture=capture_metadata(image)['id']), image, result['observed'],
                            frozen(result['coordinates']))
        read.remaining()
        return value
