"""Shared queue ordering and cooperative control of worker-owned subprocesses."""
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time

PROTOCOL = 1
TERMINAL = ("done", "failed", "stopped")
SORTS = {"oldest", "newest", "smallest", "largest", "name"}


class ControlError(ValueError):
    def __init__(self, message, code="conflict", status=409):
        super().__init__(message)
        self.code, self.status = code, status


class JobStopped(Exception):
    """An explicit user stop, rather than an encoding failure."""


class WorkerShutdown(Exception):
    """Release owned children and leave unfinished work available on restart."""


def migrate(db):
    # Separate tables preserve the recorder's existing jobs/fingerprint schema.
    db.execute("CREATE TABLE IF NOT EXISTS queue_control ("
               "id INTEGER PRIMARY KEY CHECK(id=1), mode TEXT NOT NULL DEFAULT 'running',"
               "control_revision INTEGER NOT NULL DEFAULT 0, order_revision INTEGER NOT NULL DEFAULT 0,"
               "stop_job_id TEXT, updated REAL NOT NULL)")
    db.execute("INSERT OR IGNORE INTO queue_control(id,updated) VALUES(1,?)", (time.time(),))
    db.execute("CREATE TABLE IF NOT EXISTS queue_order (job_id TEXT PRIMARY KEY, position INTEGER NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS queue_worker ("
               "id INTEGER PRIMARY KEY CHECK(id=1), worker_id TEXT NOT NULL, heartbeat REAL NOT NULL,"
               "phase TEXT NOT NULL, job_id TEXT, protocol INTEGER NOT NULL)")
    missing = db.execute("SELECT id FROM jobs WHERE status='queued' AND NOT EXISTS "
                         "(SELECT 1 FROM queue_order WHERE job_id=jobs.id) ORDER BY created,id").fetchall()
    if missing:
        position = db.execute("SELECT coalesce(max(position),0) FROM queue_order").fetchone()[0]
        db.executemany("INSERT INTO queue_order(job_id,position) VALUES(?,?)",
                       [(row[0], position + index + 1) for index, row in enumerate(missing)])
        db.execute("UPDATE queue_control SET order_revision=order_revision+1 WHERE id=1")


def has_schema(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='queue_control'").fetchone())


def queued_ids(db):
    if not has_schema(db):
        return [row[0] for row in db.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created,id")]
    return [row[0] for row in db.execute("SELECT jobs.id FROM jobs LEFT JOIN queue_order ON job_id=jobs.id "
                                       "WHERE status='queued' ORDER BY position IS NULL,position,created,jobs.id")]


def append_job(db, identifier):
    position = db.execute("SELECT coalesce(max(position),0)+1 FROM queue_order").fetchone()[0]
    db.execute("INSERT INTO queue_order(job_id,position) VALUES(?,?) "
               "ON CONFLICT(job_id) DO UPDATE SET position=excluded.position", (identifier, position))
    db.execute("UPDATE queue_control SET order_revision=order_revision+1 WHERE id=1")


def runtime(db, worker_id, phase, job_id=None):
    db.execute("INSERT INTO queue_worker(id,worker_id,heartbeat,phase,job_id,protocol) VALUES(1,?,?,?,?,?) "
               "ON CONFLICT(id) DO UPDATE SET worker_id=excluded.worker_id,heartbeat=excluded.heartbeat,"
               "phase=excluded.phase,job_id=excluded.job_id,protocol=excluded.protocol",
               (worker_id, time.time(), phase, job_id, PROTOCOL))


def snapshot(db):
    default = {"supported": False, "online": False, "mode": "running", "state": "offline",
               "phase": "offline", "current_job_id": None, "control_revision": 0, "order_revision": 0}
    if not has_schema(db):
        return default
    row = db.execute("SELECT * FROM queue_control WHERE id=1").fetchone()
    if not row:
        return default
    worker = db.execute("SELECT * FROM queue_worker WHERE id=1").fetchone()
    supported = bool(worker and worker["protocol"] == PROTOCOL)
    online = bool(supported and worker["phase"] != "offline" and time.time() - worker["heartbeat"] < 15)
    phase = worker["phase"] if online else "offline"
    state = phase
    if online:
        if row["mode"] == "paused" and phase != "paused":
            state = "pausing"
        elif row["mode"] == "stopped" and phase != "stopped":
            state = "stopping"
        elif row["mode"] == "running" and phase in ("paused", "stopped"):
            state = "resuming"
    return dict(default, supported=supported, online=online, mode=row["mode"], state=state, phase=phase,
                current_job_id=worker["job_id"] if online else None,
                control_revision=row["control_revision"], order_revision=row["order_revision"])


def require_ready(db):
    state = snapshot(db)
    if not state["supported"] or not state["online"] or state["phase"] == "maintenance":
        raise ControlError("转码控制进程尚未连接，请稍后刷新。", "worker_unavailable", 503)
    return state


def validate_revision(value, current):
    if type(value) is not int or value < 0:
        raise ControlError("队列版本不正确，请刷新页面。", "validation", 422)
    if value != current:
        raise ControlError("队列状态已变化，请刷新后再操作。", "stale_queue", 409)


def request_control(db, action, revision, expected_job=None):
    state = require_ready(db)
    validate_revision(revision, state["control_revision"])
    if action not in ("pause", "resume", "stop"):
        raise ControlError("不支持这个转码操作。", "validation", 422)
    if action == "resume" and state["mode"] == "stopped" and state["current_job_id"]:
        raise ControlError("当前任务正在停止，请等待收尾后再继续。")
    if action == "pause" and state["mode"] == "stopped":
        raise ControlError("转码已经停止，请使用继续转码。")
    if action == "stop" and expected_job != state["current_job_id"]:
        raise ControlError("当前任务已变化，未停止其他任务，请刷新后重试。", "stale_job", 409)
    mode = {"pause": "paused", "resume": "running", "stop": "stopped"}[action]
    if mode != state["mode"]:
        db.execute("UPDATE queue_control SET mode=?,stop_job_id=?,control_revision=control_revision+1,updated=? WHERE id=1",
                   (mode, expected_job if action == "stop" else None, time.time()))
    return snapshot(db)


def reorder(db, revision, identifier=None, direction=None, sort=None):
    state = require_ready(db)
    validate_revision(revision, state["order_revision"])
    ids = queued_ids(db)
    if sort is not None:
        if not isinstance(sort, str) or sort not in SORTS:
            raise ControlError("不支持这个排序方式。", "validation", 422)
        order = {"oldest": "created,id", "newest": "created DESC,id", "smallest": "size,created,id",
                 "largest": "size DESC,created,id", "name": "source,created,id"}[sort]
        if sort == "name":
            rows = db.execute("SELECT id,source,created FROM jobs WHERE status='queued'").fetchall()
            ids = [row[0] for row in sorted(rows, key=lambda row: (Path(row[1]).name.casefold(), row[2], row[0]))]
        else:
            ids = [row[0] for row in db.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY " + order)]
    else:
        if direction not in ("top", "up", "down", "bottom"):
            raise ControlError("移动方向不正确。", "validation", 422)
        if identifier not in ids:
            raise ControlError("只能调整等待转码的任务，当前任务可能已经开始。", "job_not_queued", 409)
        index = ids.index(identifier)
        target = {"top": 0, "up": max(0, index - 1), "down": min(len(ids) - 1, index + 1),
                  "bottom": len(ids) - 1}[direction]
        ids.insert(target, ids.pop(index))
    # Rewriting only queued ranks leaves active and completed records untouched.
    db.executemany("INSERT INTO queue_order(job_id,position) VALUES(?,?) "
                   "ON CONFLICT(job_id) DO UPDATE SET position=excluded.position",
                   [(identifier, index + 1) for index, identifier in enumerate(ids)])
    db.execute("UPDATE queue_control SET order_revision=order_revision+1 WHERE id=1")
    return snapshot(db)


class JobControl:
    """Poll persisted requests, suspending only children created by this worker."""
    def __init__(self, database, worker_id, job_id, shutdown=lambda: False):
        self.database, self.worker_id, self.job_id = database, worker_id, job_id
        self.shutdown = shutdown
        self.phase = "preparing"
        self.suspended = False
        self.last_heartbeat = 0

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def report(self, phase, force=False):
        if not force and time.monotonic() - self.last_heartbeat < 1:
            return
        with self.connect() as db:
            runtime(db, self.worker_id, phase, self.job_id)
        self.last_heartbeat = time.monotonic()

    @staticmethod
    def suspend(process, paused):
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                import psutil
                child = psutil.Process(process.pid)
                child.suspend() if paused else child.resume()
            else:
                os.kill(process.pid, signal.SIGSTOP if paused else signal.SIGCONT)
        except ProcessLookupError:
            pass

    def checkpoint(self, process=None, phase=None):
        if phase:
            self.phase = phase
        paused_since = None
        while True:
            if self.shutdown():
                raise WorkerShutdown()
            with self.connect() as db:
                row = db.execute("SELECT mode,stop_job_id FROM queue_control WHERE id=1").fetchone()
            if row["mode"] == "stopped":
                raise JobStopped()
            if row["mode"] != "paused":
                if self.suspended and process is not None:
                    self.suspend(process, False)
                    self.suspended = False
                self.report(self.phase, force=paused_since is not None)
                return time.monotonic() - paused_since if paused_since is not None else 0
            if paused_since is None:
                paused_since = time.monotonic()
                if process is not None:
                    self.suspend(process, True)
                    self.suspended = True
                self.report("paused", force=True)
            else:
                self.report("paused")
            time.sleep(.2)

    def run(self, command, check=False, timeout=None, **kwargs):
        self.checkpoint()
        process = subprocess.Popen(command, **kwargs)
        started, paused = time.monotonic(), 0
        try:
            while True:
                paused += self.checkpoint(process)
                remaining = None if timeout is None else timeout - (time.monotonic() - started - paused)
                if remaining is not None and remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    output, errors = process.communicate(timeout=min(.25, remaining) if remaining is not None else .25)
                except subprocess.TimeoutExpired:
                    continue
                if check and process.returncode:
                    raise subprocess.CalledProcessError(process.returncode, command, output, errors)
                self.checkpoint()
                return subprocess.CompletedProcess(command, process.returncode, output, errors)
        finally:
            if process.poll() is None:
                if self.suspended:
                    self.suspend(process, False)
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
            self.suspended = False

    @contextmanager
    def publication(self, connect):
        # Serialize stop requests with the final verified publication boundary.
        while True:
            self.checkpoint(phase="publishing")
            with connect() as db:
                db.execute("BEGIN IMMEDIATE")
                mode = db.execute("SELECT mode FROM queue_control WHERE id=1").fetchone()[0]
                if mode != "running":
                    continue
                yield db
                return
