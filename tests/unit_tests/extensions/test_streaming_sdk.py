"""The streaming SDK keeps the host session type."""

from jiuwenswarm.extensions.sdk.streaming import StreamingSession
from jiuwenswarm.gateway.channel_manager.im_platforms.platform_adapter.streaming_session import (
    StreamingSession as HostStreamingSession,
)


def test_streaming_session_is_host_class():
    assert StreamingSession is HostStreamingSession
