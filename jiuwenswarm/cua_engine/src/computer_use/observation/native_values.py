"""Display typed native values without changing their object/value identity."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from math import floor

from ..protocol.primitives import finite


def display_native_value(platform, value, aliases):
    special_uia_double = (platform == 'windows' and isinstance(value, dict) and set(value) == {'nativeType', 'value'}
                          and value['nativeType'] == 'UIA.Double'
                          and value['value'] in ('NaN', '+Infinity', '-Infinity'))
    if special_uia_double:
        return value['value'] + ' (UIA double)'
    if platform != 'macos' or not isinstance(value, dict):
        return None
    if value.get('nativeType') == 'AXUIElement' and set(value) == {'nativeType', 'node'}:
        ref = aliases.get(value['node']) if isinstance(value['node'], str) else None
        return 'element ' + ref if ref is not None else None
    if (value.get('nativeType') == 'CFDate' and set(value) == {'nativeType', 'absoluteTime'}
            and finite(value['absoluteTime'])):
        seconds = value['absoluteTime']
        whole = floor(seconds)
        try:
            instant = datetime(2001, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=whole)
        except (OverflowError, ValueError):
            return None  # The original typed fact remains an explicit annotation.
        # datetime rounds fractions to microseconds. Preserve all digits in the
        # native double's round-trippable decimal representation instead.
        decimal = Decimal(str(seconds))
        with localcontext() as context:
            context.prec = max(28, len(str(abs(whole))) - decimal.as_tuple().exponent + 1)
            fraction = decimal - whole
        suffix = format(fraction, 'f')[1:].rstrip('0') if fraction else ''
        return instant.isoformat().removesuffix('+00:00') + suffix + 'Z'
    return None
