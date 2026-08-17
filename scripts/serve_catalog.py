"""Serves fixtures/catalog.json as the Catalog API described in BRIEF.md §3, so
CATALOG_BASE points at the real HTTP server rather than a special case. Not part of the
Evaluator service itself - this stands in for the real Catalog API in local dev
and in the Docker Compose setup.

Run: python scripts/serve_catalog.py  (defaults to :8000)
"""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

CATALOG_PATH = os.path.join(os.path.dirname(__file__), "..", "fixtures", "catalog.json")
CATALOG = json.load(open(CATALOG_PATH))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/v1/catalog":
            self.send_response(404)
            self.end_headers()
            return
        version = parse_qs(parsed.query).get("version", [None])[0]
        if version is not None and version != CATALOG["version"]:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(CATALOG).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # keep test/dev output quiet


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
