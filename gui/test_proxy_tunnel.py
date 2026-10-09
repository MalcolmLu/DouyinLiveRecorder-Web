import base64
import socket
import socketserver
import threading
import unittest
from proxy_tunnel import allowed_target, read_header, youtube_proxy


class FixtureProxy(socketserver.BaseRequestHandler):
    def handle(self):
        header, _ = read_header(self.request)
        self.server.received = header
        self.request.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
        data = self.request.recv(65536)
        self.request.sendall(data)


class ProxyTests(unittest.TestCase):
    def test_authenticated_tunnel_both_inputs_and_tls_bytes(self):
        upstream = socketserver.ThreadingTCPServer(("127.0.0.1",0),FixtureProxy)
        upstream.daemon_threads = True
        thread = threading.Thread(target=upstream.serve_forever, daemon=True); thread.start()
        proxy = f"http://test:p%40ss@127.0.0.1:{upstream.server_address[1]}"
        command = ["ffmpeg","-http_proxy",proxy,"-i","video","-http_proxy",proxy,"-i","audio","out.ts"]
        try:
            with youtube_proxy(command) as adapted:
                self.assertEqual(adapted[2],adapted[6])
                self.assertNotIn("test",adapted[2])
                port = int(adapted[2].rsplit(":",1)[1])
                with socket.create_connection(("127.0.0.1",port),2) as client:
                    client.sendall(b"CONNECT manifest.googlevideo.com:443 HTTP/1.1\r\n\r\n")
                    response,_ = read_header(client)
                    self.assertIn(b"200",response)
                    payload = b"\x16\x03\x03fixture TLS bytes"
                    client.sendall(payload)
                    self.assertEqual(client.recv(100),payload)
                expected = base64.b64encode(b"test:p@ss")
                self.assertIn(b"Proxy-Authorization: Basic " + expected,upstream.received)
                with socket.create_connection(("127.0.0.1",port),2) as client:
                    client.sendall(b"CONNECT 192.0.2.10:443 HTTP/1.1\r\n\r\n")
                    self.assertIn(b"403",client.recv(200))
            with self.assertRaises(OSError): socket.create_connection(("127.0.0.1",port),0.2)
        finally:
            upstream.shutdown(); upstream.server_close(); thread.join(timeout=2)

    def test_target_restrictions_and_unauthenticated_passthrough(self):
        self.assertTrue(allowed_target("r1.googlevideo.com:443"))
        for target in ("evilgooglevideo.com:443","youtube.com.evil:443","localhost:443","youtube.com:80"):
            self.assertFalse(allowed_target(target))
        cmd=["ffmpeg","-http_proxy","http://fixture.invalid:1","-i","video","out.ts"]
        with youtube_proxy(cmd) as result:self.assertEqual(result,cmd)


if __name__ == "__main__":unittest.main()
