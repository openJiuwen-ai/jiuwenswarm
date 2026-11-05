"""Static hosting and lifecycle readiness only; no application AI transport."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
import threading
from pathlib import Path
from urllib.parse import urlsplit

def _host_allowed(host: str, port: int) -> bool:
    """Accept loopback hosts, including the market's per-app *.localhost origin."""
    if host in {f"127.0.0.1:{port}", f"localhost:{port}"}:
        return True
    hostname, _, host_port = host.rpartition(":")
    # The market opens Web pages from workswarm-app-<digest>.localhost so each
    # app gets its own browser origin; the name still resolves to loopback.
    return bool(hostname) and host_port == str(port) and hostname.endswith(".localhost")


def make_handler(context, token, gateway_api_url=""):
    class Handler(SimpleHTTPRequestHandler):
        def end_headers(self):
            # Local pages must never be cached: the market reuses ports across
            # apps, and a heuristically cached entry would render the previous
            # app's page under the new app's URL.
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def _json(self, status, data):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def translate_path(self, path):
            candidate = Path(super().translate_path(path)).resolve()
            root = Path(context["work_dir"]).resolve()
            sensitive = {".env", ".git", ".jiuwenswarm", ".ssh"}
            if not candidate.is_relative_to(root) or any(
                part in sensitive or part.startswith(".env.")
                for part in candidate.relative_to(root).parts
            ):
                return str(root / "__workswarm_access_denied__")
            return str(candidate)

        def list_directory(self, path):
            self.send_error(403, "Directory listing is disabled")
            return None

        def do_GET(self):
            if not _host_allowed(self.headers.get("Host", ""), self.server.server_port):
                self.send_error(403)
                return
            path = urlsplit(self.path).path
            if path != "/_workswarm/status":
                return super().do_GET()
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.send_error(401)
                return
            self._json(200, {"app_id": context["app_id"], "instance_id": context["instance_id"], "seq": 1, "state": "running", "detail": ""})

        def do_POST(self):
            path = urlsplit(self.path).path
            if path != "/_workswarm/shutdown":
                self.send_error(404)
                return
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.send_error(401)
                return
            self._json(200, {"app_id": context["app_id"], "instance_id": context["instance_id"], "seq": 1, "state": "stopping", "detail": ""})
            # serve_forever runs on the main thread; shutdown() must come from
            # another thread so the response below is flushed before exit.
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    return partial(Handler, directory=context["work_dir"])


def main():
    context = json.loads(Path(os.environ["WORKSWARM_INSTANCE_CONTEXT"]).read_text(encoding="utf-8"))
    handler = make_handler(context, os.environ["WORKSWARM_STATUS_TOKEN"], os.environ.get("WORKSWARM_GATEWAY_API_URL", ""))
    server = ThreadingHTTPServer(("127.0.0.1", int(os.environ["WORKSWARM_APP_PORT"])), handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
