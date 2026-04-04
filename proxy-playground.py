from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request, urllib.error, os

# Try loading .env file from the same directory as this script
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

# ── Configuration from environment variables ─────────────────────────────────
_allowed_raw = os.environ.get("ALLOWED_IPS", "*").strip()
ALLOWED_IPS = None if _allowed_raw == "*" else {ip.strip() for ip in _allowed_raw.split(",") if ip.strip()}

SERVE_DIR = os.environ.get("SERVE_DIR", os.path.dirname(os.path.abspath(__file__)))
BBG_UPSTREAM = "http://{}:{}".format(
    os.environ.get("BBG_HOST", "127.0.0.1"),
    os.environ.get("API_PORT", "8195"),
)
OPENBB_UPSTREAM = "http://{}:{}".format(
    os.environ.get("OPENBB_HOST", "127.0.0.1"),
    os.environ.get("OPENBB_PORT", "6900"),
)

# Paths forwarded to Bloomberg API
BBG_PATHS = ("/bdp", "/bdh", "/bds", "/bql", "/intraday", "/curve", "/security", "/fields", "/config", "/chat", "/health", "/docs", "/redoc", "/openapi.json")
# Paths forwarded to OpenBB API
OPENBB_PATHS = ("/api/",)


class PlaygroundHandler(BaseHTTPRequestHandler):
    def _check_ip(self):
        if ALLOWED_IPS is None:  # "*" — allow all
            return True
        # Only trust CF-Connecting-IP if TRUST_CF_IP is explicitly enabled
        if os.environ.get("TRUST_CF_IP", "").lower() in ("1", "true", "yes"):
            client_ip = self.headers.get("CF-Connecting-IP") or self.client_address[0]
        else:
            client_ip = self.client_address[0]
        if client_ip not in ALLOWED_IPS:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(f"Forbidden: {client_ip}".encode())
            print(f"BLOCKED {client_ip}")
            return False
        return True

    def _route(self):
        """Return upstream URL or None for static file serving."""
        path = self.path.split("?")[0]
        for p in BBG_PATHS:
            if path == p or path.startswith(p + "/") or path.startswith(p + "?"):
                return BBG_UPSTREAM
        for p in OPENBB_PATHS:
            if path.startswith(p):
                return OPENBB_UPSTREAM
        return None

    def _proxy(self, upstream):
        url = upstream + self.path
        body = None
        if self.headers.get("Content-Length"):
            body = self.rfile.read(int(self.headers["Content-Length"]))
        req = urllib.request.Request(url, data=body, method=self.command)
        for k, v in self.headers.items():
            if k.lower() not in ("host", "content-length"):
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp.read())
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(e.read())

    def do_GET(self):
        if not self._check_ip():
            return
        upstream = self._route()
        if upstream:
            self._proxy(upstream)
            return
        if self.path == "/":
            self.send_response(302)
            self.send_header("Location", "/playground.html")
            self.end_headers()
            return
        if self.path == "/formula-builder":
            self.send_response(302)
            self.send_header("Location", "/formula-builder.html")
            self.end_headers()
            return
        self._serve_file()

    def do_POST(self):
        if not self._check_ip():
            return
        upstream = self._route()
        if upstream:
            self._proxy(upstream)
            return
        self.send_response(404)
        self.end_headers()

    def _serve_file(self):
        import urllib.parse
        path = urllib.parse.unquote(self.path.split("?")[0])
        fspath = os.path.normpath(os.path.join(SERVE_DIR, path.lstrip("/")))
        if not fspath.startswith(SERVE_DIR):
            self.send_response(403); self.end_headers(); return
        if not os.path.isfile(fspath):
            self.send_response(404); self.end_headers(); return
        ext = os.path.splitext(fspath)[1].lower()
        mime = {".html": "text/html", ".js": "application/javascript",
                ".css": "text/css", ".json": "application/json",
                ".png": "image/png", ".ico": "image/x-icon"}.get(ext, "application/octet-stream")
        with open(fspath, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        ip = self.headers.get("CF-Connecting-IP", self.client_address[0])
        print(f"[playground] {ip} {fmt % args}")


if __name__ == "__main__":
    listen_port = int(os.environ.get("PROXY_PORT", "8081"))
    listen_host = os.environ.get("PROXY_HOST", "127.0.0.1")
    server = HTTPServer((listen_host, listen_port), PlaygroundHandler)
    print(f"Playground proxy :{listen_port} -> BBG {BBG_UPSTREAM} | OpenBB {OPENBB_UPSTREAM} | static {SERVE_DIR}")
    server.serve_forever()
