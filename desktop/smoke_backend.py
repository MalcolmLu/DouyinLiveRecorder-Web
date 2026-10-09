"""Exercise the frozen desktop backend with an isolated profile."""
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

executable=Path(sys.argv[1]).resolve();tools=Path(sys.argv[2]).resolve()
with tempfile.TemporaryDirectory(prefix="dlr-frozen-smoke-") as directory:
    errorfile=Path(directory)/"smoke-errors.log"
    with errorfile.open("wb") as errors:
        process=subprocess.Popen([str(executable),"--data-dir",directory,"--tools-dir",str(tools)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=errors,text=True)
        try:
            lines=queue.Queue()
            def read():
                for line in process.stdout:lines.put(line)
            threading.Thread(target=read,daemon=True).start()
            deadline=time.monotonic()+120;ready=None
            while time.monotonic()<deadline:
                if process.poll() is not None:raise RuntimeError(errorfile.read_text(errors="replace"))
                try:
                    value=json.loads(lines.get(timeout=1))
                    if value.get("event")=="ready":ready=value;break
                except (queue.Empty,json.JSONDecodeError):pass
            assert ready,"Backend startup timed out"
            base=ready["url"];cookie="recorder_session="+ready["cookie"]
            def api(path,method="GET",body=None,csrf=None):
                headers={"Cookie":cookie,"Origin":base,"Content-Type":"application/json"}
                if csrf:headers["X-CSRF-Token"]=csrf
                request=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,
                                               method=method,headers=headers)
                with urllib.request.urlopen(request,timeout=40) as response:return json.load(response)
            session=api("/api/session");assert session["authenticated"] and session["desktop"]
            csrf=session["csrf"]
            desktop=api("/api/desktop");assert desktop["status"]["ffmpeg_ready"] and desktop["status"]["node_ready"]
            for _ in range(30):
                snapshot=api("/api/dashboard")
                if snapshot["recorder"]["connected"]:break
                time.sleep(1)
            assert snapshot["recorder"]["connected"],"Frozen recorder did not report heartbeat"
            api("/api/desktop/stop","POST",{},csrf)
            for _ in range(70):
                if not api("/api/desktop")["status"]["running"]:break
                time.sleep(1)
            assert not api("/api/desktop")["status"]["running"]
            revision=api("/api/dashboard")["revision"]
            room=api("/api/rooms","POST",{"url":"https://www.youtube.com/@DesktopFixture/streams",
                 "name":"桌面接口测试","quality":"原画","proxy":"direct","revision":revision},csrf)
            row=api("/api/dashboard")["rooms"][0];assert row["url"].endswith("/live")
            api("/api/rooms/"+room["id"],"DELETE",{"revision":api("/api/dashboard")["revision"]},csrf)
            assert api("/api/dashboard")["rooms"]==[]
            with urllib.request.urlopen(base+"/static/app.js") as response:
                assert "loadDesktop" in response.read().decode()
            try:
                urllib.request.urlopen(base+"/api/desktop")
                raise AssertionError("Anonymous desktop access accepted")
            except urllib.error.HTTPError as error:assert error.code==401
            print("PASS frozen desktop backend: session/CSRF, recorder startup and graceful stop, room CRUD, HTML assets, tool discovery")
        except BaseException:
            for log in (Path(directory)/"logs").glob("*.log"):
                print(log.name,log.read_text(errors="replace")[-5000:])
            raise
        finally:
            if process.poll() is None:
                process.stdin.write("shutdown\n");process.stdin.flush()
                try:process.wait(timeout=75)
                except subprocess.TimeoutExpired:process.kill();process.wait()
        assert process.returncode==0,"Backend did not shut down cleanly"
