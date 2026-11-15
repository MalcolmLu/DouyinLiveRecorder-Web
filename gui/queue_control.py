"""Shared queue ordering and cooperative control of worker-owned subprocesses."""
from contextlib import contextmanager, nullcontext
import os
import json
from pathlib import Path
import re
import signal
import shutil
import sqlite3
import subprocess
import time
import uuid
import job_metrics
from platform_utils import acquire_lock, release_lock

PROTOCOL = 4
TERMINAL = ("done", "failed", "stopped")
INTERRUPTIBLE_PHASES = ("preparing", "encoding", "verifying")
ENCODER_PHASES = {"starting", "preparing", "encoding"}
PARALLEL_TABLES = ('queue_parallel_worker', 'queue_parallel_worker2')
MAX_TASKS = 3
SORTS = {"oldest", "newest", "smallest", "largest", "name", "name_desc", "mtime_asc", "mtime_desc"}


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
    columns = {row[1] for row in db.execute("PRAGMA table_info(queue_control)")}
    if "priority_job_id" not in columns:
        db.execute("ALTER TABLE queue_control ADD COLUMN priority_job_id TEXT")
    if "max_workers" not in columns:
        # Existing installations keep their single-worker behavior until enabled.
        db.execute("ALTER TABLE queue_control ADD COLUMN max_workers INTEGER NOT NULL DEFAULT 1")
    if "parallel_wait_reason" not in columns:
        db.execute("ALTER TABLE queue_control ADD COLUMN parallel_wait_reason TEXT")
    db.execute("CREATE TABLE IF NOT EXISTS queue_order (job_id TEXT PRIMARY KEY, position INTEGER NOT NULL)")
    for table in ("queue_worker", "queue_priority_worker", *PARALLEL_TABLES):
        db.execute("CREATE TABLE IF NOT EXISTS " + table + " ("
                   "id INTEGER PRIMARY KEY CHECK(id=1), worker_id TEXT NOT NULL, heartbeat REAL NOT NULL,"
                   "phase TEXT NOT NULL, job_id TEXT, protocol INTEGER NOT NULL)")
    job_metrics.migrate(db)
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


def runtime(db, worker_id, phase, job_id=None, *, priority=False, parallel=False):
    if priority and parallel:
        raise ValueError('A worker cannot occupy both secondary roles')
    if parallel not in (False, True, 1, 2):
        raise ValueError('Unknown parallel worker slot')
    table = "queue_priority_worker" if priority else PARALLEL_TABLES[int(parallel)-1] if parallel else "queue_worker"
    job_metrics.record(db, worker_id, phase, job_id, worker_table=table)
    db.execute("INSERT INTO " + table + "(id,worker_id,heartbeat,phase,job_id,protocol) VALUES(1,?,?,?,?,?) "
               "ON CONFLICT(id) DO UPDATE SET worker_id=excluded.worker_id,heartbeat=excluded.heartbeat,"
               "phase=excluded.phase,job_id=excluded.job_id,protocol=excluded.protocol",
               (worker_id, time.time(), phase, job_id, PROTOCOL))


def snapshot(db):
    default = {"supported": False, "online": False, "mode": "running", "state": "offline",
               "phase": "offline", "current_job_id": None, "control_revision": 0, "order_revision": 0,
               "priority_job_id": None, "suspended_job_id": None, "priority_worker_active": False,
               "parallel_job_id": None, "parallel2_job_id": None, "workers": [], "max_workers": 1, "parallel_wait_reason": None}
    if not has_schema(db):
        return default
    row = db.execute("SELECT * FROM queue_control WHERE id=1").fetchone()
    if not row:
        return default
    worker = db.execute("SELECT * FROM queue_worker WHERE id=1").fetchone()
    supported = bool(worker and worker["protocol"] == PROTOCOL)
    online = bool(supported and worker["phase"] != "offline" and time.time() - worker["heartbeat"] < 15)
    phase = worker["phase"] if online else "offline"
    current = worker["job_id"] if online else None
    priority = row["priority_job_id"] if "priority_job_id" in row.keys() else None
    parallel = parallel2 = None
    workers = []
    if online and current:
        workers.append({"role": "primary", "job_id": current, "phase": phase})
    for slot, table in enumerate(PARALLEL_TABLES, 1):
        if not online or not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            continue
        child = db.execute("SELECT * FROM " + table + " WHERE id=1").fetchone()
        if (child and child["job_id"] and child["protocol"] == PROTOCOL and child["phase"] != "offline"
                and 0 <= time.time() - child["heartbeat"] < 15
                and priority_status(db, child["job_id"]) in ("running", "published")):
            identifier = child["job_id"]
            if slot == 1:
                parallel = identifier
            else:
                parallel2 = identifier
            workers.append({"role": "parallel" if slot == 1 else "parallel2", "job_id": identifier, "phase": child["phase"]})
            if not current:
                current, phase = identifier, child["phase"]
    suspended = current if online and phase == "priority_wait" else None
    priority_worker_active = False
    if online and db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='queue_priority_worker'").fetchone():
        child = db.execute("SELECT * FROM queue_priority_worker WHERE id=1").fetchone()
        identifier = child["job_id"] if child else None
        active = priority_status(db, identifier) in ("running", "published") if identifier else False
        if (active and child and child["protocol"] == PROTOCOL
                and child["phase"] != "offline" and 0 <= time.time() - child["heartbeat"] < 15):
            # A priority worker can finish encoding and continue verifying after
            # the marker is released. Keep that live worker visible so the
            # console can show the encoder and verifier running side by side.
            if identifier == priority:
                suspended = current if current != identifier else None
            else:
                suspended = None
            phase, current = child["phase"], identifier
            priority_worker_active = True
            workers = [worker for worker in workers if worker["job_id"] != identifier]
            workers.append({"role": "priority", "job_id": identifier, "phase": phase})
    state = phase
    if online:
        if row["mode"] == "paused":
            state = "pausing" if any(w["phase"] != "paused" for w in workers) or (not workers and phase != "paused") else "paused"
        elif row["mode"] == "stopped":
            state = "stopping" if workers or phase != "stopped" else "stopped"
        elif row["mode"] == "running" and phase in ("paused", "stopped"):
            state = "resuming"
    return dict(default, supported=supported, online=online, mode=row["mode"], state=state, phase=phase,
                current_job_id=current, suspended_job_id=suspended, priority_worker_active=priority_worker_active,
                control_revision=row["control_revision"], order_revision=row["order_revision"],
                priority_job_id=priority, parallel_job_id=parallel, parallel2_job_id=parallel2, workers=workers,
                max_workers=row["max_workers"] if "max_workers" in row.keys() else 1,
                parallel_wait_reason=row['parallel_wait_reason'] if 'parallel_wait_reason' in row.keys() else None)


def set_max_workers(db, value, revision):
    state = require_ready(db)
    validate_revision(revision, state['control_revision'])
    if type(value) is not int or value not in (1, 2, 3):
        raise ControlError('处理任务数只能为 1、2 或 3。', 'validation', 422)
    if value != state['max_workers']:
        db.execute('UPDATE queue_control SET max_workers=?,parallel_wait_reason=NULL,control_revision=control_revision+1,updated=? WHERE id=1',
                   (value, time.time()))
    return snapshot(db)


def live_demand(db):
    """Reserve slots for active live work and work that can actually start."""
    counts = dict(db.execute("SELECT status,count(*) FROM jobs WHERE status IN ('queued','running','published') GROUP BY status"))
    active = counts.get('running', 0) + counts.get('published', 0)
    queued = int(bool(counts.get('queued', 0)))
    control = db.execute('SELECT * FROM queue_control WHERE id=1').fetchone()
    limit = control['max_workers'] if 'max_workers' in control.keys() else 1
    marker = control['priority_job_id'] if 'priority_job_id' in control.keys() else None
    if marker and priority_status(db, marker) in ('queued', 'running', 'published'):
        state = snapshot(db)
        workers = {w['job_id']: w for w in state.get('workers', [])}
        safe = {'paused', 'priority_wait', 'waiting_priority', 'waiting_prerequisites', 'stopped'}
        running = {row[0] for row in db.execute("SELECT id FROM jobs WHERE status IN ('running','published')")}
        active = sum(identifier not in workers or workers[identifier]['phase'] not in safe for identifier in running)
        return min(MAX_TASKS, max(1, active))
    # Reserve at most one queued live job: it may use the encoder only after
    # the already-running live jobs have finished encoding.
    if active and queued and active < MAX_TASKS and limit > active:
        waiting = control['parallel_wait_reason'] if 'parallel_wait_reason' in control.keys() else None
        state = snapshot(db)
        running_ids = {row[0] for row in db.execute("SELECT id FROM jobs WHERE status IN ('running','published')")}
        phases = {worker['job_id']: worker['phase'] for worker in state['workers']}
        ready = all(phases.get(identifier) in ('verifying', 'publishing', 'finalizing') for identifier in running_ids)
        queued = int(ready and waiting not in ('memory', 'storage') and
                     next_runnable(db, include_published=False) is not None)
    return min(MAX_TASKS, max(active, min(limit, active + queued)))


def job_resources(db, identifier):
    """Conservatively identify paths a transcode or merge may read or publish."""
    row = db.execute('SELECT source,delete_source FROM jobs WHERE id=?', (identifier,)).fetchone()
    if not row:
        raise ControlError('找不到这个任务。', 'not_found', 404)
    paths = [Path(row['source'])]
    merge = None
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='merge_jobs'").fetchone():
        merge = db.execute('SELECT inputs,destination FROM merge_jobs WHERE id=?', (identifier,)).fetchone()
    if merge:
        paths += [Path(p['path']) for p in json.loads(merge['inputs'])]
        paths.append(Path(merge['destination']))
    else:
        source = paths[0]
        if not row['delete_source'] and source.suffix.lower() == '.mp4':
            paths += [source.with_name(source.stem + '.H265.mp4'),
                      source.with_name(source.stem + '.converted.mp4')]
        paths += [source.with_name(source.stem + '.H265-' + identifier[:8] + suffix)
                  for suffix in ('.mp4', '.mkv')]
    return bool(merge), {os.path.normcase(str(p.resolve(strict=False).parent / p.stem)) for p in paths}


def conflicts_with_active(db, identifier, *, exclusive_merge=True):
    merge, resources = job_resources(db, identifier)
    candidate = db.execute('SELECT status FROM jobs WHERE id=?', (identifier,)).fetchone()
    if not candidate:
        raise ControlError('找不到这个任务。', 'not_found', 404)
    # A pending publication blocks new work that uses the same files. A pending
    # publication itself may be finalized as long as no worker owns a conflict.
    pending = " OR status='published'" if candidate['status'] == 'queued' else ''
    active_ids = {row[0] for row in db.execute("SELECT id FROM jobs WHERE id<>? AND (status='running'" + pending + ")", (identifier,))}
    if candidate['status'] == 'published':
        for table in ('queue_worker', 'queue_priority_worker', *PARALLEL_TABLES):
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                worker = db.execute('SELECT job_id,phase FROM ' + table + ' WHERE id=1').fetchone()
                if worker and worker['job_id'] and worker['job_id'] != identifier and worker['phase'] != 'offline':
                    active_ids.add(worker['job_id'])
    for active in active_ids:
        other_merge, other = job_resources(db, active)
        # Merges can change their destination if a part disappeared. Run them alone.
        if exclusive_merge and (merge or other_merge) or resources.intersection(other):
            return True
    return False


def next_runnable(db, *, include_published=True):
    statuses = "('published','queued')" if include_published else "('queued')"
    rows = db.execute("SELECT jobs.* FROM jobs LEFT JOIN queue_order ON job_id=jobs.id WHERE status IN " + statuses +
                      " ORDER BY CASE status WHEN 'published' THEN 0 ELSE 1 END,position IS NULL,position,created,jobs.id").fetchall()
    return next((row for row in rows if not conflicts_with_active(db, row['id'])), None)


def next_primary_runnable(db, *, priority_ready=False):
    """Only start the next encoder after existing jobs reach verification."""
    control = db.execute('SELECT max_workers,priority_job_id FROM queue_control WHERE id=1').fetchone()
    candidate = next_runnable(db)
    if not candidate or control['priority_job_id'] and priority_ready:
        return candidate
    owners = {}
    for table in ('queue_priority_worker', *PARALLEL_TABLES):
        worker = db.execute('SELECT job_id,phase FROM ' + table + ' WHERE id=1').fetchone()
        if worker and worker['job_id'] and worker['phase'] != 'offline':
            owners[worker['job_id']] = worker['phase']
    if candidate['status'] == 'published':
        return None if candidate['id'] in owners else candidate
    active = {row[0] for row in db.execute("SELECT id FROM jobs WHERE status IN ('running','published')")}
    if len(active) >= control['max_workers']:
        return None
    if any(owners.get(identifier) not in ('verifying', 'publishing', 'finalizing') for identifier in active):
        return None
    return candidate


def parallel_resource_reason(db, job, *, cgroup=Path('/sys/fs/cgroup'), meminfo=Path('/proc/meminfo')):
    """Avoid starting a second job when current memory or disk headroom is low."""
    try:
        maximum = (cgroup / 'memory.max').read_text().strip()
        if maximum != 'max':
            used = int((cgroup / 'memory.current').read_text().strip())
            values = dict(line.split()[:2] for line in (cgroup / 'memory.stat').read_text().splitlines())
            reclaimable = int(values.get('inactive_file', 0))
            available = int(maximum) - max(0, used - reclaimable)
            if available < 768 * 1024**2:
                return 'memory'
    except (OSError, ValueError, TypeError):
        pass  # Non-Linux and unlimited environments do not expose cgroup v2.
    try:
        available = next(int(line.split()[1]) * 1024 for line in meminfo.read_text().splitlines()
                         if line.startswith('MemAvailable:'))
        if available < 1024**3:
            return 'memory'
    except (OSError, ValueError, StopIteration):
        pass
    try:
        source = Path(job['source'])
        free = shutil.disk_usage(source.parent).free
        device = source.parent.stat().st_dev
        active = 0
        for row in db.execute("SELECT source,size FROM jobs WHERE status IN ('running','published') AND id<>?", (job['id'],)):
            try:
                if Path(row['source']).parent.stat().st_dev == device:
                    active += max(0, row['size'] or 0)
            except OSError:
                continue
        if free < 2 * (active + max(0, job['size'] or 0)) + 512 * 1024**2:
            return 'storage'
    except (OSError, TypeError):
        pass  # The worker will report missing or invalid sources normally.
    return None


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
    if action == "resume" and state["mode"] == "stopped" and (state["current_job_id"] or state["workers"]):
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
        if sort in ("name", "name_desc"):
            rows = db.execute("SELECT id,source,created FROM jobs WHERE status='queued'").fetchall()
            rows = sorted(rows, key=lambda row: (row[2], row[0]))
            ids = [row[0] for row in sorted(rows, key=lambda row: Path(row[1]).name.casefold(),
                                            reverse=sort == "name_desc")]
        else:
            order = {"oldest": "created,id", "newest": "created DESC,id", "smallest": "size,created,id",
                     "largest": "size DESC,created,id", "mtime_asc": "mtime,created,id",
                     "mtime_desc": "mtime DESC,created,id"}[sort]
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
    # An accepted start-now request remains first even if another page reorders.
    priority = state.get("priority_job_id")
    if priority in ids:
        ids.remove(priority)
        ids.insert(0, priority)
    # Rewriting only queued ranks leaves active and completed records untouched.
    db.executemany("INSERT INTO queue_order(job_id,position) VALUES(?,?) "
                   "ON CONFLICT(job_id) DO UPDATE SET position=excluded.position",
                   [(identifier, index + 1) for index, identifier in enumerate(ids)])
    db.execute("UPDATE queue_control SET order_revision=order_revision+1 WHERE id=1")
    return snapshot(db)


def start_now(db, identifier, revision):
    """Promote a waiting job and request suspension at the next safe checkpoint."""
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise ControlError("任务编号不正确。", "validation", 422)
    state = require_ready(db)
    validate_revision(revision, state["order_revision"])
    if state["mode"] != "running":
        raise ControlError("请先继续转码，再选择优先任务。", "queue_not_running", 409)
    row = db.execute("SELECT status FROM jobs WHERE id=?", (identifier,)).fetchone()
    if not row:
        raise ControlError("找不到这个任务。", "not_found", 404)
    if row["status"] != "queued":
        raise ControlError("只有排队中的任务可以立即开始。", "job_not_queued", 409)
    if conflicts_with_active(db, identifier, exclusive_merge=False):
        raise ControlError("该任务与正在处理的视频或合并任务使用相同文件，请等待当前任务结束。", "job_conflict", 409)
    active_priority = state.get("priority_job_id")
    if active_priority:
        active = db.execute("SELECT status FROM jobs WHERE id=?", (active_priority,)).fetchone()
        if active and active["status"] in ("queued", "running", "published"):
            raise ControlError("已有优先任务正在等待或处理，请等待它结束。", "priority_busy", 409)
    ids = queued_ids(db)
    if identifier not in ids:
        raise ControlError("任务已经开始或在处理中，请刷新后重试。", "job_not_queued", 409)
    ids.remove(identifier)
    ids.insert(0, identifier)
    db.executemany("INSERT INTO queue_order(job_id,position) VALUES(?,?) "
                   "ON CONFLICT(job_id) DO UPDATE SET position=excluded.position",
                   [(job_id, index + 1) for index, job_id in enumerate(ids)])
    db.execute("UPDATE queue_control SET priority_job_id=?,order_revision=order_revision+1,updated=? WHERE id=1",
               (identifier, time.time()))
    return snapshot(db)


def priority_status(db, identifier):
    """Return the stored job state for a promoted identifier, or None."""
    row = db.execute("SELECT status FROM jobs WHERE id=?", (identifier,)).fetchone()
    return row["status"] if row else None


def release_priority(db):
    """Clear the marker only after the promoted job reached a terminal state."""
    marker = db.execute("SELECT priority_job_id FROM queue_control WHERE id=1").fetchone()
    identifier = marker["priority_job_id"] if marker else None
    if identifier and priority_status(db, identifier) not in ("queued", "running", "published"):
        clear_priority(db, identifier)


def clear_priority(db, identifier):
    db.execute("UPDATE queue_control SET priority_job_id=NULL,updated=? "
               "WHERE id=1 AND priority_job_id=?", (time.time(), identifier))


def claim_priority(db, identifier):
    """Claim from the shared queue only while this is still the selected job."""
    db.execute("BEGIN IMMEDIATE")
    row = db.execute("SELECT mode,priority_job_id FROM queue_control WHERE id=1").fetchone()
    if row["mode"] == "stopped" or row["priority_job_id"] != identifier:
        return None
    claimed = db.execute("UPDATE jobs SET status='running',updated=? WHERE id=? AND status='queued'",
                         (time.time(), identifier))
    if claimed.rowcount != 1:
        return None
    db.execute("UPDATE queue_control SET order_revision=order_revision+1 WHERE id=1")
    return dict(db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone())


def claim_reserved_parallel(db, identifier, token, slot=1):
    """Accept only the reservation created by the current supervising worker."""
    if slot not in (1, 2):
        raise ValueError('Unknown parallel worker slot')
    table = PARALLEL_TABLES[slot-1]
    row = db.execute('SELECT jobs.* FROM jobs JOIN '+table+' ON '+table+'.job_id=jobs.id '
                     "WHERE jobs.id=? AND jobs.status='running' AND "+table+".worker_id=? "
                     "AND "+table+".phase='starting'", (identifier, token)).fetchone()
    return dict(row) if row else None


def wait_for_secondary_exit(home, shutdown=lambda: False, timeout=30):
    """Do not requeue stale jobs while an older secondary process can still run."""
    from platform_utils import acquire_lock
    for name in ('parallel-worker.lock', 'parallel-worker2.lock', 'priority-worker.lock'):
        with (Path(home) / name).open('a') as lock:
            until = time.monotonic() + timeout
            while True:
                try:
                    acquire_lock(lock, blocking=False)
                    break
                except BlockingIOError:
                    if shutdown() or time.monotonic() >= until:
                        raise WorkerShutdown('Waiting for an older secondary worker')
                    time.sleep(.1)


@contextmanager
def priority_lifetime(parent_variable="DLR_PRIORITY_PARENT_PID"):
    stopped = [False]
    parent = int(os.environ.get(parent_variable, "0"))
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, lambda *_: stopped.__setitem__(0, True))
    try:
        yield lambda: stopped[0] or bool(parent and os.getppid() != parent)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


class ParallelManager:
    """Supervise one isolated secondary worker for the lifetime of the queue worker."""
    def __init__(self, database, command, log, admission=None, slot=1):
        if slot not in (1, 2):
            raise ValueError('Unknown parallel worker slot')
        self.database, self.command, self.log = Path(database), command, Path(log)
        self.admission = admission
        self.slot, self.table = slot, PARALLEL_TABLES[slot-1]
        self.process = self.identifier = self.token = self.log_handle = None
        self.closing = False
        self.last_attempt = 0

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def reap(self):
        if self.process is None or self.process.poll() is None:
            return
        code, identifier, token = self.process.returncode, self.identifier, self.token
        # If the worker was killed unexpectedly, its FFmpeg process must not
        # outlive its lease and race a retry.
        if code and os.name != 'nt':
            try:
                # The crashed supervisor cannot reap its encoder. Terminate
                # the whole owned group even if an orphan ignores SIGTERM.
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.process = self.identifier = self.token = None
        if self.log_handle is not None:
            self.log_handle.close(); self.log_handle = None
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM jobs WHERE id=?', (identifier,)).fetchone()
            mode = db.execute('SELECT mode FROM queue_control WHERE id=1').fetchone()[0]
            if row and row['status'] == 'running':
                status = 'stopped' if mode == 'stopped' else 'queued' if self.closing else 'failed'
                error = None if status != 'failed' else f'并行转码进程异常退出（{code}）'
                db.execute('UPDATE jobs SET status=?,error=?,updated=? WHERE id=? AND status=\'running\'',
                           (status, error, time.time(), identifier))
            slot = db.execute('SELECT worker_id FROM '+self.table+' WHERE id=1').fetchone()
            if slot and slot['worker_id'] == token:
                runtime(db, token, 'offline', parallel=self.slot)

    def maybe_start(self):
        self.reap()
        if self.process is not None or self.closing:
            return
        if time.monotonic() - self.last_attempt < 1:
            return
        self.last_attempt = time.monotonic()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            control = db.execute('SELECT mode,max_workers,priority_job_id FROM queue_control WHERE id=1').fetchone()
            primary = db.execute('SELECT phase,job_id FROM queue_worker WHERE id=1').fetchone()
            if (control['mode'] != 'running' or control['max_workers'] <= self.slot or control['priority_job_id']
                    or not primary or not primary['job_id'] or primary['phase'] != 'verifying'):
                return
            active = {row[0] for row in db.execute("SELECT id FROM jobs WHERE status IN ('running','published')")}
            owners = {primary['job_id']: primary['phase']}
            for table in PARALLEL_TABLES:
                worker = db.execute('SELECT job_id,phase FROM '+table+' WHERE id=1').fetchone()
                if worker and worker['job_id'] and worker['phase'] != 'offline':
                    owners[worker['job_id']] = worker['phase']
            if len(active) >= control['max_workers'] or any(
                    owners.get(identifier) not in ('verifying', 'publishing', 'finalizing') for identifier in active):
                return
            # The historical queue checks the live queue while holding its own
            # claim transaction. Live work takes the same lock before claiming.
            job = next_runnable(db, include_published=False)
            if not job:
                db.execute('UPDATE queue_control SET parallel_wait_reason=NULL WHERE id=1 AND parallel_wait_reason IS NOT NULL')
                return
            resource_reason = parallel_resource_reason(db, job)
            if resource_reason:
                db.execute('UPDATE queue_control SET parallel_wait_reason=? WHERE id=1 AND parallel_wait_reason IS NOT ?',
                           (resource_reason, resource_reason))
                return
            # Clear a prior resource block before calculating demand. If
            # history still occupies the slot, the admission check changes
            # this to waiting_history so history will yield before we claim.
            db.execute('UPDATE queue_control SET parallel_wait_reason=NULL WHERE id=1 AND parallel_wait_reason IS NOT NULL')
            with (self.admission(db) if self.admission is not None else nullcontext(None)) as reason:
                if reason is not None:
                    db.execute('UPDATE queue_control SET parallel_wait_reason=? WHERE id=1 AND parallel_wait_reason IS NOT ?',
                               (reason, reason))
                    return
                identifier, token = job['id'], 'parallel:' + uuid.uuid4().hex
                changed = db.execute("UPDATE jobs SET status='running',updated=? WHERE id=? AND status='queued'",
                                     (time.time(), identifier))
                if changed.rowcount != 1:
                    return
                db.execute('UPDATE queue_control SET order_revision=order_revision+1 WHERE id=1')
                runtime(db, token, 'starting', identifier, parallel=self.slot)
        self.identifier, self.token = identifier, token
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.log_handle = self.log.open('ab')
        kwargs = {'stdin': subprocess.DEVNULL, 'stdout': self.log_handle, 'stderr': subprocess.STDOUT,
                  'close_fds': True, 'env': dict(os.environ, DLR_PARALLEL_PARENT_PID=str(os.getpid()),
                                               DLR_PARALLEL_TOKEN=token, DLR_PARALLEL_SLOT=str(self.slot))}
        if os.name != 'nt':
            kwargs['start_new_session'] = True
        try:
            self.process = subprocess.Popen([*self.command, identifier], **kwargs)
        except OSError as exc:
            self.log_handle.close(); self.log_handle = None
            self.identifier = self.token = None
            with self.connect() as db:
                db.execute("UPDATE jobs SET status='failed',error=?,updated=? WHERE id=? AND status='running'",
                           (f'无法启动并行转码进程：{exc}', time.time(), identifier))
                runtime(db, token, 'offline', parallel=self.slot)

    def safe_for_priority(self):
        self.reap()
        if self.process is None:
            return True
        with self.connect() as db:
            slot = db.execute('SELECT * FROM '+self.table+' WHERE id=1').fetchone()
            status = db.execute('SELECT status FROM jobs WHERE id=?', (self.identifier,)).fetchone()
            return bool(status and (status['status'] not in ('running', 'published') or
                        slot and slot['worker_id'] == self.token and slot['phase'] in ('paused', 'priority_wait')
                        and 0 <= time.time() - slot['heartbeat'] < 15))

    def close(self):
        self.closing = True
        if self.process is not None and self.process.poll() is None:
            if os.name != 'nt':
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                if os.name != 'nt':
                    try: os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                else:
                    self.process.kill()
                self.process.wait(timeout=5)
        self.reap()


class ParallelManagers:
    """Coordinate both secondary slots without starting encoders together."""
    def __init__(self, managers):
        self.managers = tuple(managers)

    def reap(self):
        for manager in self.managers:
            manager.reap()

    def maybe_start(self):
        for manager in self.managers:
            manager.maybe_start()

    def safe_for_priority(self):
        return all(manager.safe_for_priority() for manager in self.managers)

    def close(self):
        for manager in self.managers:
            manager.close()


class HistoryYieldGate:
    """Wait for historical FFmpeg children to suspend before starting live work."""
    def __init__(self, database):
        self.database = Path(database)
        self.connection = None

    def close(self):
        if self.connection is not None:
            self.connection.close(); self.connection = None

    @staticmethod
    def inspect(db, allowed_active=0):
        running = {row[0] for row in db.execute("SELECT id FROM jobs WHERE status='running'")}
        state = snapshot(db)
        if running and not state['online']:
            return 'waiting_history'
        workers = {w['job_id']: w for w in state.get('workers', [])}
        safe = {'paused', 'priority_wait', 'waiting_priority', 'waiting_prerequisites', 'stopped'}
        if any(identifier not in workers for identifier in running):
            return 'waiting_history'
        # Verification, publication and finalization do not hold the shared
        # encoder. They may overlap with live encoding under the three-task
        # budget; only a history worker that is about to encode or encoding
        # must yield the single encoder.
        active = sum(w['phase'] in ENCODER_PHASES
                     and priority_status(db, w['job_id']) in ('running', 'published')
                     for w in workers.values())
        if active > allowed_active:
            return 'waiting_history'
        return None

    def reason(self, allowed_active=0):
        if not self.database.exists():
            self.close(); return None
        try:
            if self.connection is None:
                self.connection = sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True,
                                                  timeout=.2, isolation_level=None)
                self.connection.row_factory = sqlite3.Row
                self.connection.execute('PRAGMA query_only=ON')
            return self.inspect(self.connection, allowed_active)
        except (OSError, sqlite3.Error, KeyError):
            self.close()
            return 'waiting_history'

    @contextmanager
    def admission(self, allowed_active=0, *, commit=None):
        """Hold the historical claim lock through the live reservation commit."""
        if not self.database.exists():
            yield None
            return
        db = None
        try:
            db = sqlite3.connect(self.database.as_uri() + '?mode=rw', uri=True, timeout=.2)
            db.row_factory = sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            reason = self.inspect(db, allowed_active)
        except (OSError, sqlite3.Error, KeyError):
            if db is not None: db.close()
            yield 'waiting_history'
            return
        try:
            yield reason
            if reason is None and commit is not None:
                # History must observe the committed live reservation as soon
                # as it can acquire its own claim lock, including on WAL DBs.
                commit.commit()
        finally:
            db.rollback(); db.close()


class JobControl:
    """Poll persisted requests, suspending only children created by this worker."""
    def __init__(self, database, worker_id, job_id, shutdown=lambda: False,
                 priority_command=None, priority_log=None, report_runtime=True, priority_worker=False,
                 parallel_worker=False, parallel_manager=None, encoder_lock=None):
        self.database, self.worker_id, self.job_id = database, worker_id, job_id
        self.shutdown = shutdown
        self.phase = "preparing"
        self.suspended = False
        self.waiting = False
        self.last_heartbeat = 0
        self.priority_command = priority_command
        self.priority_log = priority_log
        self.priority_process = None
        self.priority_process_id = None
        self.priority_log_handle = None
        self.report_runtime = report_runtime
        self.priority_worker = priority_worker
        self.parallel_worker = parallel_worker
        self.parallel_manager = parallel_manager
        self.encoder_lock = Path(encoder_lock) if encoder_lock is not None else None
        self._encoder_file = None
        self._encoder_locked = False

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
        if not self.report_runtime:
            return
        if not force and time.monotonic() - self.last_heartbeat < 1:
            return
        with self.connect() as db:
            runtime(db, self.worker_id, phase, self.job_id, priority=self.priority_worker,
                    parallel=self.parallel_worker)
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

    def _start_priority_worker(self, identifier):
        if self.priority_process is not None and self.priority_process.poll() is None:
            return
        self._finish_priority_worker()
        if not self.priority_command:
            return
        command = [*self.priority_command, identifier]
        if self.priority_log:
            self.priority_log.parent.mkdir(parents=True, exist_ok=True)
            self.priority_log_handle = self.priority_log.open("ab")
        kwargs = {"stdin": subprocess.DEVNULL, "close_fds": True,
                  "env": dict(os.environ, DLR_PRIORITY_PARENT_PID=str(os.getpid()))}
        if self.priority_log_handle is not None:
            kwargs["stdout"] = self.priority_log_handle
            kwargs["stderr"] = subprocess.STDOUT
        if os.name != "nt":
            kwargs["start_new_session"] = True
        try:
            self.priority_process = subprocess.Popen(command, **kwargs)
        except OSError as exc:
            if self.priority_log_handle is not None:
                self.priority_log_handle.close()
                self.priority_log_handle = None
            with self.connect() as db:
                db.execute("UPDATE jobs SET status='failed',error=?,updated=? WHERE id=? AND status='queued'",
                           (f"无法启动优先转码进程：{exc}", time.time(), identifier))
                clear_priority(db, identifier)
            return None
        self.priority_process_id = identifier
        # The priority process owns its execution; the parent only supervises it.
        return self.priority_process

    def _finish_priority_worker(self):
        if self.priority_process is not None and self.priority_process.poll() is not None:
            identifier = self.priority_process_id
            returncode = self.priority_process.returncode
            if returncode and os.name != 'nt':
                # An unexpectedly exited priority worker cannot reap an
                # encoder that survived it. Free the slot before clearing it.
                try:
                    os.killpg(self.priority_process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.priority_process = None
            self.priority_process_id = None
            if self.priority_log_handle is not None:
                self.priority_log_handle.close()
                self.priority_log_handle = None
            if identifier is not None:
                with self.connect() as db:
                    row = db.execute("SELECT status FROM jobs WHERE id=?", (identifier,)).fetchone()
                    marker = db.execute("SELECT priority_job_id FROM queue_control WHERE id=1").fetchone()
                    if marker and marker["priority_job_id"] == identifier:
                        if row and row["status"] in ("queued", "running"):
                            db.execute("UPDATE jobs SET status='failed',error=?,updated=? WHERE id=?",
                                       (f"优先转码进程异常退出（{returncode}）", time.time(), identifier))
                        clear_priority(db, identifier)

    def _stop_priority_worker(self):
        if self.priority_process is not None and self.priority_process.poll() is None:
            if os.name != "nt":
                # The worker terminates its own paused child without resuming it.
                try:
                    os.killpg(self.priority_process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                self.priority_process.terminate()
            try:
                self.priority_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    try:
                        os.killpg(self.priority_process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    self.priority_process.kill()
                self.priority_process.wait(timeout=5)
        self._finish_priority_worker()

    def close(self):
        self._stop_priority_worker()

    def wait_reason(self, mode, waiting_priority):
        if mode == "paused":
            return "paused"
        return "priority_wait" if waiting_priority else None

    def wait_phase(self, reason):
        return reason

    def _release_encoder(self):
        if self._encoder_locked:
            release_lock(self._encoder_file)
            self._encoder_locked = False

    def _close_encoder(self):
        if self._encoder_file is not None:
            try:
                self._release_encoder()
            finally:
                self._encoder_file.close()
                self._encoder_file = None

    @staticmethod
    def _wait_suspended(process):
        """Confirm Linux delivered SIGSTOP before another encoder can start."""
        status = Path('/proc') / str(process.pid) / 'status'
        if os.name == 'nt' or not Path('/proc/self/status').exists():
            return True
        deadline = time.monotonic() + .5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return True
            try:
                state = next(line for line in status.read_text().splitlines() if line.startswith('State:'))
            except (OSError, StopIteration):
                if process.poll() is not None:
                    return True
            else:
                if state.split()[1] in ('T', 't'):
                    return True
            time.sleep(.02)
        return False

    def checkpoint(self, process=None, phase=None):
        phase_changed = phase is not None and phase != self.phase
        if phase:
            self.phase = phase
        paused_since = None
        while True:
            if self.shutdown():
                self._stop_priority_worker()
                raise WorkerShutdown()
            if self.parallel_manager is not None:
                self.parallel_manager.reap()
            self._finish_priority_worker()
            with self.connect() as db:
                row = db.execute("SELECT mode,stop_job_id,priority_job_id FROM queue_control WHERE id=1").fetchone()
            if row["mode"] == "stopped":
                self._stop_priority_worker()
                raise JobStopped()
            priority = row["priority_job_id"]
            waiting_priority = self.priority_process is not None or self._priority_is_active(priority)
            reason = self.wait_reason(row["mode"], waiting_priority)
            if reason is None:
                if self._encoder_file is not None and not self._encoder_locked:
                    try:
                        acquire_lock(self._encoder_file, blocking=False)
                    except BlockingIOError:
                        new_wait = paused_since is None
                        if new_wait:
                            paused_since = time.monotonic()
                        self.waiting = True
                        if process is not None and not self.suspended:
                            self.suspend(process, True)
                            self.suspended = True
                            self._wait_suspended(process)
                        self.report('waiting_encoder', force=new_wait)
                        time.sleep(.2)
                        continue
                    self._encoder_locked = True
                    # Recheck queue and live priority after acquiring the lock.
                    continue
                if self.suspended and process is not None:
                    self.suspend(process, False)
                    self.suspended = False
                self.waiting = False
                self.report(self.phase, force=phase_changed or paused_since is not None)
                if self.parallel_manager is not None and self.phase in INTERRUPTIBLE_PHASES:
                    self.parallel_manager.maybe_start()
                return time.monotonic() - paused_since if paused_since is not None else 0
            new_wait = paused_since is None
            self.waiting = True
            if new_wait:
                paused_since = time.monotonic()
            # Freeze the current subprocess before spawning another encoder.
            if process is not None and not self.suspended:
                self.suspend(process, True)
                self.suspended = True
            if process is not None and self._encoder_locked and not self._wait_suspended(process):
                # Hold the lock until the kernel confirms suspension; retry
                # control requests instead of discarding the in-flight video.
                self.report(self.phase)
                continue
            self._release_encoder()
            self.report(self.wait_phase(reason), force=new_wait)
            if (waiting_priority and row["mode"] == "running" and priority and priority != self.job_id
                    and (self.parallel_manager is None or self.parallel_manager.safe_for_priority())):
                self._start_priority_worker(priority)
            time.sleep(.2)

    def _priority_is_active(self, priority):
        if not priority or priority == self.job_id or self.phase not in INTERRUPTIBLE_PHASES:
            return False
        with self.connect() as db:
            status = db.execute("SELECT status FROM jobs WHERE id=?", (priority,)).fetchone()
        return bool(status and status["status"] in ("queued", "running", "published"))

    def run(self, command, check=False, timeout=None, **kwargs):
        process = None
        if self.phase == 'encoding' and self.encoder_lock is not None:
            self.encoder_lock.parent.mkdir(parents=True, exist_ok=True)
            self._encoder_file = self.encoder_lock.open('a+')
        try:
            self.checkpoint()
            if self._encoder_file is not None and os.name != 'nt':
                # The child retains the lock if its supervising worker dies.
                kwargs['pass_fds'] = tuple(kwargs.get('pass_fds', ())) + (self._encoder_file.fileno(),)
            process = subprocess.Popen(command, **kwargs)
            started, paused = time.monotonic(), 0
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
                self._close_encoder()
                self.checkpoint()
                return subprocess.CompletedProcess(command, process.returncode, output, errors)
        finally:
            if process is not None and process.poll() is None:
                if self.suspended:
                    # Do not resume an encoder after yielding its lock.
                    process.kill()
                else:
                    process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
            self.suspended = False
            self._close_encoder()

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
                priority = db.execute("SELECT priority_job_id FROM queue_control WHERE id=1").fetchone()[0]
                if self._priority_is_active(priority):
                    continue
                yield db
                return
