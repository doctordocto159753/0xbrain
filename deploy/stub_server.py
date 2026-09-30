"""DEPLOYMENT STUB. Not an MCP server and never a substitute for one.

Exists only so the container/Caddy/installer/backup plumbing can be exercised
before the real transport (Agent A) is integrated. It serves /healthz and
returns 503 for everything else. Started only when BRAIN_ALLOW_STUB=1.
"""
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == os.environ.get("BRAIN_HEALTH_PATH", "/healthz"):
            body, code = b"ok stub\n", 200
        else:
            body, code = b"0xbrain deployment stub: no MCP server installed\n", 503
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET

    def log_message(self, *a):
        pass


HTTPServer((os.environ.get("BRAIN_HOST", "0.0.0.0"),
            int(os.environ.get("BRAIN_PORT", "8080"))), H).serve_forever()
