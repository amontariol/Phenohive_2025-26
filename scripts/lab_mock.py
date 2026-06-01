import json
import random
from http.server import BaseHTTPRequestHandler, HTTPServer

class MockLabHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == '/set_wavelength':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data)
            print(f"Mock Monochromator: Moving to {data.get('nm', 'unknown')}nm...")
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())

    def do_GET(self):
        if self.path == '/get_power':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            val = random.uniform(0.0001, 0.001)
            self.wfile.write(json.dumps({"watts": val}).encode())

if __name__ == "__main__":
    print("Starting Mock Lab Equipment on http://127.0.0.1:5000 (Standard Lib)")
    server = HTTPServer(('127.0.0.1', 5000), MockLabHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMock server stopped.")
