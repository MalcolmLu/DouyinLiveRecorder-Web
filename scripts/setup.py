"""Initialize fresh persistent data without replacing existing configuration."""
import argparse
import ipaddress
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(getattr(sys,"_MEIPASS",Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(ROOT / "gui"))
from shared import Paths
from platform_utils import acquire_lock


def initialize(paths):
    paths.config.mkdir(parents=True, exist_ok=True)
    with (paths.config / ".initialize.lock").open("a") as lock:
        acquire_lock(lock)
        (paths.config / "h265").mkdir(exist_ok=True)
        paths.state.mkdir(parents=True, exist_ok=True)
        paths.state.chmod(0o700)
        for template, destination in (
            ("config.ini", paths.config / "config.ini"),
            ("URL_config.ini", paths.config / "URL_config.ini"),
            ("h265.json", paths.config / "h265/settings.json"),
        ):
            if not destination.exists():
                fd, name = tempfile.mkstemp(prefix=".initialize-", dir=destination.parent)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write((ROOT / "defaults" / template).read_bytes())
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(name, destination)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)


def certificate(state, host):
    address = str(ipaddress.ip_address(host))
    tls = state / "tls"
    tls.mkdir(parents=True, exist_ok=True)
    tls.chmod(0o700)
    cert, key = tls / "server.crt", tls / "server.key"
    if cert.exists() or key.exists():
        if not cert.is_file() or not key.is_file():
            raise RuntimeError("Incomplete certificate pair; no files replaced.")
        subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-checkend", "86400"],
                       check=True, stdout=subprocess.DEVNULL)
        print("Existing HTTPS certificate retained.")
        return
    with tempfile.TemporaryDirectory(prefix=".certificate-", dir=tls) as directory:
        staging = Path(directory)
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes",
            "-days", "365", "-subj", "/CN=DouyinLiveRecorder-Web",
            "-addext", "subjectAltName=IP:" + address + ",IP:127.0.0.1,DNS:localhost",
            "-addext", "basicConstraints=critical,CA:FALSE",
            "-addext", "keyUsage=critical,digitalSignature,keyEncipherment",
            "-addext", "extendedKeyUsage=serverAuth",
            "-keyout", str(staging / "server.key"), "-out", str(staging / "server.crt"),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        (staging / "server.key").chmod(0o600)
        (staging / "server.crt").chmod(0o644)
        os.replace(staging / "server.key", key)
        os.replace(staging / "server.crt", cert)
    print("HTTPS certificate created for " + address)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("init", "certificate"))
    parser.add_argument("--host", help="IP address used to access this NAS")
    args = parser.parse_args()
    if args.action == "certificate" and not args.host:
        parser.error("certificate requires --host")
    paths = Paths.from_env()
    initialize(paths)
    if args.action == "certificate":
        with (paths.config / ".initialize.lock").open("a") as lock:
            acquire_lock(lock)
            certificate(paths.state, args.host)


if __name__ == "__main__":
    main()
