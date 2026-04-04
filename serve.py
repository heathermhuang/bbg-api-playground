from http.server import HTTPServer, SimpleHTTPRequestHandler
import os

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=r"C:\openbb", **kwargs)

    def do_GET(self):
        if self.path == "/":
            self.send_response(302)
            self.send_header("Location", "/playground.html")
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, fmt, *args):
        pass  # suppress logs

if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 8080), Handler)
    print("Serving playground on http://127.0.0.1:8080/playground.html")
    server.serve_forever()
