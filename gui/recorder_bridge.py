"""Recorder-side adapter. It exposes status files, never a Docker socket."""
import functools
import importlib.util
import os
from pathlib import Path
import re
import threading
import time

from settings_service import ConfigService
from repository import ConfigRepository
from shared import AppError, Paths, canonical_url, read_json, room_id, sanitize_error, write_json
from youtube_source import extract_live, with_audio
from proxy_tunnel import youtube_proxy
from contextlib import nullcontext


def safe_url(value):
    try:
        return canonical_url(value)
    except AppError:
        return str(value)


class StopMonitoring(BaseException):
    """Exit the retired room loop without entering upstream's error/retry handler."""


class LoggerProxy:
    def __init__(self, logger, bridge):
        self.logger, self.bridge = logger, bridge

    def __getattr__(self, name):
        return getattr(self.logger, name)

    def error(self, message, *args, **kwargs):
        url = getattr(self.bridge.local, "url", None)
        if url:
            self.bridge.event(url, "error", error=sanitize_error(message))
        return self.logger.error(message, *args, **kwargs)


class RecorderBridge:
    def __init__(self, namespace, paths=None):
        self.namespace = namespace
        self.paths = paths or Paths.from_env()
        self.config = ConfigService(ConfigRepository(self.paths))
        self.paths.state.mkdir(parents=True, exist_ok=True)
        self.mutex = threading.RLock()
        self.local = threading.local()
        self.rooms = {}
        self.active = {}
        self.processor = None
        script = Path(__file__).resolve().parent.parent / "h265/postprocess.py"
        if script.exists():
            spec = importlib.util.spec_from_file_location("web_h265_processor", script)
            self.processor = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.processor)

    def event(self, url, state, force=False, **fields):
        try:
            identifier = room_id(safe_url(url))
            with self.mutex:
                current = self.rooms.setdefault(identifier, {})
                if not force and current.get("state") == "recording" and state in ("checking", "error"):
                    return
                current.update(state=state, checked_at=time.time(), **fields)
                if state != "error":
                    current["error"] = ""
        except (TypeError, ValueError):
            return

    def resolve_proxy(self, url, fallback):
        with self.config.lock:
            mode = read_json(self.config.metadata, {}).get(safe_url(url), {}).get("proxy", "inherit")
            if mode == "direct":
                result = None
            elif mode == "shared":
                result = self.config.proxy_url() or None
            else:
                result = fallback
        self.event(url, "checking", applied_proxy=bool(result))
        return result

    def current_room_data(self, original):
        with self.config.lock:
            current = next((r for r in self.config.rooms() if r["url"] == safe_url(original[1])), None)
        if not current or not current["enabled"]:
            raise StopMonitoring()
        return current["quality"], original[1], current["_name_value"]

    def observed(self, url, name, is_live, platform):
        self.event(url, "live" if is_live else "offline",
                   name=re.sub(r"^序号\d+\s*", "", name), platform=platform)

    async def youtube_stream(self, url, proxy_addr=None, cookies=None):
        self.local.youtube_audio = None
        result = await extract_live(url, proxy_addr, cookies)
        self.local.youtube_audio = result.get("audio_url")
        return result

    def source_files(self, pattern):
        path = Path(pattern)
        directory = path.parent.resolve()
        directory.relative_to(self.paths.downloads)
        expression = re.escape(path.name)
        expression = re.sub(r"%0?\d*d", lambda _: r"\d+", expression)
        match = re.compile(expression).fullmatch
        return sorted(p for p in directory.iterdir() if p.is_file() and match(p.name)
                      and p.suffix.lower() in (".ts", ".mkv", ".mp4", ".flv") and not p.name.startswith("."))

    def policy(self, source):
        with self.mutex:
            for entry in self.active.values():
                if Path(source).parent == Path(entry["pattern"]).parent:
                    prefix = Path(entry["pattern"]).stem.split("%")[0]
                    if Path(source).name.startswith(prefix):
                        return entry["policy"]
        section = self.config._section()
        return {"convert": section.get("录制完成后自动转为mp4格式", "是") == "是",
                "delete": section.get("追加格式后删除原文件", "是") == "是"}

    def dispatch_single(self, source, delete_original=True, policy=None):
        """Native TS callbacks and the format adapter share the same deduplication."""
        if not self.processor:
            return
        source = Path(source)
        selected = policy or self.policy(source)
        if not selected["convert"] or not source.is_file() or not source.stat().st_size:
            return
        source = source.resolve()
        source.relative_to(self.paths.downloads)
        if self.processor.opened_by_ffmpeg(source):
            return
        stat = source.stat()
        with self.processor.connect() as db:
            previous = db.execute("SELECT output_size,output_mtime FROM jobs WHERE status='done' AND output=? ORDER BY updated DESC LIMIT 1",
                                  (str(source),)).fetchone()
        if previous and (stat.st_size, stat.st_mtime_ns) == (previous["output_size"], previous["output_mtime"]):
            return
        self.processor.enqueue(source, selected["delete"])
        self.processor.ensure_worker()

    def wrap_start(self, original):
        @functools.wraps(original)
        def run(url_data, *args, **kwargs):
            self.local.url = url_data[1]
            self.event(url_data[1], "checking")
            try:
                return original(url_data, *args, **kwargs)
            except StopMonitoring:
                return None
            finally:
                with self.config.lock:
                    enabled = next((r["enabled"] for r in self.config.rooms() if r["url"] == safe_url(url_data[1])), False)
                self.event(url_data[1], "unknown" if enabled else "disabled", force=True)
                running = self.namespace.get("running_list", [])
                if not enabled and url_data[1] in running:
                    running.remove(url_data[1])
                    self.namespace["monitoring"] = max(0, self.namespace.get("monitoring", 0) - 1)
                self.local.url = None
                self.local.youtube_audio = None
        return run

    def wrap_record(self, original):
        @functools.wraps(original)
        def run(record_name, record_url, ffmpeg_command, save_type, *args, **kwargs):
            # A link may be edited while upstream is fetching its stream metadata.
            # Recheck immediately before starting FFmpeg so the retired URL cannot record.
            try:
                self.current_room_data((None, record_url, None))
            except StopMonitoring:
                self.namespace.get("recording", set()).discard(record_name)
                self.namespace.get("recording_time_list", {}).pop(record_name, None)
                raise
            audio = getattr(self.local, "youtube_audio", None)
            if audio:
                ffmpeg_command = with_audio(ffmpeg_command, audio)
            policy = {"convert": bool(self.namespace.get("converts_to_mp4", True)),
                      "delete": bool(self.namespace.get("delete_origin_file", True))}
            with self.mutex:
                self.active[room_id(safe_url(record_url))] = {"pattern": ffmpeg_command[-1], "policy": policy}
            self.event(record_url, "recording", name=re.sub(r"^序号\d+\s*", "", record_name),
                       started_at=time.time(), file=Path(ffmpeg_command[-1]).name)
            try:
                is_youtube = "youtube.com/" in record_url or "youtu.be/" in record_url
                tunnel = youtube_proxy(ffmpeg_command) if is_youtube else nullcontext(ffmpeg_command)
                with tunnel as command:
                    result = original(record_name, record_url, command, save_type, *args, **kwargs)
                if policy["convert"] and save_type.upper() in ("TS", "MKV", "MP4"):
                    for source in self.source_files(ffmpeg_command[-1]):
                        self.dispatch_single(source, policy=policy)
                self.event(record_url, "checking", force=True, started_at=None, file=None)
                return result
            except BaseException:
                self.event(record_url, "error", force=True, error="录制进程异常结束，已保留可用的原始文件。")
                raise
            finally:
                with self.mutex:
                    self.active.pop(room_id(safe_url(record_url)), None)
        return run

    def heartbeat(self):
        last_worker_check = 0
        while True:
            try:
                with self.mutex:
                    for identifier in self.active:
                        self.rooms.setdefault(identifier, {}).update(state="recording", checked_at=time.time())
                    states = {key: dict(value) for key, value in self.rooms.items()}
                    idle = not self.active and not self.namespace.get("recording")
                if idle:
                    self.config.apply_pending()
                write_json(self.paths.state / "runtime.json",
                           {"heartbeat": time.time(), "pid": os.getpid(), "version": self.namespace.get("version"),
                            "rooms": states})
                if self.processor and time.monotonic() - last_worker_check > 30:
                    self.processor.ensure_worker()
                    last_worker_check = time.monotonic()
            except Exception as exc:
                # Logging stays in the existing recorder; no raw credentials in the web state.
                self.namespace["logger"].warning("网页状态同步暂时不可用：" + type(exc).__name__)
            time.sleep(3)


def install(namespace, paths=None, start_heartbeat=True):
    bridge = RecorderBridge(namespace, paths)
    namespace["file_update_lock"] = bridge.config.lock
    read = namespace["read_config_value"]

    @functools.wraps(read)
    def locked_read(*args, **kwargs):
        with bridge.config.lock:
            return read(*args, **kwargs)

    namespace["read_config_value"] = locked_read
    namespace["start_record"] = bridge.wrap_start(namespace["start_record"])
    namespace["check_subprocess"] = bridge.wrap_record(namespace["check_subprocess"])
    namespace["converts_mp4"] = bridge.dispatch_single
    namespace["logger"] = LoggerProxy(namespace["logger"], bridge)
    if "spider" in namespace:
        namespace["spider"].get_youtube_stream_url = bridge.youtube_stream
    if start_heartbeat:
        threading.Thread(target=bridge.heartbeat, name="recorder-web-status", daemon=True).start()
    return bridge
