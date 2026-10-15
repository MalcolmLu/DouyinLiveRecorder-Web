"""Dashboard aggregation and the recorder's shared-file integration."""
import json
from pathlib import Path
import re
import time
from shared import AppError, read_json, room_id


class DashboardService:
    def __init__(self, paths, config, queue):
        self.paths, self.config, self.queue = paths, config, queue

    def progress(self):
        current = read_json(self.paths.config / "h265/current-job.json", {})
        if not current:
            return None
        progress_file = self.paths.config / "h265/progress.txt"
        fields = {}
        if progress_file.exists() and progress_file.stat().st_mtime >= current.get("started_at", 0):
            for line in progress_file.read_text(errors="replace").splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    fields[key] = value
        try:
            seconds = float(fields.get("out_time_us", 0)) / 1_000_000
            duration = float(current.get("duration", 0))
            ratio = min(99.9, max(0, seconds / duration * 100)) if duration else None
            speed = float(fields.get("speed", "0x").rstrip("x"))
            return {"job_id": current["job_id"], "percent": round(ratio, 1) if ratio is not None else None,
                    "processed_seconds": round(seconds, 1), "duration": duration,
                    "speed": speed, "updated": progress_file.stat().st_mtime if progress_file.exists() else None}
        except (KeyError, TypeError, ValueError):
            return None

    def snapshot(self):
        runtime = read_json(self.paths.state / "runtime.json", {})
        heartbeat = runtime.get("heartbeat", 0)
        connected = time.time() - heartbeat < 20
        with self.config.lock:
            rooms = self.config.rooms()
            revision = self.config.revision()
            proxy_ready = self.config.proxy_public()["configured"]
            settings = self.config.settings()
        observed = runtime.get("rooms", {})
        for room in rooms:
            room["uses_proxy"] = self.config.uses_proxy(room)
            state = observed.get(room["id"], {})
            room.pop("_line", None)
            room.pop("_name_value", None)
            fresh = time.time() - state.get("checked_at", 0) < max(settings["actual"]["poll_seconds"] * 2 + 120, 600)
            room["state"] = ("disabled" if not room["enabled"] else
                             state.get("state", "unknown") if connected and fresh else "unknown")
            room["checked_at"] = state.get("checked_at")
            room["started_at"] = state.get("started_at")
            room["last_error"] = state.get("error", "")
            room["applied_proxy"] = state.get("applied_proxy")
            room["file"] = state.get("file")
            room["display_name"] = state.get("name") or room["name"]
        summary = self.queue.summary()
        return {
            "rooms": rooms, "revision": revision, "queue": summary, "queue_control": self.queue.control(),
            "recorder": {"connected": connected, "heartbeat": heartbeat,
                         "version": runtime.get("version", "v4.0.7"), "proxy_ready": proxy_ready},
            "recording_count": sum(r["state"] == "recording" for r in rooms),
            "monitoring_count": sum(r["enabled"] for r in rooms),
            "progress": self.progress(), "settings_pending": settings["pending"],
            "default_quality": settings["values"]["quality"],
            "updated": time.time(),
        }
