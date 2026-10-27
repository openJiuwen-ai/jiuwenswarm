"""Fit encoded model images to the delivery budget and retain their inverse."""

import base64
import math
from dataclasses import dataclass
from io import BytesIO

from ..protocol.png import decode_png, png_size
from ..client.image_coordinates import ImageCoordinates
from PIL import Image


@dataclass(frozen=True)
class ImageDelivery:
    """One output PNG and the exact source image whose coordinates it represents."""

    source: bytes | bytearray | memoryview
    png: bytes
    coordinates: ImageCoordinates


def prepare_image(
    png: bytes | bytearray | memoryview, *, max_dimension: int = 2048, max_pixels: int = 1536 * 1536
) -> ImageDelivery:
    """Resize once before publication; never derive a map from a nominal ratio."""
    if type(max_dimension) is not int or max_dimension <= 0 or type(max_pixels) is not int or max_pixels <= 0:
        raise ValueError("Image delivery limits must be positive integers.")
    decode_png(base64.b64encode(png).decode("ascii"))
    size = png_size(png)
    width, height = size["width"], size["height"]
    scale = min(1, max_dimension / max(width, height), math.sqrt(max_pixels / (width * height)))
    fitted = max(1, math.floor(width * scale)), max(1, math.floor(height * scale))
    if fitted == (width, height):
        encoded = memoryview(png).tobytes()
    else:
        with (
            Image.open(BytesIO(png)) as source,
            source.convert("RGBA") as rgba,
            rgba.resize(fitted, Image.Resampling.LANCZOS) as image,
        ):
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            encoded = buffer.getvalue()
    actual = png_size(encoded)
    return ImageDelivery(png, encoded, ImageCoordinates((width, height), (actual["width"], actual["height"])))
