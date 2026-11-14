"""Static Web forwards remote file bytes and credentials to the Gateway."""
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from jiuwenswarm.channels.web import app_web


@pytest.mark.parametrize('backend', ['agentos_router', 'adx_router'])
@pytest.mark.parametrize('method', ['GET', 'HEAD', 'POST'])
@pytest.mark.parametrize('status', [200, 403])
def test_remote_file_proxy_preserves_auth_path_status_and_body(monkeypatch, backend, method, status):
    received = []
    data = b'stream-file' * 30000
    class Gateway(BaseHTTPRequestHandler):
        def handle_file(self):
            length = int(self.headers.get('Content-Length', '0'))
            received.append((self.command, self.path, self.headers.get('Authorization'),
                             self.headers.get('Range'), self.rfile.read(length)))
            self.send_response(status)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Content-Type', 'application/octet-stream')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(data)
        do_GET = handle_file
        do_HEAD = handle_file
        do_POST = handle_file
        def log_message(self, *args):
            pass
    gateway = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
    class Web(app_web._SpaStaticHandler):
        api_target = f'http://127.0.0.1:{gateway.server_port}'
        def log_message(self, *args):
            pass
    web = ThreadingHTTPServer(('127.0.0.1', 0), Web)
    threads = [Thread(target=s.serve_forever) for s in (gateway, web)]
    for thread in threads:
        thread.start()
    from jiuwenswarm.common import config
    monkeypatch.setattr(config, 'get_config_raw', lambda: {'gateway': {'agent_client': {'type': backend}}})
    path = '/file-api/upload?user_id=alice' if method == 'POST' else '/file-api/raw-file?user_id=alice&path=/home/agentos/a'
    try:
        connection = HTTPConnection('127.0.0.1', web.server_port)
        connection.request(method, path, body=data if method == 'POST' else None,
                           headers={'Authorization': 'Bearer business-token', 'Range': 'bytes=0-99'})
        response = connection.getresponse()
        assert response.status == status
        assert response.read() == (b'' if method == 'HEAD' else data)
        assert received == [(method, path, 'Bearer business-token', 'bytes=0-99', data if method == 'POST' else b'')]
        connection.close()
    finally:
        for server, thread in zip((gateway, web), threads):
            server.shutdown()
            thread.join()
            server.server_close()


def test_remote_file_routing_does_not_fall_back_when_config_read_fails(monkeypatch):
    from jiuwenswarm.common import config

    def unavailable():
        raise OSError('configuration is unavailable')

    monkeypatch.setattr(config, 'get_config_raw', unavailable)
    with pytest.raises(OSError, match='configuration is unavailable'):
        app_web._uses_agentos_routing()
