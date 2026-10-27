"""The market static host exposes lifecycle status, never AI requests."""
import threading
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import pytest
from jiuwenswarm.extensions.app_market.app_market.static_server import make_handler


def test_static_host_has_no_ai_client_or_forwarding_endpoint(tmp_path):
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(
        {'app_id':'a','instance_id':'i','work_dir':str(tmp_path)}, 'secret'))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    try:
        for request in [url+'/_workswarm/client.js', Request(url+'/_workswarm/api', data=b'{}')]:
            with pytest.raises(HTTPError) as error:
                urlopen(request)
            assert error.value.code == 404
        request = Request(url+'/_workswarm/status', headers={'Authorization':'Bearer secret'})
        assert urlopen(request).status == 200
    finally:
        server.shutdown(); server.server_close(); thread.join()
