"""The live and historical queues share three slots and preserve live priority."""
from pathlib import Path
from contextlib import contextmanager
from contextlib import nullcontext
import importlib.util
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'gui'))
import library_worker
import queue_control


class SharedBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dlr-budget-test-')
        self.root = Path(self.tmp.name)
        self.main, self.hist = self.root/'main.sqlite3', self.root/'history.sqlite3'
        self.live = [c*64 for c in 'xyz']
        self.history = [c*64 for c in 'abc']
        for file, ids in [(self.main,self.live),(self.hist,self.history)]:
            with self.connect(file) as db:
                db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,source TEXT,size INTEGER,mtime INTEGER,delete_source INTEGER,h264 INTEGER,status TEXT,error TEXT,output TEXT,output_size INTEGER,output_mtime INTEGER,created REAL,updated REAL)')
                db.executemany('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                               [(identifier,str(self.root/(identifier[0]+'.flv')),10,1,1,0,'done',None,None,None,None,i,i)
                                for i,identifier in enumerate(ids)])
                queue_control.migrate(db)
                db.execute('UPDATE queue_control SET max_workers=2')
                queue_control.runtime(db,'main' if file==self.main else 'hist','idle')
        with self.connect(self.hist) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id IN (?,?)",self.history[:2])
            queue_control.runtime(db,'hist','encoding',self.history[0])
            queue_control.runtime(db,'hist-secondary','encoding',self.history[1],parallel=True)
        self.gate = library_worker.PriorityGate(self.main, historical_database=self.hist)
        self.admission = queue_control.HistoryYieldGate(self.hist)

    @staticmethod
    @contextmanager
    def connect(file):
        db = sqlite3.connect(file, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def tearDown(self):
        self.gate.close(); self.admission.close(); self.tmp.cleanup()

    def test_live_demand_yields_only_the_history_slot_it_needs(self):
        with self.connect(self.main) as db:
            db.execute("UPDATE jobs SET status='queued' WHERE id=?",(self.live[0],))
            self.assertEqual(queue_control.live_demand(db), 1)
        self.assertIsNone(self.gate.reason_for(self.history[0],'primary'))
        self.assertIsNone(self.gate.reason_for(self.history[1],'parallel'))
        with self.admission.admission(2) as reason: self.assertIsNone(reason)
        with self.connect(self.main) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?",(self.live[0],))
            queue_control.runtime(db,'main','encoding',self.live[0])
            db.execute("UPDATE jobs SET status='queued' WHERE id=?",(self.live[1],))
            self.assertEqual(queue_control.live_demand(db), 1)
            queue_control.runtime(db,'main','verifying',self.live[0])
            self.assertEqual(queue_control.live_demand(db), 2)
        self.assertIsNone(self.gate.reason_for(self.history[0],'primary'))
        self.assertEqual(self.gate.reason_for(self.history[1],'parallel'),'primary_busy')
        with self.admission.admission(1) as reason: self.assertEqual(reason,'waiting_history')
        with self.connect(self.hist) as db:
            queue_control.runtime(db,'hist-secondary','waiting_priority',self.history[1],parallel=True)
        with self.admission.admission(1) as reason: self.assertIsNone(reason)
        with self.connect(self.main) as db:
            db.execute("UPDATE jobs SET status='done' WHERE id IN (?,?)",self.live[:2])
            queue_control.runtime(db,'main','idle')
        self.assertIsNone(self.gate.reason_for(self.history[0],'primary'))
        self.assertIsNone(self.gate.reason_for(self.history[1],'parallel'))

    def test_blocked_live_file_or_merge_keeps_one_history_slot(self):
        with self.connect(self.main) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (self.live[0],))
            db.execute("UPDATE jobs SET status='queued',source=? WHERE id=?",
                       (str(self.root/'x.ts'), self.live[1]))
            queue_control.runtime(db, 'main', 'encoding', self.live[0])
            self.assertEqual(queue_control.live_demand(db), 1)
        self.assertIsNone(self.gate.reason_for(self.history[0], 'primary'))
        self.assertIsNone(self.gate.reason_for(self.history[1], 'parallel'))
        with self.connect(self.main) as db:
            db.execute('UPDATE jobs SET source=? WHERE id=?', (str(self.root/'y.flv'), self.live[1]))
            db.execute('CREATE TABLE merge_jobs(id TEXT,inputs TEXT,destination TEXT)')
            db.execute('INSERT INTO merge_jobs VALUES(?,?,?)',
                       (self.live[1], '[]', str(self.root/'merged.mp4')))
            self.assertEqual(queue_control.live_demand(db), 1)
            db.execute('DELETE FROM merge_jobs WHERE id=?', (self.live[1],))
            queue_control.runtime(db, 'main', 'verifying', self.live[0])
            self.assertEqual(queue_control.live_demand(db), 2)
        self.assertIsNone(self.gate.reason_for(self.history[0], 'primary'))
        self.assertEqual(self.gate.reason_for(self.history[1], 'parallel'), 'primary_busy')

    def test_live_priority_verifier_remains_admitted_after_marker_is_cleared(self):
        promoted = self.history[2]
        with self.connect(self.hist) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (promoted,))
            queue_control.runtime(db, 'history-priority', 'verifying', promoted, priority=True)
            self.assertIsNone(db.execute(
                'SELECT priority_job_id FROM queue_control WHERE id=1'
            ).fetchone()[0])
        self.assertIsNone(self.gate.reason_for(promoted, 'priority'))
        self.assertIsNone(self.gate.reason_for(self.history[0], 'primary'))

    def test_live_resource_block_keeps_history_slot_until_capacity_recovers(self):
        with self.connect(self.main) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (self.live[0],))
            db.execute("UPDATE jobs SET status='queued' WHERE id=?", (self.live[1],))
            queue_control.runtime(db, 'main', 'encoding', self.live[0])
            for reason in ('memory','storage'):
                db.execute('UPDATE queue_control SET parallel_wait_reason=?', (reason,))
                self.assertEqual(queue_control.live_demand(db), 1)
        self.assertIsNone(self.gate.reason_for(self.history[0], 'primary'))
        self.assertIsNone(self.gate.reason_for(self.history[1], 'parallel'))
        # The second live task is admitted only once the first is verifying.
        with self.connect(self.main) as db:
            queue_control.runtime(db, 'main', 'verifying', self.live[0])
        observed=[]
        def admission(db):
            observed.append(queue_control.live_demand(db))
            return nullcontext('waiting_history')
        manager=queue_control.ParallelManager(self.main,[sys.executable,'-c','pass'],self.root/'parallel.log',admission=admission)
        try:
            with patch.object(queue_control,'parallel_resource_reason',return_value=None):manager.maybe_start()
            self.assertEqual(observed,[2])
            with self.connect(self.main) as db:
                self.assertEqual(db.execute('SELECT parallel_wait_reason FROM queue_control').fetchone()[0],'waiting_history')
                self.assertEqual(queue_control.live_demand(db),2)
        finally:manager.close()
        self.assertIsNone(self.gate.reason_for(self.history[0], 'primary'))
        self.assertEqual(self.gate.reason_for(self.history[1], 'parallel'), 'primary_busy')

    def test_live_claim_commits_before_history_claim_lock_is_released(self):
        with self.connect(self.main) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (self.live[0],))
            db.execute("UPDATE jobs SET status='queued' WHERE id=?", (self.live[1],))
            queue_control.runtime(db, 'main', 'verifying', self.live[0])
        with self.connect(self.hist) as db:
            db.execute("UPDATE jobs SET status='done' WHERE status='running'")
            queue_control.runtime(db, 'hist', 'idle')
            queue_control.runtime(db, 'hist-secondary', 'offline', parallel=True)
        attempted, observed, failures = threading.Event(), [], []

        def competing_history_claim():
            try:
                with self.connect(self.hist) as history:
                    attempted.set()
                    history.execute('BEGIN IMMEDIATE')
                    with self.connect(self.main) as live:
                        row = live.execute('SELECT status FROM jobs WHERE id=?', (self.live[1],)).fetchone()
                        owner = live.execute('SELECT job_id,phase FROM queue_parallel_worker WHERE id=1').fetchone()
                        observed.append((row[0], tuple(owner) if owner else None))
                    history.rollback()
            except Exception as exc:
                failures.append(exc)

        @contextmanager
        def admission(live):
            with self.admission.admission(2, commit=live) as reason:
                thread = threading.Thread(target=competing_history_claim)
                thread.start()
                self.assertTrue(attempted.wait(2))
                yield reason
            # Join before the caller's outer live transaction exits: this
            # catches a reservation committed only after releasing the lock.
            thread.join(3)
            self.assertFalse(thread.is_alive())

        manager = queue_control.ParallelManager(
            self.main, [sys.executable, '-c', 'pass'], self.root/'parallel.log', admission=admission)
        try:
            with patch.object(queue_control, 'parallel_resource_reason', return_value=None):
                manager.maybe_start()
            self.assertEqual(failures, [])
            self.assertEqual(observed, [('running', (self.live[1], 'starting'))])
        finally:
            manager.close()

    def test_live_and_history_verifying_allow_third_history_task(self):
        with self.connect(self.main) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (self.live[0],))
            queue_control.runtime(db, 'main', 'verifying', self.live[0])
        with self.connect(self.hist) as db:
            db.execute("UPDATE jobs SET status='done' WHERE id=?", (self.history[1],))
            db.execute("UPDATE jobs SET status='queued' WHERE id=?", (self.history[2],))
            db.execute('UPDATE queue_control SET max_workers=3')
            queue_control.runtime(db, 'hist', 'verifying', self.history[0])
            queue_control.runtime(db, 'hist-secondary', 'offline', parallel=True)
        self.assertIsNone(self.gate.reason_for(None, 'parallel'))
        manager = queue_control.ParallelManager(self.hist, [sys.executable, '-c', 'pass'], self.root/'third.log',
                     admission=lambda db: nullcontext(self.gate.reason_for(None, 'parallel', db)))
        try:
            with patch.object(queue_control, 'parallel_resource_reason', return_value=None):
                manager.maybe_start()
            with self.connect(self.hist) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0], 2)
                self.assertEqual(db.execute('SELECT job_id FROM queue_parallel_worker WHERE id=1').fetchone()[0], self.history[2])
        finally:
            manager.close()

    def test_live_takes_encoder_while_two_history_verifications_continue(self):
        with self.connect(self.main) as db:
            db.execute("UPDATE jobs SET status='queued' WHERE id=?", (self.live[0],))
        with self.connect(self.hist) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (self.history[2],))
            queue_control.runtime(db, 'hist', 'encoding', self.history[0])
            queue_control.runtime(db, 'hist-secondary', 'verifying', self.history[1], parallel=1)
            queue_control.runtime(db, 'hist-third', 'verifying', self.history[2], parallel=2)
        self.assertEqual(self.gate.reason_for(self.history[0], 'primary'), 'primary_busy')
        self.assertIsNone(self.gate.reason_for(self.history[1], 'parallel'))
        self.assertIsNone(self.gate.reason_for(self.history[2], 'parallel2'))

    def test_history_verification_does_not_block_live_encoder_admission(self):
        with self.connect(self.hist) as db:
            queue_control.runtime(db, 'hist', 'verifying', self.history[0])
            queue_control.runtime(
                db, 'hist-secondary', 'verifying', self.history[1], parallel=True
            )
        self.assertIsNone(self.admission.reason())

        with self.connect(self.hist) as db:
            queue_control.runtime(db, 'hist', 'encoding', self.history[0])
        self.assertEqual(self.admission.reason(), 'waiting_history')

        with self.connect(self.hist) as db:
            queue_control.runtime(db, 'hist', 'publishing', self.history[0])
        self.assertIsNone(self.admission.reason())

    def test_only_remaining_parallel_history_can_use_the_free_slot(self):
        with self.connect(self.main) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?",(self.live[0],))
            queue_control.runtime(db,'main','encoding',self.live[0])
        with self.connect(self.hist) as db:
            db.execute("UPDATE jobs SET status='done' WHERE id=?",(self.history[0],))
            queue_control.runtime(db,'hist','idle')
        self.assertIsNone(self.gate.reason_for(None,'primary'))
        self.assertIsNone(self.gate.reason_for(self.history[1],'parallel'))

    def test_priority_transition_reserves_two_until_old_live_workers_pause(self):
        with self.connect(self.main) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id IN (?,?)",self.live[:2])
            db.execute("UPDATE jobs SET status='queued' WHERE id=?",(self.live[2],))
            db.execute('UPDATE queue_control SET priority_job_id=?',(self.live[2],))
            queue_control.runtime(db,'main','encoding',self.live[0])
            queue_control.runtime(db,'secondary','encoding',self.live[1],parallel=True)
            self.assertEqual(queue_control.live_demand(db),2)
            queue_control.runtime(db,'main','priority_wait',self.live[0])
            queue_control.runtime(db,'secondary','priority_wait',self.live[1],parallel=True)
            self.assertEqual(queue_control.live_demand(db),1)
            db.execute("UPDATE jobs SET status='running' WHERE id=?",(self.live[2],))
            queue_control.runtime(db,'priority','encoding',self.live[2],priority=True)
            self.assertEqual(queue_control.live_demand(db),1)
            db.execute("UPDATE jobs SET status='done' WHERE id=?",(self.live[2],))
            db.execute('UPDATE queue_control SET priority_job_id=NULL')
            self.assertEqual(queue_control.live_demand(db),2)

    def test_historical_priority_owns_the_single_free_slot(self):
        with self.connect(self.main) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?",(self.live[0],))
            queue_control.runtime(db,'main','encoding',self.live[0])
        with self.connect(self.hist) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?",(self.history[2],))
            db.execute('UPDATE queue_control SET priority_job_id=?',(self.history[2],))
            queue_control.runtime(db,'hist','priority_wait',self.history[0])
            queue_control.runtime(db,'hist-secondary','priority_wait',self.history[1],parallel=True)
            queue_control.runtime(db,'hist-priority','encoding',self.history[2],priority=True)
        self.assertIsNone(self.gate.reason_for(self.history[2],'priority'))
        self.assertIsNone(self.gate.reason_for(self.history[0],'primary'))
        self.assertEqual(self.gate.reason_for(self.history[1],'parallel'),'primary_busy')

    def test_live_resume_waits_for_history_to_release_the_reserved_slot(self):
        spec=importlib.util.spec_from_file_location('dual_live_processor',ROOT/'h265/postprocess.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with self.connect(self.main) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id IN (?,?)",self.live[:2])
            queue_control.runtime(db,'main','priority_wait',self.live[0])
            queue_control.runtime(db,'secondary','priority_wait',self.live[1],parallel=True)
        control=module.LiveControl(self.main,'main',self.live[0],history_gate=self.admission)
        control.suspended=True
        self.assertEqual(control.wait_reason('running',False),'waiting_history')
        control.suspended=False; control.waiting=True
        self.assertEqual(control.wait_reason('running',False),'waiting_history')
        with self.connect(self.hist) as db:
            queue_control.runtime(db,'hist','waiting_priority',self.history[0])
            queue_control.runtime(db,'hist-secondary','waiting_priority',self.history[1],parallel=True)
        self.assertIsNone(control.wait_reason('running',False))

    def test_live_admission_respects_historical_claim_lock(self):
        with self.connect(self.hist) as hist:
            try:
                hist.execute('BEGIN IMMEDIATE')
                with self.admission.admission(2) as reason: self.assertEqual(reason,'waiting_history')
            finally:
                hist.rollback()
        with self.admission.admission(2) as reason: self.assertIsNone(reason)


if __name__ == '__main__': unittest.main()
