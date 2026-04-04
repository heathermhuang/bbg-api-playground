from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request, urllib.error, os

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

_allowed_raw = os.environ.get("ALLOWED_IPS", "*").strip()
ALLOWED_IPS = None if _allowed_raw == "*" else {ip.strip() for ip in _allowed_raw.split(",") if ip.strip()}
UPSTREAM = "http://{}:{}".format(
    os.environ.get("BBG_HOST", "127.0.0.1"),
    os.environ.get("API_PORT", "8195"),
)

class ProxyHandler(BaseHTTPRequestHandler):
    def do_request(self):
        if ALLOWED_IPS is not None:
            client_ip = self.headers.get("CF-Connecting-IP") or self.client_address[0]
            if client_ip not in ALLOWED_IPS:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(f"Forbidden: {client_ip}".encode())
                print(f"BLOCKED {client_ip}")
                return
        url = UPSTREAM + self.path
        body = None
        if self.headers.get("Content-Length"):
            body = self.rfile.read(int(self.headers["Content-Length"]))
        req = urllib.request.Request(url, data=body, method=self.command)
        for k, v in self.headers.items():
            if k.lower() not in ("host", "content-length"):
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp.read())
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(e.read())
    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_request
    def log_message(self, fmt, *args):
        print(f"[bbg-proxy] {self.headers.get('CF-Connecting-IP', self.client_address[0])} {fmt % args}")

if __name__ == "__main__":
    listen_port = int(os.environ.get("BBG_PROXY_PORT", "8196"))
    listen_host = os.environ.get("PROXY_HOST", "127.0.0.1")
    server = HTTPServer((listen_host, listen_port), ProxyHandler)
    print(f"Bloomberg API proxy running on {listen_host}:{listen_port} -> {UPSTREAM}")
    server.serve_forever()
