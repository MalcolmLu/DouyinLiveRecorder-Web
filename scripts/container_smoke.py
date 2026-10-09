"""Exercise the built image with isolated Docker volumes and local HTTPS."""
import json
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

image = sys.argv[1]
name = "dlr-web-smoke-" + secrets.token_hex(5)
volume = name + "-config"


def run(*args):
    return subprocess.check_output(["docker",*args],text=True).strip()


run("volume","create",volume)
try:
    run("run","--rm","-v",volume+":/app/config",image,"python","scripts/setup.py","certificate","--host","127.0.0.1")
    run("run","-d","--name",name,"--read-only","--tmpfs","/tmp:rw,size=64m",
        "-v",volume+":/app/config","-p","127.0.0.1::18888",image,"python","gui/serve.py")
    port = int(json.loads(run("inspect",name))[0]["NetworkSettings"]["Ports"]["18888/tcp"][0]["HostPort"])
    with tempfile.TemporaryDirectory(prefix="dlr-https-test-") as directory:
        cert = Path(directory) / "server.crt"
        run("cp",name+":/app/config/gui/state/tls/server.crt",str(cert))
        context = ssl.create_default_context(cafile=str(cert))
        base = "https://127.0.0.1:" + str(port)
        deadline = time.monotonic() + 45
        while True:
            try:
                with urllib.request.urlopen(base+"/health",context=context,timeout=3) as response:
                    assert response.status == 200
                break
            except (OSError,urllib.error.URLError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)
        for endpoint in ("/ready","/","/static/app.js"):
            with urllib.request.urlopen(base+endpoint,context=context,timeout=5) as response:
                assert response.status == 200
                if endpoint == "/static/app.js":
                    assert "删除记录" in response.read().decode()
        try:
            urllib.request.urlopen(base+"/api/jobs",context=context,timeout=5)
            raise AssertionError("Unauthenticated queue access accepted")
        except urllib.error.HTTPError as error:
            assert error.code == 401
        print("PASS built image: HTTPS certificate verification, health/readiness, HTML/JS, protected queue API")
except BaseException:
    subprocess.run(["docker","logs","--tail","80",name],check=False)
    raise
finally:
    subprocess.run(["docker","rm","-f",name],check=False,stdout=subprocess.DEVNULL)
    subprocess.run(["docker","volume","rm",volume],check=False,stdout=subprocess.DEVNULL)
