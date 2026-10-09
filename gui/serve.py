"""Production WSGI server. HTTPS is mandatory outside loopback development."""
import logging
import os
from pathlib import Path
import ssl
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "vendor"))
from gunicorn.app.base import BaseApplication
from shared import Paths


class Server(BaseApplication):
    def load_config(self):
        paths = Paths.from_env()
        dev = os.environ.get("GUI_ENV") == "development"
        host = os.environ.get("GUI_HOST", "127.0.0.1" if dev else "0.0.0.0")
        port = int(os.environ.get("GUI_PORT", "18888"))
        if dev and host not in ("127.0.0.1", "::1"):
            raise RuntimeError("Development HTTP is restricted to loopback.")
        if not 1024 <= port <= 65535:
            raise RuntimeError("GUI_PORT must be between 1024 and 65535")
        options = {
            "bind": host + ":" + str(port), "workers": 1, "threads": 4, "timeout": 20,
            "graceful_timeout": 15, "keepalive": 3, "limit_request_line": 4094,
            "limit_request_fields": 40, "accesslog": None, "errorlog": "-",
            "worker_tmp_dir": "/tmp", "forwarded_allow_ips": "",
            "control_socket_disable": True,
        }
        if not dev:
            cert = Path(os.environ.get("GUI_CERT_FILE", str(paths.state / "tls/server.crt")))
            key = Path(os.environ.get("GUI_KEY_FILE", str(paths.state / "tls/server.key")))
            if not cert.is_file() or not key.is_file():
                raise RuntimeError("HTTPS certificate or private key is missing.")
            options.update(certfile=str(cert), keyfile=str(key))
        for key, value in options.items():
            self.cfg.set(key, value)

    def load(self):
        from app import create_app
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        return create_app()


if __name__ == "__main__":
    Server().run()
