"""Persist active task time without counting queue, pause or priority waits."""
import time


ACTIVE_PHASES = {'preparing', 'encoding', 'verifying', 'publishing', 'finalizing'}


def migrate(db):
    db.execute('CREATE TABLE IF NOT EXISTS job_timings ('
               'job_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, seconds REAL NOT NULL, '
               'last_tick REAL NOT NULL, active INTEGER NOT NULL)')


def record(db, worker_id, phase, job_id, *, worker_table='queue_worker'):
    if worker_table not in ('queue_worker', 'queue_priority_worker', 'queue_parallel_worker', 'queue_parallel_worker2'):
        raise ValueError('Unknown worker table')
    tick = time.monotonic()
    previous = db.execute('SELECT worker_id,job_id FROM ' + worker_table + ' WHERE id=1').fetchone()
    if previous and previous[1]:
        # A new worker must never charge downtime after a crash to the old task.
        db.execute('UPDATE job_timings SET seconds=seconds+CASE WHEN active=1 AND worker_id=? '
                   'AND ?=? THEN max(0,?-last_tick) ELSE 0 END,last_tick=?,active=0 WHERE job_id=?',
                   (worker_id, previous[0], worker_id, tick, tick, previous[1]))
    if not job_id:
        return
    same_task = previous and tuple(previous) == (worker_id, job_id)
    existing = db.execute('SELECT 1 FROM job_timings WHERE job_id=?', (job_id,)).fetchone()
    if not same_task and phase != 'finalizing':
        # Requeued work starts a new attempt, rather than inheriting failed work.
        db.execute('INSERT INTO job_timings VALUES(?,?,0,?,?) ON CONFLICT(job_id) DO UPDATE SET '
                   'worker_id=excluded.worker_id,seconds=0,last_tick=excluded.last_tick,active=excluded.active',
                   (job_id, worker_id, tick, int(phase in ACTIVE_PHASES)))
    elif existing:
        db.execute('UPDATE job_timings SET worker_id=?,last_tick=?,active=? WHERE job_id=?',
                   (worker_id, tick, int(phase in ACTIVE_PHASES), job_id))


def read(db, identifiers):
    if not identifiers or not db.execute("SELECT 1 FROM sqlite_master WHERE name='job_timings'").fetchone():
        return {}
    workers = []
    for table in ('queue_worker', 'queue_priority_worker', 'queue_parallel_worker', 'queue_parallel_worker2'):
        if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            worker = db.execute('SELECT worker_id,job_id,heartbeat,phase FROM ' + table + ' WHERE id=1').fetchone()
            if worker and worker[3] in ACTIVE_PHASES and 0 <= time.time() - worker[2] < 15:
                workers.append(tuple(worker[:2]))
    tick = time.monotonic()
    rows = db.execute('SELECT job_id,worker_id,seconds,last_tick,active,status FROM job_timings '
                      'JOIN jobs ON jobs.id=job_timings.job_id WHERE job_id IN (' +
                      ','.join('?' for _ in identifiers) + ')', identifiers)
    return {row[0]: round(row[2] + (max(0, tick - row[3]) if row[4] and
            (row[1], row[0]) in workers and row[5] in ('running', 'published') else 0), 3)
            for row in rows}
