"""Dashboard aggregation and the recorder's shared-file integration."""
import json
from pathlib import Path
import re
import time
from shared import AppError, read_json, room_id


class DashboardService:
    def __init__(self, paths, config, queue):
        self.paths, self.config, self.queue = paths, config, queue

    def progress(self, control=None, *, suspended=False, job_id=None):
        control = control or {}
        expected = job_id or control.get('suspended_job_id' if suspended else 'current_job_id')
        if suspended and not expected:
            return None
        home = self.paths.config / 'h265'
        role = next((w.get('role') for w in control.get('workers', []) if w.get('job_id') == expected), None)
        if role == 'parallel2' or expected and expected == control.get('parallel2_job_id'):
            home = home / 'parallel2-state'
        elif role == 'parallel' or expected and expected == control.get('parallel_job_id'):
            home = home / 'parallel-state'
        elif role == 'priority' or expected and expected == control.get('priority_job_id') and control.get('priority_worker_active'):
            home = home / 'priority-state'
        current = read_json(home / 'current-job.json', {})
        if not current or (expected and current.get('job_id') != expected):
            return None
        phase = next((w.get('phase') for w in control.get('workers', []) if w.get('job_id') == expected), None)
        state = read_json(home / 'verification-state.json', {})
        state_time = state.get('updated')
        state_current = (state.get('job_id') == expected and isinstance(state_time, (int, float)) and
                         state_time >= current.get('started_at', 0))
        if phase == 'verifying' or (state_current and phase in ('paused', 'priority_wait', 'waiting_priority', 'waiting_encoder')):
            stage = state.get('stage') if state_current else None
            progress_file = home / 'verification-progress.txt'
            fields = {}
            try:
                if (stage == 'output_decode' and progress_file.exists() and
                        progress_file.stat().st_mtime >= state.get('updated', 0)):
                    for line in progress_file.read_text(errors='replace').splitlines():
                        if '=' in line:
                            key, value = line.split('=', 1); fields[key] = value
                duration = float(current.get('duration', 0))
                seconds = float(fields['out_time_us']) / 1_000_000 if 'out_time_us' in fields else None
                percent = min(99.9, max(0, seconds / duration * 100)) if duration and seconds is not None else None
                speed = float(fields.get('speed', '0x').rstrip('x'))
                return {'job_id': current['job_id'], 'kind': 'verification', 'stage': stage,
                        'percent': round(percent, 1) if percent is not None else None,
                        'processed_seconds': round(seconds, 1) if seconds is not None else None,
                        'duration': duration, 'speed': speed,
                        'updated': progress_file.stat().st_mtime if fields else state.get('updated')}
            except (OSError, KeyError, TypeError, ValueError):
                return {'job_id': current.get('job_id'), 'kind': 'verification', 'stage': stage,
                        'percent': None, 'processed_seconds': None, 'duration': None, 'speed': 0, 'updated': None}
        progress_file = home / 'progress.txt'
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
            return {"job_id": current["job_id"], "kind": "encoding", "percent": round(ratio, 1) if ratio is not None else None,
                    "processed_seconds": round(seconds, 1), "duration": duration,
                    "speed": speed, "updated": progress_file.stat().st_mtime if progress_file.exists() else None}
        except (KeyError, TypeError, ValueError):
            return None

    def progress_by_job(self, control):
        result = {}
        for worker in (control or {}).get('workers', []):
            identifier = worker.get('job_id')
            progress = self.progress(control, job_id=identifier) if identifier else None
            if progress:
                result[identifier] = progress
        return result

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
        control = self.queue.control()
        return {
            "rooms": rooms, "revision": revision, "queue": summary, "queue_control": control,
            "recorder": {"connected": connected, "heartbeat": heartbeat,
                         "version": runtime.get("version", "v4.0.7"), "proxy_ready": proxy_ready},
            "recording_count": sum(r["state"] == "recording" for r in rooms),
            "monitoring_count": sum(r["enabled"] for r in rooms),
            "progress": self.progress(control), "suspended_progress": self.progress(control, suspended=True),
            "progress_by_job": self.progress_by_job(control),
            "settings_pending": settings["pending"],
            "default_quality": settings["values"]["quality"],
            "updated": time.time(),
        }
