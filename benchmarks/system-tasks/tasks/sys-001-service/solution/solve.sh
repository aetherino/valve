cat > /app/server.py <<'PY'
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.path == "/ping"
        self.send_response(200 if ok else 404); self.end_headers()
        self.wfile.write(b"pong" if ok else b"")
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8000), H).serve_forever()
PY
nohup python3 /app/server.py > /tmp/server.log 2>&1 &
sleep 1
