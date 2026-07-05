#!/usr/bin/env python3
"""FileBrowser reverse proxy with auto-login bridge.
Listens on :8088 (HTTPS), proxies to FileBrowser on :8087 (HTTP).
/bridge?token=JWT → sets auth cookie, redirects to /
"""
import http.server
import ssl
import urllib.request
import urllib.parse

FB_BACKEND = 'http://127.0.0.1:8087'
CERT = '/etc/filebrowser/ssl/cert.pem'
KEY = '/etc/filebrowser/ssl/key.pem'

class ProxyHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path == '/bridge' and qs.get('token'):
            token = qs['token'][0]
            self.send_response(302)
            self.send_header('Set-Cookie', f'auth={token}; Path=/; SameSite=Strict; Secure')
            self.send_header('Location', '/')
            self.end_headers()
            return
        self._proxy('GET')

    def _proxy(self, method):
        try:
            url = f'{FB_BACKEND}{self.path}'
            # Read body if present
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length) if length > 0 else b''
            req = urllib.request.Request(url, data=body, method=method)
            # Forward relevant headers
            for h in ('Cookie', 'Authorization', 'Content-Type', 'Depth', 'Destination', 'If', 'Lock-Token', 'Overwrite', 'Timeout'):
                v = self.headers.get(h)
                if v:
                    req.add_header(h, v)
            resp = urllib.request.urlopen(req, timeout=60)
            rbody = resp.read()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                kl = k.lower()
                if kl not in ('transfer-encoding', 'connection', 'keep-alive'):
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(rbody)
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f'Proxy error: {e}'.encode())

    # All HTTP methods → generic proxy
    def do_POST(self):      self._proxy('POST')
    def do_PUT(self):       self._proxy('PUT')
    def do_DELETE(self):    self._proxy('DELETE')
    def do_PATCH(self):     self._proxy('PATCH')
    def do_OPTIONS(self):   self._proxy('OPTIONS')
    def do_PROPFIND(self):  self._proxy('PROPFIND')
    def do_MKCOL(self):     self._proxy('MKCOL')
    def do_COPY(self):      self._proxy('COPY')
    def do_MOVE(self):      self._proxy('MOVE')
    def do_LOCK(self):      self._proxy('LOCK')
    def do_UNLOCK(self):    self._proxy('UNLOCK')
    def do_PROPPATCH(self): self._proxy('PROPPATCH')

    def log_message(self, *args):
        pass

if __name__ == '__main__':
    addr = ('0.0.0.0', 8088)
    httpd = http.server.HTTPServer(addr, ProxyHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT, KEY)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print(f'FB Proxy running on :8088 -> {FB_BACKEND}')
    httpd.serve_forever()
