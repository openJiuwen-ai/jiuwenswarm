"""Static hosting and lifecycle readiness only; no application AI transport."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

def make_handler(context, token, gateway_api_url=""):
    class Handler(SimpleHTTPRequestHandler):
        def _json(self, status, data):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
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
            allowed = {"127.0.0.1:" + str(self.server.server_port), "localhost:" + str(self.server.server_port)}
            if self.headers.get("Host", "") not in allowed:
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
            self.send_error(404)

    return partial(Handler, directory=context["work_dir"])


def main():
    context = json.loads(Path(os.environ["WORKSWARM_INSTANCE_CONTEXT"]).read_text(encoding="utf-8"))
    handler = make_handler(context, os.environ["WORKSWARM_STATUS_TOKEN"], os.environ.get("WORKSWARM_GATEWAY_API_URL", ""))
    server = ThreadingHTTPServer(("127.0.0.1", int(os.environ["WORKSWARM_APP_PORT"])), handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
