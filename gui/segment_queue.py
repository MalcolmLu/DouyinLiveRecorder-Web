"""Queue closed recording parts while the collector keeps the live session open."""
import configparser
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import time

from segment_names import filename_matcher


class ClosedSegmentScanner:
    def __init__(self, config, roots, connect, enqueue, opened, logger=None,
                 interval=5, stable_seconds=5, clock=time.time, monotonic=time.monotonic):
        self.config = Path(config)
        state = Path(os.environ.get("GUI_STATE_DIR", str(self.config / "gui/state")))
        self.runtime = state / "runtime.json"
        self.roots, self.connect, self.enqueue, self.opened = roots, connect, enqueue, opened
        self.logger = logger or logging.getLogger("h265")
        self.interval, self.stable_seconds = interval, stable_seconds
        self.clock, self.monotonic = clock, monotonic
        self.sessions, self.samples = {}, {}

    def policy(self):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.read(self.config / "config.ini", encoding="utf-8-sig")
        if not parser.has_section("录制设置"):
            return {"convert": False, "delete": False}
        section = parser["录制设置"]
        return {"convert": section.get("录制完成后自动转为mp4格式", "是") == "是",
                "delete": section.get("追加格式后删除原文件", "是") == "是"}

    @staticmethod
    def contained(path, roots):
        return any(path.is_relative_to(root) for root in roots)

    @staticmethod
    def fingerprint(path, stat):
        return hashlib.sha256(f"{path}\0{stat.st_size}\0{stat.st_mtime_ns}".encode()).hexdigest()

    def discover_directories(self, roots, match, started):
        directories = set()
        for root in roots:
            if not root.is_dir():
                continue
            for candidate in root.rglob("*"):
                try:
                    if candidate.is_file() and match(candidate.name) and candidate.stat().st_mtime >= started:
                        source = candidate.resolve(strict=True)
                        if self.contained(source, roots):
                            directories.add(source.parent)
                except FileNotFoundError:
                    continue
        return sorted(directories)

    def scan_once(self):
        try:
            runtime = json.loads(self.runtime.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return 0
        now, tick = self.clock(), self.monotonic()
        heartbeat = runtime.get("heartbeat", 0)
        if not isinstance(heartbeat, (int, float)) or not math.isfinite(heartbeat) or not 0 <= now - heartbeat < 20:
            return 0
        roots = list(dict.fromkeys(Path(root).resolve() for root in self.roots()))
        current, candidates = set(), {}
        for identifier, room in runtime.get("rooms", {}).items():
            started, pattern = room.get("started_at"), room.get("file")
            if (room.get("state") != "recording" or not isinstance(pattern, str) or not pattern
                    or not isinstance(started, (int, float)) or not math.isfinite(started) or not 0 < started <= now):
                continue
            pattern = Path(pattern).name
            if Path(pattern).suffix.lower() not in (".ts", ".mkv", ".mp4", ".flv"):
                continue
            key = (identifier, started, pattern)
            current.add(key)
            if key not in self.sessions:
                self.sessions[key] = {"policy": self.policy(), "directories": []}
            session = self.sessions[key]
            if not session["policy"]["convert"]:
                continue
            match = filename_matcher(pattern)
            directories = [path for path in session["directories"] if path.is_dir() and self.contained(path, roots)]
            if not directories:
                directories = self.discover_directories(roots, match, started)
            session["directories"] = directories
            for directory in directories:
                for candidate in directory.iterdir():
                    try:
                        if not candidate.is_file() or candidate.name.startswith(".") or not match(candidate.name):
                            continue
                        source = candidate.resolve(strict=True)
                        stat = source.stat()
                        if not self.contained(source, roots) or not stat.st_size or stat.st_mtime < started:
                            continue
                        candidates[source] = (stat, session["policy"])
                    except FileNotFoundError:
                        continue
        self.sessions = {key: value for key, value in self.sessions.items() if key in current}
        self.samples = {path: value for path, value in self.samples.items() if path in candidates}
        ready = []
        with self.connect() as db:
            for source, (stat, policy) in sorted(candidates.items(), key=lambda item: (item[1][0].st_mtime, str(item[0]))):
                known = db.execute("SELECT 1 FROM jobs WHERE id=? OR "
                                   "(status IN ('published','done') AND output=? AND output_size=? AND output_mtime=?) LIMIT 1",
                                   (self.fingerprint(source, stat), str(source), stat.st_size, stat.st_mtime_ns)).fetchone()
                if known:
                    # Failed/stopped/deleted fingerprints remain authoritative.
                    self.samples.pop(source, None)
                    continue
                signature = (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)
                previous = self.samples.get(source)
                if previous is None or previous[0] != signature:
                    self.samples[source] = (signature, tick)
                    continue
                if tick - previous[1] < self.stable_seconds or now - stat.st_mtime < 2:
                    continue
                if not self.opened(source):
                    ready.append((source, stat, policy))
        added = 0
        for source, previous, policy in ready:
            try:
                actual = source.stat()
                if (actual.st_size, actual.st_mtime_ns) != (previous.st_size, previous.st_mtime_ns) or self.opened(source):
                    continue
                self.enqueue(source, policy["delete"])
                self.samples.pop(source, None)
                added += 1
                self.logger.info("Queued closed recording segment %s", source.name)
            except FileNotFoundError:
                continue
        return added

    def run(self, stop, after_scan=None):
        while not stop.is_set():
            try:
                self.scan_once()
                if after_scan:
                    after_scan()
            except Exception as error:
                self.logger.warning("Completed segment scan will retry: %s", type(error).__name__)
            stop.wait(self.interval)
