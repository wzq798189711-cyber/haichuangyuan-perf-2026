#!/usr/bin/env python3
"""开发用静态服务：禁用缓存，确保每次刷新都拿到最新 HTML。"""
import http.server
import socketserver

PORT = 5000


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("0.0.0.0", PORT), NoCacheHandler) as httpd:
        print(f"Serving HTTP on 0.0.0.0 port {PORT} (no-cache) ...", flush=True)
        httpd.serve_forever()
