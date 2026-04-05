#!/usr/bin/env python3
"""VOLMAX Lighting — Design Preview Server"""

import http.server
import socketserver
import os
import json
from pathlib import Path
from urllib.parse import urlparse

PORT = 8080
BASE_DIR = Path(__file__).parent

class VolmaxHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        routes = {
            "/": "templates/index.html",
            "/performance": "templates/performance.html",
            "/setup": "templates/fixture_setup.html",
            "/zones": "templates/zones.html",
            "/timeline": "templates/track_timeline.html",
        }

        if path in routes:
            file_path = BASE_DIR / routes[path]
            if file_path.exists():
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(file_path.read_bytes())
            else:
                self.send_error(404, f"Template not found: {routes[path]}")
        else:
            super().do_GET()

    def log_message(self, format, *args):
        print(f"  {self.address_string()} → {format % args}")


if __name__ == "__main__":
    with socketserver.TCPServer(("", PORT), VolmaxHandler) as httpd:
        print(f"\n⚡ VOLMAX Design Server running at http://localhost:{PORT}\n")
        print("  Screens:")
        print(f"    /              → App overview")
        print(f"    /performance   → Performance Mode (main screen)")
        print(f"    /setup         → Fixture Setup")
        print(f"    /zones         → Zone Manager")
        print(f"    /timeline      → Tracks\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")
