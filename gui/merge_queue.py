"""Persistent recording sessions and merge requests shared by worker and web UI."""
import configparser
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

import queue_control
from segment_names import filename_matcher

STAMP = re.compile(r"^(.*)_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})(?:\.H265|\.converted)?\.(?:mp4|ts|mkv|flv)$")


def migrate(db):
    db.execute("CREATE TABLE IF NOT EXISTS merge_sessions (id TEXT PRIMARY KEY,room_id TEXT NOT NULL,"
               "name TEXT NOT NULL,started REAL NOT NULL,ended REAL,pattern TEXT NOT NULL,"
               "parts TEXT NOT NULL DEFAULT '[]',auto_merge INTEGER NOT NULL,requested INTEGER NOT NULL DEFAULT 0,"
               "job_id TEXT,error TEXT,origin TEXT NOT NULL DEFAULT 'live',updated REAL NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS merge_jobs (id TEXT PRIMARY KEY,session_id TEXT NOT NULL UNIQUE,"
               "inputs TEXT NOT NULL,destination TEXT NOT NULL,delete_parts INTEGER NOT NULL,verification TEXT,skipped TEXT NOT NULL DEFAULT '[]')")


def has_schema(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='merge_jobs'").fetchone())


def merge_job(db, identifier):
    return db.execute("SELECT * FROM merge_jobs WHERE id=?", (identifier,)).fetchone() if has_schema(db) else None


def session_id(room, started, pattern):
    return hashlib.sha256(f"{room}\0{started}\0{pattern}".encode()).hexdigest()


def file_snapshot(path, roots):
    path = Path(path)
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not any(resolved.is_relative_to(Path(root).resolve()) for root in roots):
        raise ValueError("分 P 路径不在录像目录内。")
    stat = resolved.stat()
    if not resolved.is_file() or not stat.st_size or resolved.suffix.lower() not in (".mp4", ".ts", ".mkv", ".flv"):
        raise ValueError("分 P 文件不可用。")
    return {"path": str(resolved), "size": stat.st_size, "mtime": stat.st_mtime_ns}


def omitted_parts(db, session):
    omitted = []
    for part in json.loads(session["parts"]):
        if Path(part["source"]).exists():
            continue
        job = db.execute("SELECT status,output FROM jobs WHERE id=?", (part["job_id"],)).fetchone() if part.get("job_id") else None
        if job and (job["status"] in ("queued", "running", "published") or (job["output"] and Path(job["output"]).exists())):
            continue
        omitted.append(part["source"])
    return omitted


def ready_inputs(db, session, roots, opened=lambda path: False):
    result = []
    omitted = omitted_parts(db, session)
    for part in json.loads(session["parts"]):
        if part["source"] in omitted:
            continue
        path = Path(part["source"])
        if part.get("job_id"):
            job = db.execute("SELECT * FROM jobs WHERE id=?", (part["job_id"],)).fetchone()
            if not job or job["status"] != "done":
                raise ValueError("等待同场全部分 P 转码成功。")
            path = Path(job["output"] or "")
            snapshot = file_snapshot(path, roots)
            if (snapshot["size"], snapshot["mtime"]) != (job["output_size"], job["output_mtime"]):
                raise ValueError("分 P 转码输出已变化，已保留原文件。")
        else:
            snapshot = file_snapshot(path, roots)
            if part.get("convert"):
                raise ValueError("等待同场全部分 P 进入转码队列。")
        if opened(path):
            raise ValueError("分 P 仍在录制或处理中。")
        result.append(snapshot)
    if len(result) < 2 or len({p["path"] for p in result}) != len(result):
        raise ValueError("同场至少需要两个不同的分 P。")
    if len({str(Path(p["path"]).parent) for p in result}) != 1:
        raise ValueError("同场分 P 必须位于同一录像文件夹。")
    return sorted(result, key=lambda p: p["path"])


def enqueue_merge(db, session, inputs, delete_parts):
    encoded = json.dumps(inputs, sort_keys=True)
    identifier = hashlib.sha256(("merge\0" + session["id"] + "\0" + encoded).encode()).hexdigest()
    first = Path(inputs[0]["path"])
    match = STAMP.fullmatch(first.name)
    stem = match[1] + "_" + match[2] if match else first.stem
    suffix = first.suffix if len({Path(p["path"]).suffix.lower() for p in inputs}) == 1 else ".mkv"
    skipped = omitted_parts(db, session)
    destination = first.with_name(stem + ("_合并_缺%dP" % len(skipped) if skipped else "_完整") + suffix)
    if destination.exists():
        raise ValueError("完整录像的文件名已存在，已保留所有分 P。")
    now = time.time()
    db.execute("INSERT INTO merge_jobs(id,session_id,inputs,destination,delete_parts,skipped) VALUES(?,?,?,?,?,?)",
               (identifier, session["id"], encoded, str(destination), int(delete_parts), json.dumps(skipped)))
    db.execute("INSERT INTO jobs(id,source,size,mtime,delete_source,h264,status,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
               (identifier, str(destination), sum(p["size"] for p in inputs), 0, int(delete_parts), 0, "queued", now, now))
    queue_control.append_job(db, identifier)
    db.execute("UPDATE merge_sessions SET job_id=?,error=NULL,updated=? WHERE id=?", (identifier, now, session["id"]))
    return identifier


class MergeManager:
    def __init__(self, config, roots, connect, opened, clock=time.time, is_hevc=None):
        self.config, self.roots, self.connect, self.opened, self.clock = Path(config), roots, connect, opened, clock
        self.runtime = Path(os.environ.get("GUI_STATE_DIR", str(self.config / "gui/state"))) / "runtime.json"
        self.directories = {}
        self.is_hevc = is_hevc or self.probe_hevc
        self.codecs = {}
        with self.connect() as db:
            migrate(db)

    def settings(self):
        try:
            return json.loads((self.config / "h265/settings.json").read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {}

    @staticmethod
    def probe_hevc(path):
        raw = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                       "stream=codec_name", "-of", "json", str(path)], stderr=subprocess.PIPE, timeout=60)
        return json.loads(raw).get("streams", [{}])[0].get("codec_name") == "hevc"

    def policy(self):
        ini = configparser.ConfigParser(interpolation=None, strict=False)
        ini.read(self.config / "config.ini", encoding="utf-8-sig")
        return ini.get("录制设置", "录制完成后自动转为mp4格式", fallback="是") == "是"

    def auto_for(self, identifier):
        try:
            rooms = json.loads((self.config / "gui/rooms.json").read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return True
        return next((meta.get("auto_merge", True) for url, meta in rooms.items()
                     if hashlib.sha256(url.encode()).hexdigest()[:20] == identifier), True)

    def refresh_parts(self, db, session, roots):
        parts = {p["source"]: p for p in json.loads(session["parts"])}
        match = filename_matcher(session["pattern"])
        limit = (session["ended"] or self.clock()) + 5
        jobs = db.execute("SELECT * FROM jobs WHERE mtime>=? AND mtime<=? ORDER BY created",
                          (int((session["started"] - 1) * 1e9), int(limit * 1e9))).fetchall()
        for job in jobs:
            source = Path(job["source"])
            if match(source.name) and any(source.resolve().is_relative_to(root) for root in roots):
                parts[str(source)] = {"source": str(source), "job_id": job["id"]}
        directories = self.directories.get(session["id"], set())
        directories.update(Path(p).parent for p in parts)
        if not directories:
            for root in roots:
                if root.is_dir():
                    for source in root.rglob("*"):
                        if source.is_file() and match(source.name) and source.stat().st_mtime >= session["started"] - 1:
                            if source.resolve().is_relative_to(root):
                                directories.add(source.parent.resolve())
        self.directories[session["id"]] = directories
        for directory in directories:
            if not directory.is_dir():
                continue
            for source in directory.iterdir():
                try:
                    if source.is_symlink() or not source.is_file() or not match(source.name):
                        continue
                    stat = source.stat()
                    if not session["started"] - 1 <= stat.st_mtime <= limit or self.opened(source):
                        continue
                    if str(source) not in parts:
                        parts[str(source)] = {"source": str(source), "convert": self.policy()}
                except FileNotFoundError:
                    continue
        # A raw part may have been queued since the previous scan.
        for source, part in parts.items():
            if not part.get("job_id"):
                row = next((job for job in reversed(jobs) if job["source"] == source), None)
                if row:
                    parts[source] = {"source": source, "job_id": row["id"]}
        encoded = json.dumps(sorted(parts.values(), key=lambda p: p["source"]))
        db.execute("UPDATE merge_sessions SET parts=?,updated=? WHERE id=?", (encoded, self.clock(), session["id"]))

    def scan_once(self):
        try:
            runtime = json.loads(self.runtime.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return
        now = self.clock()
        if not 0 <= now - runtime.get("heartbeat", 0) < 20 and not runtime.get("stopped"):
            return  # Loss of heartbeat alone does not prove that a live session ended.
        current, roots = [], [Path(root).resolve() for root in self.roots()]
        settings = self.settings()
        with self.connect() as db:
            for room, value in runtime.get("rooms", {}).items():
                started, pattern = value.get("started_at"), value.get("file")
                if value.get("state") != "recording" or not isinstance(started, (int, float)) or not pattern:
                    continue
                identifier = session_id(room, started, pattern)
                previous = db.execute("SELECT * FROM merge_sessions WHERE room_id=? AND pattern=? AND job_id IS NULL "
                                      "AND origin='live' ORDER BY started DESC LIMIT 1", (room, Path(pattern).name)).fetchone()
                if previous and previous["started"] <= started and (
                        (previous["ended"] is None and now - previous["updated"] < 120) or
                        (previous["ended"] is not None and 0 <= started - previous["ended"] <= 120)):
                    identifier = previous["id"]
                    db.execute("UPDATE merge_sessions SET ended=NULL,updated=? WHERE id=?", (now, identifier))
                current.append(identifier)
                db.execute("INSERT OR IGNORE INTO merge_sessions(id,room_id,name,started,pattern,auto_merge,updated) VALUES(?,?,?,?,?,?,?)",
                           (identifier, room, value.get("name") or Path(pattern).stem, started, Path(pattern).name,
                            int(self.auto_for(room)), now))
            for row in db.execute("SELECT * FROM merge_sessions WHERE ended IS NULL AND origin='live'").fetchall():
                if row["id"] not in current:
                    db.execute("UPDATE merge_sessions SET ended=?,updated=? WHERE id=?", (now, now, row["id"]))
            for row in db.execute("SELECT * FROM merge_sessions WHERE job_id IS NULL").fetchall():
                if row["origin"] == "live":
                    self.refresh_parts(db, row, roots)
                if row["ended"] is None:
                    continue
                if not row["requested"] and (not settings.get("merge_parts", True) or not row["auto_merge"]):
                    continue
                row = db.execute("SELECT * FROM merge_sessions WHERE id=?", (row["id"],)).fetchone()
                try:
                    inputs = ready_inputs(db, row, roots, self.opened)
                    if row["requested"] != 1:
                        skipped = omitted_parts(db, row)
                        parts = [part for part in json.loads(row["parts"]) if part["source"] not in skipped]
                        if any(not part.get("job_id") for part in parts):
                            raise ValueError("自动合并等待同场全部分 P 成功转为 H.265。")
                        completed = [db.execute("SELECT updated FROM jobs WHERE id=?", (part["job_id"],)).fetchone()[0] for part in parts]
                        if now < max(row["ended"], *completed) + 120:
                            continue
                        for part in inputs:
                            key = (part["path"], part["size"], part["mtime"])
                            if key not in self.codecs:
                                self.codecs[key] = self.is_hevc(part["path"])
                            if not self.codecs[key]:
                                raise ValueError("自动合并等待同场全部分 P 成功转为 H.265。")
                    enqueue_merge(db, row, inputs, settings.get("delete_merged_parts", True))
                except (ValueError, OSError) as error:
                    db.execute("UPDATE merge_sessions SET error=? WHERE id=?", (str(error), row["id"]))


def seed_sessions(db, entries, roots):
    """Import a reviewed historical plan; no recordings are changed here."""
    migrate(db)
    for entry in entries:
        for part in entry["parts"]:
            # Already converted parts may refer to removed TS sources.
            if not any(Path(part["source"]).resolve().is_relative_to(Path(root).resolve()) for root in roots):
                raise ValueError("Historical part is outside the recording roots")
        db.execute("INSERT OR IGNORE INTO merge_sessions(id,room_id,name,started,ended,pattern,parts,auto_merge,requested,origin,updated) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (entry["id"], entry["room_id"], entry["name"], entry["started"], entry["ended"],
                   entry.get("pattern", ""), json.dumps(entry["parts"]), int(entry.get("auto_merge", True)),
                   int(entry.get("requested", False)), entry.get("origin", "historical"), time.time()))
