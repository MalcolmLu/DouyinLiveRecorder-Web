"""Exercise the shared encoder with real processes and independent queues."""
from concurrent.futures import ThreadPoolExecutor
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
import library_worker
import queue_control
from platform_utils import acquire_lock, release_lock


@unittest.skipUnless(os.name != 'nt', 'Uses POSIX process suspension and inherited locks')
class EncodeVerifyOverlapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dlr-encode-verify-')
        self.root = Path(self.temp.name)
        self.main = self.root / 'main' / 'queue.sqlite3'
        self.hist = self.root / 'history' / 'queue.sqlite3'
        self.lock = self.hist.parent / 'shared-encoder.lock'
        self.live_id, self.hist_id, self.next_id = ('a' * 64, 'b' * 64, 'c' * 64)
        for path, identifier in ((self.main, self.live_id), (self.hist, self.hist_id)):
            path.parent.mkdir()
            with self.connect(path) as db:
                db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,source TEXT,size INTEGER,mtime INTEGER,delete_source INTEGER,h264 INTEGER,status TEXT,error TEXT,output TEXT,output_size INTEGER,output_mtime INTEGER,created REAL,updated REAL)')
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (identifier, str(self.root / (identifier[0] + '.flv')), 10, 1, 1, 0,
                            'running', None, None, None, None, 1, 1))
                queue_control.migrate(db)
                db.execute('UPDATE queue_control SET max_workers=2')
                queue_control.runtime(db, 'worker', 'idle')
        self.shutdown = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=3)
        self.futures = []
        self.controls = []
        self.script = ('import pathlib,sys,time\n'
                       'start,stop,counter=map(pathlib.Path,sys.argv[1:])\n'
                       'start.write_text(str(__import__("os").getpid()))\n'
                       'i=0\nwhile not stop.exists():\n i+=1;counter.write_text(str(i));time.sleep(.03)\n')

    @staticmethod
    def connect(path):
        db = sqlite3.connect(path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def wait(self, predicate, label, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.03)
        self.fail('Timed out: ' + label)

    def phase(self, database):
        with self.connect(database) as db:
            return db.execute('SELECT phase FROM queue_worker WHERE id=1').fetchone()[0]

    def control(self, path, identifier, **kwargs):
        controller = queue_control.JobControl(path, 'worker', identifier, self.shutdown.is_set,
                                              encoder_lock=self.lock, **kwargs)
        self.controls.append(controller)
        return controller

    def command(self, name):
        start, stop, count = (self.root / (name + suffix) for suffix in ('.start', '.stop', '.count'))
        return [sys.executable, '-c', self.script, str(start), str(stop), str(count)], start, stop, count

    def submit(self, fn):
        future = self.pool.submit(fn)
        self.futures.append(future)
        return future

    def tearDown(self):
        self.shutdown.set()
        for future in self.futures:
            try:
                future.result(timeout=10)
            except (queue_control.WorkerShutdown, queue_control.JobStopped):
                pass
        for controller in self.controls:
            controller.close()
        self.pool.shutdown(wait=True)
        self.temp.cleanup()

    def test_next_encoder_waits_then_runs_while_first_verifies(self):
        first, second = self.control(self.hist, self.hist_id), self.control(self.main, self.live_id)
        enc1, start1, stop1, _ = self.command('enc1')
        enc2, start2, _, _ = self.command('enc2')
        verify, vstart, _, _ = self.command('verify')

        def pipeline():
            first.checkpoint(phase='encoding')
            first.run(enc1, check=True)
            first.checkpoint(phase='verifying')
            first.run(verify, check=True)

        self.submit(pipeline)
        self.wait(start1.exists, 'first encoder started')
        self.submit(lambda: (second.checkpoint(phase='encoding'), second.run(enc2, check=True)))
        self.wait(lambda: self.phase(self.main) == 'waiting_encoder', 'second encoder waiting')
        self.assertFalse(start2.exists())
        stop1.touch()
        self.wait(lambda: vstart.exists() and start2.exists(), 'verification and next encoder overlap')
        self.assertEqual(self.phase(self.hist), 'verifying')
        self.assertEqual(self.phase(self.main), 'encoding')

    def test_second_same_queue_job_starts_only_at_verification(self):
        with self.connect(self.hist) as db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (self.next_id, str(self.root/'next.flv'), 10, 1, 1, 0, 'queued',
                        None, None, None, None, 2, 2))
            queue_control.append_job(db, self.next_id)
            queue_control.runtime(db, 'worker', 'encoding', self.hist_id)
        marker = self.root / 'secondary-started'
        manager = queue_control.ParallelManager(self.hist,
            [sys.executable, '-c', 'from pathlib import Path; import sys,time; Path(sys.argv[1]).touch(); time.sleep(10)', str(marker)],
            self.root/'secondary.log')
        try:
            with patch.object(queue_control, 'parallel_resource_reason', return_value=None):
                manager.maybe_start()
                self.assertFalse(marker.exists())
                with self.connect(self.hist) as db:
                    queue_control.runtime(db, 'worker', 'verifying', self.hist_id)
                manager.last_attempt = 0
                manager.maybe_start()
                self.wait(marker.exists, 'secondary starts during verification')
        finally:
            manager.close()

    def test_two_verifiers_allow_a_third_task_but_not_a_fourth(self):
        ids = [self.hist_id, self.next_id, 'd'*64, 'e'*64]
        with self.connect(self.hist) as db:
            for number, identifier in enumerate(ids[1:], 2):
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (identifier, str(self.root/(identifier[0]+'.flv')), 10, 1, 1, 0, 'queued',
                            None, None, None, None, number, number))
                queue_control.append_job(db, identifier)
            db.execute('UPDATE queue_control SET max_workers=3')
            queue_control.runtime(db, 'worker', 'verifying', self.hist_id)
        child = self.root / 'three_slot_child.py'
        child.write_text('''import os,sqlite3,sys,time\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nimport queue_control\ndatabase,folder,identifier=sys.argv[2:]\nslot=int(os.environ['DLR_PARALLEL_SLOT']);token=os.environ['DLR_PARALLEL_TOKEN']\nwith sqlite3.connect(database) as db:\n db.row_factory=sqlite3.Row\n assert queue_control.claim_reserved_parallel(db,identifier,token,slot)\n queue_control.runtime(db,token,'encoding',identifier,parallel=slot)\nfolder=Path(folder);(folder/('slot%d-start'%slot)).touch()\nwhile not (folder/('slot%d-verify'%slot)).exists(): time.sleep(.03)\nwith sqlite3.connect(database) as db:\n db.row_factory=sqlite3.Row\n queue_control.runtime(db,token,'verifying',identifier,parallel=slot)\nwhile True: time.sleep(.1)\n''')
        managers = [queue_control.ParallelManager(self.hist,
                    [sys.executable, str(child), str(ROOT/'gui'), str(self.hist), str(self.root)],
                    self.root/('worker%d.log'%slot), slot=slot) for slot in (1, 2)]
        group = queue_control.ParallelManagers(managers)
        try:
            with patch.object(queue_control, 'parallel_resource_reason', return_value=None):
                group.maybe_start()
                self.wait((self.root/'slot1-start').exists, 'second task encoding')
                managers[1].last_attempt = 0
                group.maybe_start()
                self.assertFalse((self.root/'slot2-start').exists())
                (self.root/'slot1-verify').touch()
                self.wait(lambda: self._slot_phase(1) == 'verifying', 'second task verifying')
                managers[1].last_attempt = 0
                group.maybe_start()
                self.wait((self.root/'slot2-start').exists, 'third task encoding')
                with self.connect(self.hist) as db:
                    state = queue_control.snapshot(db)
                    self.assertEqual(len(state['workers']), 3)
                    self.assertEqual([w['phase'] for w in state['workers']].count('verifying'), 2)
                    self.assertEqual([w['phase'] for w in state['workers']].count('encoding'), 1)
                    self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0], 1)
                group.maybe_start()
                self.assertEqual(len([m for m in managers if m.process is not None]), 2)
        finally:
            group.close()

    def _slot_phase(self, slot):
        with self.connect(self.hist) as db:
            row = db.execute('SELECT phase FROM '+queue_control.PARALLEL_TABLES[slot-1]+' WHERE id=1').fetchone()
            return row[0] if row else None

    def test_paused_encoder_yields_lock_and_resume_waits_for_new_owner(self):
        first, second = self.control(self.hist, self.hist_id), self.control(self.main, self.live_id)
        enc1, start1, _, count1 = self.command('paused')
        enc2, start2, stop2, _ = self.command('other')
        self.submit(lambda: (first.checkpoint(phase='encoding'), first.run(enc1, check=True)))
        self.wait(start1.exists, 'first encoder')
        with self.connect(self.hist) as db:
            state = queue_control.snapshot(db)
            queue_control.request_control(db, 'pause', state['control_revision'])
        self.wait(lambda: self.phase(self.hist) == 'paused', 'first encoder paused')
        frozen = count1.read_text()
        self.submit(lambda: (second.checkpoint(phase='encoding'), second.run(enc2, check=True)))
        self.wait(start2.exists, 'second encoder uses released lock')
        with self.connect(self.hist) as db:
            state = queue_control.snapshot(db)
            queue_control.request_control(db, 'resume', state['control_revision'])
        self.wait(lambda: self.phase(self.hist) == 'waiting_encoder', 'resume waits for lock')
        time.sleep(.2)
        self.assertEqual(count1.read_text(), frozen)
        stop2.touch()
        self.wait(lambda: count1.read_text() != frozen, 'first encoder resumes')

    def test_live_encoder_preempts_history_and_history_resumes_during_verification(self):
        with self.connect(self.main) as db:
            db.execute("UPDATE jobs SET status='done' WHERE id=?", (self.live_id,))
        gate = library_worker.PriorityGate(self.main, historical_database=self.hist)
        history = library_worker.PriorityControl(self.hist, 'worker', self.hist_id,
                                                  self.shutdown.is_set, gate=gate)
        self.controls.append(history)
        live = self.control(self.main, self.live_id)
        hist_cmd, hist_start, _, hist_count = self.command('history')
        live_cmd, live_start, live_stop, _ = self.command('live')
        verify, verify_start, _, _ = self.command('live-verify')
        try:
            self.submit(lambda: (history.checkpoint(phase='encoding'), history.run(hist_cmd, check=True)))
            self.wait(hist_start.exists, 'history encoding')
            with self.connect(self.main) as db:
                db.execute("UPDATE jobs SET status='running' WHERE id=?", (self.live_id,))
                queue_control.runtime(db, 'worker', 'preparing', self.live_id)
            self.wait(lambda: self.phase(self.hist) == 'waiting_priority', 'history yielded to live')
            frozen = hist_count.read_text()

            def live_pipeline():
                live.checkpoint(phase='encoding')
                live.run(live_cmd, check=True)
                live.checkpoint(phase='verifying')
                live.run(verify, check=True)

            self.submit(live_pipeline)
            self.wait(live_start.exists, 'live encoding')
            time.sleep(.2)
            self.assertEqual(hist_count.read_text(), frozen)
            live_stop.touch()
            self.wait(verify_start.exists, 'live verification')
            self.wait(lambda: hist_count.read_text() != frozen, 'history resumed during verification')
            self.assertFalse(self.shutdown.is_set())
        finally:
            self.shutdown.set()
            for future in self.futures:
                try:
                    future.result(timeout=10)
                except (queue_control.WorkerShutdown, queue_control.JobStopped):
                    pass
            gate.close()

    def test_encoder_keeps_lock_if_worker_is_killed(self):
        started, stop, count = (self.root / ('orphan' + suffix) for suffix in ('.start', '.stop', '.count'))
        worker_source = (
            'import sys\nfrom pathlib import Path\n'
            'sys.path.insert(0,sys.argv[1])\nimport queue_control\n'
            'c=queue_control.JobControl(sys.argv[2],"worker",sys.argv[3],encoder_lock=sys.argv[4])\n'
            'c.checkpoint(phase="encoding")\n'
            'c.run([sys.executable,"-c",sys.argv[5],*sys.argv[6:]],check=True)\n')
        worker = subprocess.Popen([sys.executable, '-c', worker_source, str(ROOT/'gui'), str(self.hist),
                                   self.hist_id, str(self.lock), self.script, str(started), str(stop), str(count)],
                                  start_new_session=True)
        child_pid = None
        try:
            self.wait(started.exists, 'orphan-test encoder started')
            child_pid = int(started.read_text())
            worker.kill(); worker.wait(timeout=5)
            with self.lock.open('a+') as contender:
                with self.assertRaises(BlockingIOError):
                    acquire_lock(contender, blocking=False)
                stop.touch()
                def available():
                    try:
                        acquire_lock(contender, blocking=False)
                        return True
                    except BlockingIOError:
                        return False
                self.wait(available, 'encoder released inherited lock')
                release_lock(contender)
                child_pid = None
        finally:
            if worker.poll() is None:
                worker.kill(); worker.wait(timeout=5)
            if child_pid is not None:
                try:
                    os.killpg(worker.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == '__main__':
    unittest.main()
