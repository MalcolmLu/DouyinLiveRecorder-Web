"""Desktop preferences, tool checks and ownership of recorder/worker processes."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from platform_utils import child_command, process_options
from shared import AppError, Paths, SharedLock, atomic_write, read_json, write_json

ENCODERS={"libx265":"CPU · H.265","hevc_qsv":"Intel QSV · H.265",
          "hevc_videotoolbox":"Apple VideoToolbox · H.265","hevc_nvenc":"NVIDIA NVENC · H.265",
          "hevc_amf":"AMD AMF · H.265"}


class DesktopService:
    def __init__(self,paths,data,tools=None):
        self.paths,self.data,self.tools=paths,Path(data),tools
        self.file=paths.state/"desktop-settings.json"
        self.lock=SharedLock(paths.config/".record-web.lock")
        self.mutex=threading.RLock()
        self.recorder=None;self.worker=None;self.logs=[]
        self.stopping=False
        self.closed=threading.Event()
        self.defaults={"downloads":str(paths.downloads),"ffmpeg":"","encoder":"auto",
                       "bitrate_kbps":4000,"cpu_threads":2,"auto_start":True,
                       "close_to_tray":True,"start_minimized":False,"prevent_sleep":True,
                       "recording_roots":[str(paths.downloads)]}
        if not self.file.exists():
            write_json(self.file,self.defaults)
        self.apply_environment()
        self.apply_encoding()

    def preferences(self):
        return dict(self.defaults,**read_json(self.file,{}))

    def revision(self):
        return hashlib.sha256(self.file.read_bytes()).hexdigest()

    def ffmpeg(self,values=None):
        values=values or self.preferences()
        suffix=".exe" if os.name=="nt" else ""
        if values["ffmpeg"]:
            return Path(values["ffmpeg"])
        if self.tools and (self.tools/("ffmpeg"+suffix)).is_file():
            return self.tools/("ffmpeg"+suffix)
        found=shutil.which("ffmpeg")
        return Path(found) if found else None

    def apply_environment(self):
        selected=self.preferences()
        self.paths=Paths(self.paths.config,Path(selected["downloads"]).expanduser().resolve(),self.paths.state)
        self.paths.downloads.mkdir(parents=True,exist_ok=True)
        os.environ["RECORDER_DOWNLOADS_DIR"]=str(self.paths.downloads)
        folders=[]
        binary=self.ffmpeg(selected)
        if binary:folders.append(str(binary.parent))
        if self.tools:folders.append(str(self.tools))
        os.environ["PATH"]=os.pathsep.join([*folders,os.environ.get("PATH","")])
        # Source settings are changed through the same lock as the web console.
        from settings_service import ConfigService
        from repository import ConfigRepository
        with self.lock:
            ConfigService(ConfigRepository(self.paths))._change_ini({
                "直播保存路径(不填则默认)":str(self.paths.downloads),
                "是否跳过代理检测(是/否)":"是",
            })

    def encoder_options(self,values=None):
        binary=self.ffmpeg(values)
        if not binary:return []
        try:
            result=subprocess.run([str(binary),"-hide_banner","-encoders"],capture_output=True,
                                  text=True,timeout=15,**process_options())
        except (OSError,subprocess.SubprocessError):
            return []
        permitted={"libx265","hevc_videotoolbox"} if sys.platform=="darwin" else {"libx265","hevc_qsv","hevc_nvenc","hevc_amf"}
        return [key for key in ENCODERS if key in permitted and key in result.stdout]

    def test_encoder(self,encoder,values=None):
        values=values or self.preferences()
        binary=self.ffmpeg(values)
        if encoder not in self.encoder_options(values):
            raise AppError("当前 FFmpeg 不支持这个编码器。","encoder_unavailable",409)
        command=[str(binary),"-hide_banner","-v","error","-f","lavfi","-i",
                 "testsrc2=size=320x180:rate=15","-frames:v","8","-c:v",encoder]
        if encoder=="hevc_videotoolbox":command+=["-allow_sw","0","-b:v","1000k"]
        elif encoder=="libx265":command+=["-x265-params","pools=1:frame-threads=1:log-level=error"]
        elif encoder=="hevc_qsv" and os.name=="nt":
            command[1:1]=["-init_hw_device","qsv=qs:hw,child_device_type=d3d11va","-filter_hw_device","qs"]
            command+=["-vf","format=nv12,hwupload=extra_hw_frames=32"]
        command+=["-f","null","-"]
        try:
            result=subprocess.run(command,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                                  timeout=30,**process_options())
        except (OSError,subprocess.SubprocessError):
            raise AppError("编码测试未完成，请检查工具路径和显卡驱动。","encoder_unavailable",409)
        if result.returncode:
            raise AppError("编码器初始化失败，请检查显卡驱动或选择其他编码器；录像会保留。","encoder_unavailable",409)
        return {"ok":True,"message":ENCODERS[encoder]+" 编码测试通过。"}

    def apply_encoding(self):
        selected=self.preferences();encoder=selected["encoder"]
        if encoder=="auto":
            encoder="libx265"
            candidates=["hevc_videotoolbox"] if sys.platform=="darwin" else ["hevc_qsv","hevc_nvenc","hevc_amf"]
            for candidate in candidates:
                try:self.test_encoder(candidate,selected);encoder=candidate;break
                except AppError:pass
        with self.lock:
            target=self.paths.config/"h265/settings.json"
            settings=read_json(target,{})
            settings.update(encoder=encoder,bitrate_kbps=selected["bitrate_kbps"],cpu_threads=selected["cpu_threads"])
            write_json(target,settings)

    def status(self):
        return {"running":bool(self.recorder and self.recorder.poll() is None),
                "stopping":self.stopping,
                "worker_running":bool(self.worker and self.worker.poll() is None),
                "ffmpeg_ready":bool(self.ffmpeg() and self.ffmpeg().is_file()),
                "node_ready":bool(shutil.which("node")),
                "encoder":read_json(self.paths.config/"h265/settings.json",{}).get("encoder","libx265")}

    def snapshot(self):
        return {"values":self.preferences(),"revision":self.revision(),"status":self.status(),
                "encoders":[{"value":key,"label":ENCODERS[key]} for key in self.encoder_options()],
                "data_dir":str(self.data),"platform":sys.platform,
                "ffmpeg_path":str(self.ffmpeg() or "")}

    def _spawn(self,role):
        logdir=self.data/"logs";logdir.mkdir(parents=True,exist_ok=True)
        command=child_command(role,*( ["--worker"] if role=="postprocess" else []))
        with (logdir/(role+"-desktop.log")).open("ab") as log:
            return subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                                    env=os.environ.copy(),**process_options())

    def start(self):
        if self.closed.is_set():raise AppError("软件正在退出，请稍候。","shutting_down",409)
        if self.stopping:raise AppError("采集器正在保存录像，请稍候。","stopping",409)
        with self.mutex:
            if self.status()["running"]:return
            if not self.status()["ffmpeg_ready"] or not self.status()["node_ready"]:
                raise AppError("请先在桌面设置中配置 FFmpeg，并检查随软件提供的 Node.js。","tools_missing",409)
            (self.paths.state/"desktop-stop").unlink(missing_ok=True)
            self.recorder=self._spawn("recorder")

    def start_worker(self):
        with self.mutex:
            if self.closed.is_set():return
            if not self.status()["worker_running"] and self.status()["ffmpeg_ready"]:
                self.worker=self._spawn("postprocess")

    def request_stop(self):
        if self.stopping:return
        threading.Thread(target=self.stop,daemon=True).start()

    def stop(self):
        with self.mutex:
            if not self.status()["running"]:return
            self.stopping=True
            atomic_write(self.paths.state/"desktop-stop","stop")
            try:self.recorder.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self._terminate(self.recorder)
            self.recorder=None
            self.stopping=False

    @staticmethod
    def _terminate(process):
        import psutil
        if not process or process.poll() is not None:return
        try:
            owned=psutil.Process(process.pid)
            children=owned.children(recursive=True)
            for child in children:child.terminate()
            owned.terminate()
            _,alive=psutil.wait_procs([owned,*children],timeout=5)
            for child in alive:
                try:child.kill()
                except psutil.NoSuchProcess:pass
        except psutil.NoSuchProcess:pass

    def save(self,body):
        if self.closed.is_set():raise AppError("软件正在退出，请稍候。","shutting_down",409)
        if not isinstance(body,dict) or body.get("revision")!=self.revision():
            raise AppError("桌面设置已变化，请刷新后重试。","stale_revision",409)
        values=body.get("values")
        required=set(self.defaults)-{"recording_roots"}
        if not isinstance(values,dict) or set(values)!=required:
            raise AppError("桌面设置字段不完整。")
        for key in ("auto_start","close_to_tray","start_minimized","prevent_sleep"):
            if type(values[key]) is not bool:raise AppError("桌面开关设置不正确。")
        for key,low,high in (("bitrate_kbps",500,100000),("cpu_threads",1,16)):
            if type(values[key]) is not int or not low<=values[key]<=high:raise AppError("编码参数超出支持范围。")
        if values["encoder"] not in ("auto",*ENCODERS):raise AppError("编码器不正确。")
        if not isinstance(values["downloads"],str) or not Path(values["downloads"]).expanduser().is_absolute():
            raise AppError("请选择绝对路径的录像目录。")
        if not isinstance(values["ffmpeg"],str):raise AppError("FFmpeg 路径不正确。")
        if values["ffmpeg"]:
            binary=Path(values["ffmpeg"])
            if not binary.is_file() or binary.name.lower() not in ("ffmpeg","ffmpeg.exe"):
                raise AppError("请选择名为 ffmpeg 或 ffmpeg.exe 的程序文件。")
        values=dict(values);values["downloads"]=str(Path(values["downloads"]).expanduser().resolve())
        with self.mutex,self.lock:
            if body.get("revision")!=self.revision():
                raise AppError("桌面设置已变化，请刷新后重试。","stale_revision",409)
            previous=self.preferences()
            changed=any(values[key]!=previous[key] for key in ("downloads","ffmpeg"))
            if changed and self.status()["running"]:
                raise AppError("修改录像目录或 FFmpeg 路径前，请先停止采集器。","recorder_running",409)
            from repository import QueueRepository
            queue=QueueRepository(self.paths).summary()
            if changed and sum(queue.get(k,0) for k in ("queued","running","published")):
                raise AppError("转码队列处理完毕后才能修改目录或工具路径。","queue_active",409)
            Path(values["downloads"]).mkdir(parents=True,exist_ok=True)
            values["recording_roots"]=list(dict.fromkeys([*previous["recording_roots"],previous["downloads"],values["downloads"]]))
            if values["encoder"]!="auto":self.test_encoder(values["encoder"],values)
            if changed:
                self._terminate(self.worker);self.worker=None
            write_json(self.file,values)
            self.apply_environment();self.apply_encoding()
            self.start_worker()
        return {"ok":True,"message":"桌面设置已保存。编码参数从下个转码任务生效。","snapshot":self.snapshot()}

    def shutdown(self):
        if self.closed.is_set():return
        self.closed.set()
        self.stop()
        from repository import QueueRepository
        queue=QueueRepository(self.paths)
        interrupted=None
        if self.paths.queue.exists():
            with queue.connect() as db:
                interrupted=db.execute("SELECT * FROM jobs WHERE status='running' LIMIT 1").fetchone()
        self._terminate(self.worker);self.worker=None
        if interrupted:
            source=Path(interrupted["source"])
            try:unchanged=(source.stat().st_size,source.stat().st_mtime_ns)==(interrupted["size"],interrupted["mtime"])
            except OSError:unchanged=False
            if unchanged:
                with queue.connect(write=True) as db:
                    db.execute("UPDATE jobs SET status='queued',error=NULL,updated=? WHERE id=? AND status IN ('running','failed')",
                               (time.time(),interrupted["id"]))
        for stream in self.logs:stream.close()
