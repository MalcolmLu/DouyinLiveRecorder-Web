"""File and SQLite access; no HTTP request objects belong in this module."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import quote, unquote, urlsplit, urlunsplit
import queue_control

from platform_utils import recording_roots, recording_relative
from shared import (
    AppError, PROXY_MODES, QUALITIES, RECORD_KEYS, SharedLock, atomic_write,
    canonical_url, clean_label, platform, read_ini, read_json, room_id,
    sanitize_error, write_json,
)


class ConfigRepository:
    def __init__(self, paths):
        self.paths = paths
        self.lock = SharedLock(paths.config / ".record-web.lock")
        self.urls = paths.config / "URL_config.ini"
        self.ini = paths.config / "config.ini"
        self.metadata = paths.config / "gui/rooms.json"
        self.h265 = paths.config / "h265/settings.json"
        self.pending = paths.state / "pending-recording.json"
        self.deleted = paths.state / "deleted-rooms.json"

    @staticmethod
    def exists(path): return Path(path).exists()
    @staticmethod
    def bytes(path): return Path(path).read_bytes()
    @staticmethod
    def text(path): return Path(path).read_text(encoding="utf-8-sig")
    @staticmethod
    def write(path, value): atomic_write(path, value)
    @staticmethod
    def remove(path): Path(path).unlink()
    @staticmethod
    def json_read(path, default=None): return read_json(path, default)
    @staticmethod
    def json_write(path, value): write_json(path, value)
    @staticmethod
    def ini_read(path): return read_ini(path)



class QueueRepository:
    def __init__(self, paths):
        self.paths = paths

    @contextmanager
    def connect(self, write=False):
        if not self.paths.queue.exists():
            raise AppError("转码队列尚未创建。", "not_ready", 503)
        db = sqlite3.connect(str(self.paths.queue) if write else self.paths.queue.as_uri() + "?mode=ro",
                             timeout=5, uri=not write)
        db.row_factory = sqlite3.Row
        try:
            yield db
            if write:
                db.commit()
        finally:
            db.close()

    @staticmethod
    def migrate_deleted_jobs(db):
        # Keep the worker's fingerprint rows so scans cannot enqueue them again.
        db.execute("CREATE TABLE IF NOT EXISTS deleted_jobs(id TEXT PRIMARY KEY, deleted_at REAL NOT NULL)")

    @staticmethod
    def visible_jobs(db):
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deleted_jobs'").fetchone()
        if not exists:
            return "1"
        # An externally retried task must remain visible while it is processing.
        return "(status NOT IN ('done','failed','stopped') OR NOT EXISTS (SELECT 1 FROM deleted_jobs WHERE deleted_jobs.id=jobs.id))"

    def control(self):
        if not self.paths.queue.exists():
            return {"supported": False, "online": False, "mode": "running", "state": "offline",
                    "phase": "offline", "current_job_id": None, "control_revision": 0, "order_revision": 0}
        with self.connect() as db:
            return queue_control.snapshot(db)

    def change_control(self, action, revision, current_job_id=None):
        with self.connect(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            queue_control.migrate(db)
            try:
                return queue_control.request_control(db, action, revision, current_job_id)
            except queue_control.ControlError as exc:
                raise AppError(str(exc), exc.code, exc.status) from exc

    def reorder(self, revision, identifier=None, direction=None, sort=None):
        if identifier is not None and (not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier)):
            raise AppError("任务编号不正确。")
        with self.connect(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            queue_control.migrate(db)
            try:
                return queue_control.reorder(db, revision, identifier, direction, sort)
            except queue_control.ControlError as exc:
                raise AppError(str(exc), exc.code, exc.status) from exc

    def summary(self):
        if not self.paths.queue.exists():
            return {"queued": 0, "running": 0, "done": 0, "failed": 0, "stopped": 0, "saved_bytes": 0}
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
                return {"queued":0,"running":0,"done":0,"failed":0,"saved_bytes":0}
            states = {r[0]: r[1] for r in db.execute("SELECT status,count(*) FROM jobs WHERE " + self.visible_jobs(db) + " GROUP BY status")}
            savings = db.execute("SELECT coalesce(sum(size-output_size),0) FROM jobs WHERE status='done' AND output_size>0").fetchone()[0]
        return dict({"queued": 0, "running": 0, "done": 0, "failed": 0, "stopped": 0}, **states, saved_bytes=savings)

    def jobs(self, status, search, page, page_size=25):
        if not self.paths.queue.exists():
            return {"items": [], "total": 0, "page": page, "page_size": page_size}
        conditions, params = [], []
        if status != "all":
            conditions.append("status=?")
            params.append(status)
        if search:
            conditions.append("source LIKE ? ESCAPE '\\'")
            params.append("%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
                return {"items":[],"total":0,"page":page,"page_size":page_size}
            where = " WHERE " + " AND ".join([self.visible_jobs(db), *conditions])
            total = db.execute("SELECT count(*) FROM jobs" + where, params).fetchone()[0]
            controls = queue_control.snapshot(db)
            queued = queue_control.queued_ids(db)
            ranks = {identifier: index + 1 for index, identifier in enumerate(queued)}
            ordered = queue_control.has_schema(db)
            join = " LEFT JOIN queue_order ON job_id=jobs.id" if ordered else ""
            order = "CASE WHEN status='queued' THEN coalesce(position,9223372036854775807) END," if ordered else ""
            rows = db.execute("SELECT jobs.* FROM jobs" + join + where +
                              " ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'published' THEN 1 WHEN 'queued' THEN 2 WHEN 'stopped' THEN 3 WHEN 'failed' THEN 4 ELSE 5 END," + order +
                              "CASE WHEN status='queued' THEN created END,updated DESC,jobs.id LIMIT ? OFFSET ?",
                              [*params, page_size, (page - 1) * page_size]).fetchall()
        items = []
        for r in rows:
            row = dict(r)
            source = Path(row["source"])
            try:
                relative = str(recording_relative(source, recording_roots(self.paths.config,self.paths.downloads)))
            except ValueError:
                relative = source.name
            items.append({
                "id": row["id"], "name": source.name, "path": relative, "status": row["status"],
                "size": row["size"], "output_size": row["output_size"], "updated": row["updated"],
                "error": sanitize_error(row["error"]), "created": row["created"],
                "queue_position": ranks.get(row["id"]), "queued_total": len(queued),
                "saved_percent": round((1 - row["output_size"] / row["size"]) * 100, 1) if row["output_size"] and row["size"] else None,
            })
        return {"items": items, "total": total, "page": page, "page_size": page_size,
                "control": controls, "order_revision": controls["order_revision"]}

    def delete_record(self, identifier):
        self.delete_records([identifier])

    def delete_records(self, identifiers):
        if not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 200 or any(
                not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in identifiers):
            raise AppError("请选择 1 至 200 条有效的任务记录。")
        identifiers = list(dict.fromkeys(identifiers))
        with self.connect(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in identifiers)
            rows = db.execute("SELECT status FROM jobs WHERE id IN (" + placeholders + ")", identifiers).fetchall()
            if len(rows) != len(identifiers):
                raise AppError("找不到这个任务。", "not_found", 404)
            if any(row["status"] not in queue_control.TERMINAL for row in rows):
                raise AppError("排队中或处理中的任务暂不能删除，请等待任务结束。", "conflict", 409)
            self.migrate_deleted_jobs(db)
            deleted_at = time.time()
            db.executemany("INSERT OR IGNORE INTO deleted_jobs(id,deleted_at) VALUES(?,?)",
                           [(identifier, deleted_at) for identifier in identifiers])
        return len(identifiers)

    def retry(self, identifier):
        if not re.fullmatch(r"[0-9a-f]{64}", identifier):
            raise AppError("任务编号不正确。")
        with self.connect(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            self.migrate_deleted_jobs(db)
            queue_control.migrate(db)
            row = db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
            if not row or db.execute("SELECT 1 FROM deleted_jobs WHERE id=?", (identifier,)).fetchone():
                raise AppError("找不到这个任务。", "not_found", 404)
            if row["status"] not in ("failed", "stopped"):
                raise AppError("只能重新排队失败或已停止的任务。", "conflict", 409)
            try:
                source = Path(row["source"]).resolve(strict=True)
                recording_relative(source, recording_roots(self.paths.config,self.paths.downloads))
                stat = source.stat()
            except (OSError, ValueError):
                raise AppError("原录像不存在，暂时无法重试。", "source_missing", 409)
            if (stat.st_size, stat.st_mtime_ns) != (row["size"], row["mtime"]):
                raise AppError("原录像已变化，已保留文件，请先检查后再处理。", "source_changed", 409)
            target = source.with_suffix(".mp4")
            if target != source and target.exists():
                raise AppError("同名 MP4 已存在，已保留两个文件，请先检查。", "destination_exists", 409)
            db.execute("UPDATE jobs SET status='queued',error=NULL,updated=? WHERE id=?", (time.time(), identifier))
            queue_control.append_job(db, identifier)
