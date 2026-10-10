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
import urllib.parse

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
            # A disposable byte fixture checks the packaged library and HTTP seeking;
            # video encoding/decoding is covered separately by the real HEVC pipeline.
            recording=Path(directory)/"recordings/Youtube/桌面接口测试/桌面接口测试_2026-10-11_12-34-56.mp4"
            recording.parent.mkdir(parents=True)
            payload=b"0123456789abcdef";recording.write_bytes(payload)
            library_path="/api/rooms/"+room["id"]+"/recordings"
            library=api(library_path)
            assert library["total"]==1 and library["items"][0]["name"]==recording.name
            media_path=library_path+"/media?"+urllib.parse.urlencode({"path":library["items"][0]["path"]})
            with urllib.request.urlopen(urllib.request.Request(base+media_path,
                    headers={"Cookie":cookie,"Range":"bytes=4-7"})) as response:
                assert response.status==206 and response.read()==payload[4:8]
                assert response.headers["Content-Range"]=="bytes 4-7/16"
                assert response.headers.get_content_type()=="video/mp4"
            with urllib.request.urlopen(urllib.request.Request(base+media_path,method="HEAD",
                    headers={"Cookie":cookie})) as response:
                assert response.status==200 and response.headers["Content-Length"]==str(len(payload))
                assert response.read()==b""
            try:
                urllib.request.urlopen(base+media_path)
                raise AssertionError("Anonymous recording access accepted")
            except urllib.error.HTTPError as error:assert error.code==401
            api("/api/rooms/"+room["id"],"DELETE",{"revision":api("/api/dashboard")["revision"]},csrf)
            assert api("/api/dashboard")["rooms"]==[]
            with urllib.request.urlopen(base+"/static/app.js") as response:
                javascript=response.read().decode()
                assert "loadDesktop" in javascript and "openRecordings" in javascript
            try:
                urllib.request.urlopen(base+"/api/desktop")
                raise AssertionError("Anonymous desktop access accepted")
            except urllib.error.HTTPError as error:assert error.code==401
            print("PASS frozen desktop backend: session/CSRF, recorder startup and graceful stop, room CRUD, authenticated recording library and byte ranges, HTML assets, tool discovery")
        except BaseException:
            print(errorfile.read_text(errors="replace")[-8000:])
            for log in (Path(directory)/"logs").glob("*.log"):
                print(log.name,log.read_text(errors="replace")[-5000:])
            raise
        finally:
            if process.poll() is None:
                process.stdin.write("shutdown\n");process.stdin.flush()
                try:process.wait(timeout=75)
                except subprocess.TimeoutExpired:process.kill();process.wait()
        assert process.returncode==0,"Backend did not shut down cleanly"
