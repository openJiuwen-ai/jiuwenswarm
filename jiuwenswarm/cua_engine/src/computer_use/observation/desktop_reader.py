"""Desktop observation over v5: no window-subtree walk, launch or input preparation."""
from ..execution.native import Binding
from .desktop import renderDesktop
from .formatting import jsonString
from ..protocol.captures import captureMetadata


class NativeDesktopReader:
    def __init__(self, port):
        self.port = port

    async def read(self, *, max_dimension=4096, timeout_ms=30000):
        if type(max_dimension) is not int or not 1 <= max_dimension <= 4096:
            raise ValueError('Image dimension budget must be an integer in 1..4096')
        async with self.port.calls.open(None, timeout_ms) as read:
            if read.target != Binding(read.target.session, None, None, None):
                raise ValueError('Desktop observation requires this session\'s read scope')
            result = await self.port.capture(read.token, read.target, max_dimension, read.remaining(), kind='desktop')
            read.remaining()
            text = renderDesktop(result['observation'])
            if result['screenshot'] is not None:
                text += ''.join('\nImage note: ' + jsonString(reason)
                    for reason in captureMetadata(result['screenshot']).get('stopReasons', ()))
            value = {**result, 'text': text}
            read.remaining()
            return value
