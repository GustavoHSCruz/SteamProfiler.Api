"""HTTP gate and real security collector for the nginx integration check."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import security_log

upstream_requests = 0


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global upstream_requests
        if self.path == "/__security_test__":
            body = json.dumps({**security_log.report(), "test_upstream_requests": upstream_requests}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
        else:
            upstream_requests += 1
            self.send_response(403 if self.path == "/trap" else 204)
            self.end_headers()

    def do_POST(self):
        global upstream_requests
        upstream_requests += 1
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(400)
        self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    security_log.start()
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
