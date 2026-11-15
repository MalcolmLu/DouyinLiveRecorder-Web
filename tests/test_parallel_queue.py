"""Exercise concurrent child processes and the real persisted queue controls."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'gui'))
import queue_control
import job_metrics


@unittest.skipUnless(os.name != 'nt', 'Uses POSIX process suspension')
class ParallelQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dlr-parallel-test-')
        self.root = Path(self.tmp.name)
        self.dbpath = self.root / 'queue.sqlite3'
        self.ids = [c * 64 for c in 'abc']
        with self.db() as db:
            db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,source TEXT,size INTEGER,mtime INTEGER,delete_source INTEGER,h264 INTEGER,status TEXT,error TEXT,output TEXT,output_size INTEGER,output_mtime INTEGER,created REAL,updated REAL)')
            db.executemany('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', [
                (identifier, str(self.root / (name + '.flv')), 10, 1, 1, 0, status, None, None, None, None, i, i)
                for i,(identifier,name,status) in enumerate(zip(self.ids, ('a','b','c'), ('running','queued','queued')))])
            queue_control.migrate(db)
            db.execute('UPDATE queue_control SET max_workers=2')
            queue_control.runtime(db, 'primary', 'verifying', self.ids[0])
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.primary_count = self.root / 'primary-count'
        self.second_count = self.root / 'second-count'
        self.second_pid = self.root / 'second-pid'
        self.priority_started = self.root / 'priority-started'
        # Readers must observe completed snapshots, including while a child is
        # being paused; write_text alone briefly exposes an empty file.
        self.counter = ('import pathlib,sys,time\np=pathlib.Path(sys.argv[1]);n=0\n'
                        'while True:\n n+=1;t=p.with_suffix(".next");t.write_text(str(n));'
                        't.replace(p);time.sleep(.03)\n')
        self.child = self.root / 'parallel_child.py'
        self.child.write_text('''import os,sqlite3,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import queue_control
database,counter,pidfile,script,identifier=sys.argv[2:]
token=os.environ['DLR_PARALLEL_TOKEN']
with sqlite3.connect(database) as db:
 db.row_factory=sqlite3.Row
 assert queue_control.claim_reserved_parallel(db,identifier,token)
with queue_control.priority_lifetime('DLR_PARALLEL_PARENT_PID') as shutdown:
 c=queue_control.JobControl(database,token,identifier,shutdown,parallel_worker=True)
 c.report('encoding',force=True)
 Path(pidfile).write_text(str(os.getpid()))
 try:
  c.run([sys.executable,'-c',script,counter],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 except queue_control.JobStopped:
  with sqlite3.connect(database) as db: db.execute("UPDATE jobs SET status='stopped' WHERE id=?",(identifier,))
 except queue_control.WorkerShutdown:
  with sqlite3.connect(database) as db: db.execute("UPDATE jobs SET status='queued' WHERE id=?",(identifier,))
 finally:
  c.close()
  with sqlite3.connect(database) as db: queue_control.runtime(db,token,'offline',parallel=True)
''')
        self.priority = self.root / 'priority_child.py'
        self.priority.write_text('''import sqlite3,sys,time\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nimport queue_control\ndatabase,marker,identifier=sys.argv[2:]\nwith sqlite3.connect(database) as db:\n db.row_factory=sqlite3.Row\n assert queue_control.claim_priority(db,identifier)\n queue_control.runtime(db,'priority-child','encoding',identifier,priority=True)\nPath(marker).write_text('running')\ntime.sleep(.9)\nwith sqlite3.connect(database) as db:\n db.execute("UPDATE jobs SET status='done' WHERE id=?",(identifier,))\n queue_control.runtime(db,'priority-child','offline',priority=True)\n queue_control.clear_priority(db,identifier)\n''')
        self.manager = queue_control.ParallelManager(
            self.dbpath, [sys.executable, str(self.child), str(ROOT / 'gui'), str(self.dbpath),
                          str(self.second_count), str(self.second_pid), self.counter], self.root / 'parallel.log')
        self.control = queue_control.JobControl(
            self.dbpath, 'primary', self.ids[0], self.stop.is_set, parallel_manager=self.manager,
            priority_command=[sys.executable, str(self.priority), str(ROOT / 'gui'), str(self.dbpath),
                              str(self.priority_started)], priority_log=self.root / 'priority.log')
        # The first synthetic process represents verification, so the second
        # task may start its encoder without running two encoders together.
        self.control.phase = 'verifying'

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.dbpath, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def wait(self, predicate, label, timeout=10):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if predicate(): return
            time.sleep(.03)
        self.fail('Timed out: ' + label)

    def start(self):
        self.future = self.pool.submit(self.control.run, [sys.executable, '-c', self.counter, str(self.primary_count)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wait(lambda:self.primary_count.exists() and self.second_count.exists() and self.second_pid.exists(), 'both processes')

    def state(self):
        with self.db() as db: return queue_control.snapshot(db)

    def tearDown(self):
        self.stop.set()
        if hasattr(self, 'future'):
            try: self.future.result(timeout=15)
            except (queue_control.WorkerShutdown, queue_control.JobStopped): pass
        self.control.close(); self.manager.close(); self.pool.shutdown(wait=True)
        self.tmp.cleanup()

    def test_two_tasks_pause_resume_and_priority_preserve_both_processes(self):
        self.start()
        self.wait(lambda:len(self.state()['workers']) == 2, 'two visible workers')
        with self.db() as db:
            timings = job_metrics.read(db, self.ids[:2])
        self.assertEqual(set(timings), set(self.ids[:2]))
        pids = self.manager.process.pid, self.second_pid.read_text()
        before = int(self.primary_count.read_text()), int(self.second_count.read_text())
        time.sleep(.3)
        self.assertGreater(int(self.primary_count.read_text()), before[0])
        self.assertGreater(int(self.second_count.read_text()), before[1])
        with self.db() as db:
            state = queue_control.snapshot(db)
            queue_control.request_control(db, 'pause', state['control_revision'])
        self.wait(lambda:self.state()['state'] == 'paused', 'both paused')
        frozen = self.primary_count.read_text(), self.second_count.read_text()
        time.sleep(.4)
        self.assertEqual((self.primary_count.read_text(), self.second_count.read_text()), frozen)
        with self.db() as db:
            state = queue_control.snapshot(db)
            queue_control.request_control(db, 'resume', state['control_revision'])
        self.wait(lambda:self.primary_count.read_text()!=frozen[0] and self.second_count.read_text()!=frozen[1], 'both resumed')
        with self.db() as db:
            state = queue_control.snapshot(db)
            queue_control.start_now(db, self.ids[2], state['order_revision'])
        self.wait(lambda:self.priority_started.exists(), 'priority worker')
        self.wait(lambda:all(w['phase']=='priority_wait' for w in self.state()['workers'] if w['role']!='priority'), 'both yielded')
        frozen = self.primary_count.read_text(), self.second_count.read_text()
        time.sleep(.3)
        self.assertEqual((self.primary_count.read_text(), self.second_count.read_text()), frozen)
        self.wait(lambda:self.state()['priority_job_id'] is None, 'priority completion')
        self.wait(lambda:self.primary_count.read_text()!=frozen[0] and self.second_count.read_text()!=frozen[1], 'both resumed after priority')
        self.assertEqual((self.manager.process.pid, self.second_pid.read_text()), pids)

    def test_stop_ends_both_active_tasks_and_keeps_queued_work(self):
        self.start()
        self.wait(lambda:len(self.state()['workers']) == 2, 'two visible workers')
        child = self.manager.process
        self.assertIsNotNone(child)
        with self.db() as db:
            state = queue_control.snapshot(db)
            queue_control.request_control(db, 'stop', state['control_revision'], state['current_job_id'])
        with self.assertRaises(queue_control.JobStopped): self.future.result(timeout=15)
        with self.db() as db:
            db.execute("UPDATE jobs SET status='stopped' WHERE id=?", (self.ids[0],))
        self.wait(lambda:child.poll() is not None, 'parallel stopped')
        self.manager.reap()
        self.assertIsNone(self.manager.process)
        with self.db() as db:
            statuses = [db.execute('SELECT status FROM jobs WHERE id=?',(i,)).fetchone()[0] for i in self.ids]
        self.assertEqual(statuses, ['stopped','stopped','queued'])

    def test_same_destination_and_merges_do_not_overlap(self):
        with self.db() as db:
            db.execute('UPDATE jobs SET source=? WHERE id=?', (str(self.root/'a.ts'), self.ids[1]))
            self.assertTrue(queue_control.conflicts_with_active(db, self.ids[1]))
            self.assertEqual(queue_control.next_runnable(db, include_published=False)['id'], self.ids[2])
            with self.assertRaises(queue_control.ControlError):
                queue_control.start_now(db, self.ids[1], queue_control.snapshot(db)['order_revision'])
            db.execute('CREATE TABLE merge_jobs(id TEXT,inputs TEXT,destination TEXT)')
            db.execute('INSERT INTO merge_jobs VALUES(?,?,?)', (self.ids[2], '[]', str(self.root/'merged.mp4')))
            self.assertIsNone(queue_control.next_runnable(db, include_published=False))

    def test_concurrency_limit_is_revision_guarded(self):
        with self.db() as db:
            state = queue_control.snapshot(db)
            after = queue_control.set_max_workers(db, 1, state['control_revision'])
            self.assertEqual(after['max_workers'], 1)
            with self.assertRaises(queue_control.ControlError):
                queue_control.set_max_workers(db, 2, state['control_revision'])
            with self.assertRaises(queue_control.ControlError):
                queue_control.set_max_workers(db, 4, after['control_revision'])

    def test_lowered_limit_waits_for_existing_parallel_task_before_claiming_another(self):
        with self.db() as db:
            db.execute("UPDATE jobs SET status='done' WHERE id=?", (self.ids[0],))
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (self.ids[1],))
            queue_control.runtime(db, 'primary', 'idle')
            queue_control.runtime(db, 'parallel:test', 'encoding', self.ids[1], parallel=True)
            db.execute('UPDATE queue_control SET max_workers=1')
            self.assertIsNone(queue_control.next_primary_runnable(db))
            db.execute("UPDATE jobs SET status='published' WHERE id=?", (self.ids[1],))
            queue_control.runtime(db, 'parallel:test', 'publishing', self.ids[1], parallel=True)
            self.assertIsNone(queue_control.next_primary_runnable(db))
            db.execute("UPDATE jobs SET status='done' WHERE id=?", (self.ids[1],))
            self.assertEqual(queue_control.next_primary_runnable(db)['id'], self.ids[2])

    def test_lowered_limit_still_allows_a_priority_task_after_parallel_yields(self):
        with self.db() as db:
            db.execute("UPDATE jobs SET status='done' WHERE id=?", (self.ids[0],))
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (self.ids[1],))
            queue_control.runtime(db, 'primary', 'idle')
            queue_control.runtime(db, 'parallel:test', 'priority_wait', self.ids[1], parallel=True)
            db.execute('UPDATE queue_control SET max_workers=1,priority_job_id=?', (self.ids[2],))
            self.assertIsNone(queue_control.next_primary_runnable(db,priority_ready=False))
            self.assertEqual(queue_control.next_primary_runnable(db,priority_ready=True)['id'], self.ids[2])

    def test_idle_primary_still_exposes_parallel_progress_owner(self):
        with self.db() as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (self.ids[1],))
            queue_control.runtime(db, 'primary', 'idle')
            queue_control.runtime(db, 'parallel:test', 'verifying', self.ids[1], parallel=True)
            state = queue_control.snapshot(db)
        self.assertEqual(state['current_job_id'], self.ids[1])
        self.assertEqual(state['parallel_job_id'], self.ids[1])
        self.assertEqual(state['workers'], [{'role':'parallel', 'job_id':self.ids[1], 'phase':'verifying'}])

    def test_priority_verifier_stays_visible_after_marker_clear(self):
        with self.db() as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (self.ids[1],))
            queue_control.runtime(db, 'primary', 'priority_wait', self.ids[0])
            queue_control.runtime(db, 'priority:test', 'verifying', self.ids[1], priority=True)
            state = queue_control.snapshot(db)
        self.assertIsNone(state['priority_job_id'])
        self.assertTrue(state['priority_worker_active'])
        self.assertEqual((state['current_job_id'], state['phase']), (self.ids[1], 'verifying'))
        self.assertIsNone(state['suspended_job_id'])
        self.assertIn({'role':'primary', 'job_id':self.ids[0], 'phase':'priority_wait'}, state['workers'])
        self.assertIn({'role':'priority', 'job_id':self.ids[1], 'phase':'verifying'}, state['workers'])

    def test_progress_files_are_read_from_each_workers_own_directory(self):
        from services import DashboardService
        from shared import Paths, write_json
        config = self.root / 'config'; home = config / 'h265'; second = home / 'parallel-state'
        second.mkdir(parents=True)
        for directory, identifier, seconds in [(home, self.ids[0], 25), (second, self.ids[1], 75)]:
            write_json(directory / 'current-job.json', {'job_id':identifier,'started_at':0,'duration':100})
            (directory / 'progress.txt').write_text('out_time_us='+str(seconds*1000000)+'\nspeed=1x\n')
        with self.db() as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (self.ids[1],))
            queue_control.runtime(db, 'parallel:test', 'encoding', self.ids[1], parallel=True)
            state = queue_control.snapshot(db)
        dashboard = DashboardService(Paths(config, self.root, self.root/'state'), None, None)
        progress = dashboard.progress_by_job(state)
        # The primary is already verifying and has no verification progress
        # file. Its old encoder percentage must not be presented as a check.
        self.assertIsNone(progress[self.ids[0]]['percent'])
        self.assertEqual(progress[self.ids[0]]['kind'], 'verification')
        self.assertEqual(progress[self.ids[1]]['percent'], 75)

    def test_pending_publications_are_finalized_instead_of_deadlocking(self):
        with self.db() as db:
            db.execute("UPDATE jobs SET status='published' WHERE id IN (?,?)", self.ids[:2])
            db.execute("UPDATE queue_worker SET phase='idle',job_id=NULL")
            self.assertIn(queue_control.next_runnable(db)['id'], self.ids[:2])
            self.assertEqual(queue_control.next_runnable(db, include_published=False)['id'], self.ids[2])

    def test_stale_secondary_lock_blocks_restart_until_released(self):
        from platform_utils import acquire_lock
        with (self.root / 'parallel-worker.lock').open('a') as held:
            acquire_lock(held, blocking=False)
            with self.assertRaises(queue_control.WorkerShutdown):
                queue_control.wait_for_secondary_exit(self.root, timeout=.2)
        queue_control.wait_for_secondary_exit(self.root, timeout=.2)

    def test_low_memory_or_disk_defers_the_second_job(self):
        cgroup = self.root/'cgroup'; cgroup.mkdir()
        (cgroup/'memory.max').write_text(str(1536*1024**2))
        (cgroup/'memory.current').write_text(str(1400*1024**2))
        (cgroup/'memory.stat').write_text('inactive_file 104857600\n')
        meminfo = self.root/'meminfo';meminfo.write_text('MemAvailable: 4000000 kB\n')
        with self.db() as db:
            job = db.execute('SELECT * FROM jobs WHERE id=?',(self.ids[1],)).fetchone()
            self.assertEqual(queue_control.parallel_resource_reason(db,job,cgroup=cgroup,meminfo=meminfo),'memory')
            (cgroup/'memory.current').write_text(str(400*1024**2))
            with patch.object(queue_control.shutil,'disk_usage',return_value=type('Disk',(),{'free':1000})()):
                self.assertEqual(queue_control.parallel_resource_reason(db,job,cgroup=cgroup,meminfo=meminfo),'storage')
            meminfo.write_text('MemAvailable: 500000 kB\n')
            self.assertEqual(queue_control.parallel_resource_reason(db,job,cgroup=cgroup,meminfo=meminfo),'memory')

    def test_second_worker_crash_terminates_its_encoder_and_does_not_start_a_third(self):
        self.start()
        with self.db() as db:
            state = queue_control.snapshot(db)
            queue_control.set_max_workers(db, 1, state['control_revision'])
        os.kill(self.manager.process.pid, 9)
        self.wait(lambda:self.manager.process is None, 'crashed worker reaped')
        self.wait(lambda:self.state()['parallel_job_id'] is None, 'secondary slot released')
        frozen = self.second_count.read_text()
        time.sleep(.4)
        self.assertEqual(self.second_count.read_text(), frozen)
        with self.db() as db:
            statuses = [db.execute('SELECT status FROM jobs WHERE id=?',(i,)).fetchone()[0] for i in self.ids]
        self.assertEqual(statuses, ['running','failed','queued'])

    def test_crashed_worker_kills_encoder_even_if_it_ignores_term(self):
        marker = self.root/'orphan-counter'
        child = ('import pathlib,signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                 'p=pathlib.Path(sys.argv[1]); n=0\n'
                 'while True:\n n+=1; p.write_text(str(n)); time.sleep(.03)\n')
        parent = ('import pathlib,subprocess,sys,time\n'
                  'p=pathlib.Path(sys.argv[2]); subprocess.Popen([sys.executable,"-c",sys.argv[1],str(p)])\n'
                  'while not p.exists(): time.sleep(.02)\n'
                  'sys.exit(7)\n')
        with self.db() as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (self.ids[1],))
            queue_control.runtime(db, 'parallel:test', 'encoding', self.ids[1], parallel=True)
        process = subprocess.Popen([sys.executable, '-c', parent, child, str(marker)], start_new_session=True)
        try:
            self.wait(lambda:process.poll() is not None and marker.exists(), 'worker crash')
            self.manager.process, self.manager.identifier, self.manager.token = process, self.ids[1], 'parallel:test'
            self.manager.reap()
            frozen = marker.read_text()
            time.sleep(.4)
            self.assertEqual(marker.read_text(), frozen)
            with self.db() as db:
                self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?', (self.ids[1],)).fetchone()[0], 'failed')
        finally:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass

    def test_crashed_priority_worker_kills_its_orphan_before_releasing_slot(self):
        marker = self.root/'priority-orphan-counter'
        child = ('import pathlib,signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                 'p=pathlib.Path(sys.argv[1]); n=0\n'
                 'while True:\n n+=1; p.write_text(str(n)); time.sleep(.03)\n')
        parent = ('import pathlib,subprocess,sys,time\n'
                  'p=pathlib.Path(sys.argv[2]); subprocess.Popen([sys.executable,"-c",sys.argv[1],str(p)])\n'
                  'while not p.exists(): time.sleep(.02)\n'
                  'sys.exit(7)\n')
        with self.db() as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (self.ids[1],))
            db.execute('UPDATE queue_control SET priority_job_id=?', (self.ids[1],))
            queue_control.runtime(db, 'priority:test', 'encoding', self.ids[1], priority=True)
        process = subprocess.Popen([sys.executable, '-c', parent, child, str(marker)], start_new_session=True)
        try:
            self.wait(lambda:process.poll() is not None and marker.exists(), 'priority worker crash')
            self.control.priority_process, self.control.priority_process_id = process, self.ids[1]
            self.control._finish_priority_worker()
            frozen = marker.read_text()
            time.sleep(.4)
            self.assertEqual(marker.read_text(), frozen)
            with self.db() as db:
                self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?', (self.ids[1],)).fetchone()[0], 'failed')
                self.assertIsNone(db.execute('SELECT priority_job_id FROM queue_control').fetchone()[0])
        finally:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass

    def test_both_historical_verifiers_continue_while_primary_is_busy(self):
        import library_worker
        marker = self.root / 'primary-busy'
        text = self.child.read_text()
        text = text.replace('import queue_control\n',
                            'import queue_control\nfrom library_worker import PriorityControl\n'
                            'class Gate:\n def reason(self): return "primary_busy" if Path(' + repr(str(marker)) + ').exists() else None\n')
        text = text.replace('c=queue_control.JobControl(database,token,identifier,shutdown,parallel_worker=True)',
                            'c=PriorityControl(database,token,identifier,shutdown,gate=Gate(),parallel_worker=True)')
        self.child.write_text(text)
        class Gate:
            def reason(self): return 'primary_busy' if marker.exists() else None
        self.control = library_worker.PriorityControl(
            self.dbpath, 'primary', self.ids[0], self.stop.is_set, gate=Gate(), parallel_manager=self.manager)
        self.control.phase = 'verifying'
        self.start()
        before = self.primary_count.read_text(), self.second_count.read_text()
        marker.touch()
        self.wait(
            lambda:(self.primary_count.read_text(), self.second_count.read_text()) != before,
            'verification workers continue while the primary queue is busy')
        phases = {worker['phase'] for worker in self.state()['workers']}
        self.assertIn('verifying', phases)
        self.assertNotIn('waiting_priority', phases)

    def test_live_work_waits_until_both_historical_workers_have_yielded(self):
        with self.db() as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (self.ids[1],))
            queue_control.runtime(db, 'secondary', 'encoding', self.ids[1], parallel=True)
        gate = queue_control.HistoryYieldGate(self.dbpath)
        try:
            self.assertEqual(gate.reason(), 'waiting_history')
            with self.db() as db:
                queue_control.runtime(db, 'primary', 'waiting_priority', self.ids[0])
            self.assertEqual(gate.reason(), 'waiting_history')
            with self.db() as db:
                queue_control.runtime(db, 'secondary', 'waiting_priority', self.ids[1], parallel=True)
            self.assertIsNone(gate.reason())
            with self.db() as db:
                db.execute('UPDATE queue_parallel_worker SET heartbeat=0')
            self.assertEqual(gate.reason(), 'waiting_history')
        finally:
            gate.close()


if __name__ == '__main__': unittest.main()
