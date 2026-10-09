"""Private CONNECT adapter for OpenClash's bare Basic authentication challenge."""
import base64
from contextlib import contextmanager
import select
import socket
import socketserver
import ssl
import threading
from urllib.parse import unquote, urlsplit


def allowed_target(target):
    host, separator, port = target.rpartition(":")
    host = host.lower()
    domains = ("googlevideo.com", "youtube.com", "youtube-nocookie.com", "youtu.be")
    return separator == ":" and port == "443" and any(
        host == domain or host.endswith("." + domain) for domain in domains)


def read_header(connection):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(4096)
        if not chunk or len(data) + len(chunk) > 32768:
            raise ValueError("Invalid CONNECT header")
        data += chunk
    return data.split(b"\r\n\r\n", 1)


class TunnelServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, proxy):
        self.proxy = urlsplit(proxy)
        if self.proxy.scheme not in ("http", "https") or not self.proxy.hostname:
            raise ValueError("YouTube 录制代理必须是 HTTP 或 HTTPS 代理。")
        self.stopping = threading.Event()
        self.connections = 0
        super().__init__(("127.0.0.1", 0), TunnelHandler)

    def handle_error(self, request, client_address):
        # Neither stream tokens nor proxy credentials belong in logs.
        pass


class TunnelHandler(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = None
        try:
            self.request.settimeout(15)
            header, pending = read_header(self.request)
            parts = header.split(b"\r\n", 1)[0].decode("ascii").split()
            if len(parts) != 3 or parts[0] != "CONNECT" or not allowed_target(parts[1]):
                self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            target = parts[1]
            proxy = self.server.proxy
            upstream = socket.create_connection((proxy.hostname, proxy.port or (443 if proxy.scheme == "https" else 80)), 15)
            if proxy.scheme == "https":
                upstream = ssl.create_default_context().wrap_socket(upstream, server_hostname=proxy.hostname)
            credentials = unquote(proxy.username or "") + ":" + unquote(proxy.password or "")
            authentication = base64.b64encode(credentials.encode()).decode()
            upstream.sendall((f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
                              f"Proxy-Authorization: Basic {authentication}\r\n\r\n").encode("ascii"))
            response, extra = read_header(upstream)
            if response.split(b"\r\n", 1)[0].split()[1] != b"200":
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                return
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n" + extra)
            if pending:
                upstream.sendall(pending)
            self.server.connections += 1
            while not self.server.stopping.is_set():
                ready, _, _ = select.select((self.request, upstream), (), (), 1)
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is self.request else self.request).sendall(data)
        except (OSError, ValueError, UnicodeError, IndexError):
            return
        finally:
            if upstream:
                upstream.close()


@contextmanager
def youtube_proxy(command):
    """Keep credentials upstream; preserve HTTPS bytes and both input proxies."""
    command = list(command)
    indexes = [i + 1 for i, value in enumerate(command[:-1]) if value == "-http_proxy"]
    if not indexes or urlsplit(command[indexes[0]]).username is None:
        yield command
        return
    proxy = command[indexes[0]]
    if any(command[i] != proxy for i in indexes):
        raise ValueError("YouTube 音视频输入的代理配置不一致。")
    server = TunnelServer(proxy)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval":0.1}, daemon=True)
    thread.start()
    try:
        for i in indexes:
            command[i] = f"http://127.0.0.1:{server.server_address[1]}"
        yield command
    finally:
        server.stopping.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
