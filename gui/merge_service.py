"""Room-scoped session listing and authenticated manual merge requests."""
import hashlib
import json
from pathlib import Path
import re
import time

import merge_queue
from repository import QueueRepository
from shared import AppError


class MergeService:
    def __init__(self, paths, recordings):
        self.paths, self.recordings = paths, recordings
        self.repository = QueueRepository(paths)

    @staticmethod
    def revision(row):
        return hashlib.sha256(json.dumps([row["parts"], row["ended"], row["job_id"]]).encode()).hexdigest()

    def scoped(self, identifier, db):
        _, locations = self.recordings._context(identifier)
        if not merge_queue.has_schema(db):
            return []
        rows = db.execute("SELECT * FROM merge_sessions WHERE room_id=? ORDER BY started DESC", (identifier,)).fetchall()
        return [row for row in rows if json.loads(row["parts"]) and all(
            any(Path(part["source"]).resolve().is_relative_to(location["base"]) and
                (location["prefixes"] is None or Path(part["source"]).name.startswith(location["prefixes"]))
                for location in locations) for part in json.loads(row["parts"]))]

    def list(self, identifier):
        self.recordings._context(identifier)
        if not self.paths.queue.exists():
            return {"items": []}
        with self.repository.connect() as db:
            items = []
            for row in self.scoped(identifier, db):
                job = db.execute("SELECT status,error FROM jobs WHERE id=?", (row["job_id"],)).fetchone() if row["job_id"] else None
                merged = merge_queue.merge_job(db, row["job_id"]) if job else None
                omitted = json.loads(merged["skipped"]) if merged else merge_queue.omitted_parts(db, row)
                count = len(json.loads(merged["inputs"])) if merged else len(json.loads(row["parts"])) - len(omitted)
                status = job["status"] if job else "recording" if row["ended"] is None else "requested" if row["requested"] else "ready"
                items.append({"id": row["id"], "name": row["name"], "started": row["started"], "ended": row["ended"],
                              "part_count": count, "auto_merge": bool(row["auto_merge"]), "status": status,
                              "skipped": [Path(path).name for path in omitted],
                              "job_id": row["job_id"], "error": job["error"] if job else row["error"],
                              "can_merge": row["ended"] is not None and count >= 2 and not row["job_id"] and not row["requested"],
                              "revision": self.revision(row)})
        return {"items": items}

    def request(self, identifier, session, body):
        if not re.fullmatch(r"[0-9a-f]{64}", session):
            raise AppError("场次编号不正确。")
        with self.repository.connect(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            row = next((row for row in self.scoped(identifier, db) if row["id"] == session), None)
            if not row:
                raise AppError("找不到此直播间的录制场次。", "not_found", 404)
            if body.get("revision") != self.revision(row):
                raise AppError("场次已更新，请刷新后再合并。", "conflict", 409)
            if row["ended"] is None or len(json.loads(row["parts"])) - len(merge_queue.omitted_parts(db, row)) < 2:
                raise AppError("请等待整场录制结束，且至少有两个分 P。", "recording", 409)
            if row["job_id"] or row["requested"]:
                raise AppError("此场次已经提交合并，请查看转码队列。", "conflict", 409)
            db.execute("UPDATE merge_sessions SET requested=1,error=NULL,updated=? WHERE id=?", (time.time(), session))
