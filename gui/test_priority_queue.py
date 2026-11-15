"""Priority jobs suspend only their sibling encoder and preserve its progress."""
from contextlib import closing
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import queue_control
import library_worker


class PriorityQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="priority-queue-test-")
        self.root = Path(self.temp.name)
        self.db = self.root / "queue.sqlite3"
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "CREATE TABLE jobs(id TEXT PRIMARY KEY, source TEXT, size INTEGER, mtime INTEGER, "
                "delete_source INTEGER, h264 INTEGER, status TEXT, error TEXT, output TEXT, "
                "output_size INTEGER, output_mtime INTEGER, created REAL, updated REAL)")
            connection.executemany(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [("a" * 64, "a.ts", 1, 1, 1, 0, "running", None, None, None, None, 1, 1),
                 ("b" * 64, "b.ts", 1, 1, 1, 0, "queued", None, None, None, None, 2, 2)])
            queue_control.migrate(connection)
            queue_control.runtime(connection, "worker", "encoding", "a" * 64)

    def tearDown(self):
        self.temp.cleanup()

    def test_priority_child_runs_once_and_old_process_resumes_after_marker_clears(self):
        marker = self.root / "priority-started"
        command = [sys.executable, "-c",
                   "import sys,time;from pathlib import Path;"
                   "p=Path(sys.argv[1]);p.write_text(sys.argv[2]);time.sleep(.35)",
                   str(marker), "b" * 64]
        control = queue_control.JobControl(self.db, "worker", "a" * 64,
                                           priority_command=command)
        old_process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(5)"])
        try:
            with closing(sqlite3.connect(self.db)) as connection, connection:
                connection.execute("UPDATE queue_control SET priority_job_id=?", ("b" * 64,))
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(control.checkpoint, old_process)
                deadline = time.monotonic() + 3
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertEqual(marker.read_text(), "b" * 64)
                self.assertTrue(control.suspended)
                self.assertEqual(old_process.poll(), None)
                elapsed = future.result(timeout=5)
            self.assertGreaterEqual(elapsed, .15)
            with closing(sqlite3.connect(self.db)) as connection, connection:
                connection.execute("UPDATE jobs SET status='done' WHERE id=?", ("b" * 64,))
                connection.execute("UPDATE queue_control SET priority_job_id=NULL")
            control.checkpoint(old_process)
            self.assertFalse(control.suspended)
            self.assertLess(time.monotonic() - started, 3)
        finally:
            control.close()
            old_process.terminate()
            old_process.wait(timeout=5)

    def test_priority_marker_is_cleared_when_child_fails(self):
        command = [sys.executable, "-c", "raise SystemExit(3)"]
        control = queue_control.JobControl(self.db, "worker", "a" * 64,
                                           priority_command=command)
        old_process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(5)"])
        try:
            with closing(sqlite3.connect(self.db)) as connection, connection:
                connection.execute("UPDATE queue_control SET priority_job_id=?", ("b" * 64,))
            control.checkpoint(old_process)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with closing(sqlite3.connect(self.db)) as connection, connection:
                    value = connection.execute(
                        "SELECT priority_job_id FROM queue_control WHERE id=1").fetchone()[0]
                if value is None:
                    break
                time.sleep(.05)
            self.assertIsNone(value)
            with closing(sqlite3.connect(self.db)) as connection, connection:
                status = connection.execute("SELECT status FROM jobs WHERE id=?", ("b" * 64,)).fetchone()[0]
            self.assertNotEqual(status, "running")
        finally:
            control.close()
            old_process.terminate()
            old_process.wait(timeout=5)

    def test_library_controller_reaps_finished_child_and_unfreezes_the_same_process(self):
        gate = unittest.mock.Mock()
        gate.reason.return_value = None
        marker = self.root / 'library-priority'
        command = [sys.executable, '-c',
                   'import sys,time;from pathlib import Path;Path(sys.argv[1]).touch();time.sleep(.5)', str(marker)]
        control = library_worker.PriorityControl(self.db, 'worker', 'a' * 64, gate=gate,
                                                 priority_command=command)
        old = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(15)'])
        try:
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute('UPDATE queue_control SET priority_job_id=?', ('b' * 64,))
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(control.checkpoint, old)
                deadline = time.monotonic() + 4
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists())
                self.assertTrue(control.suspended)
                future.result(timeout=5)
            self.assertIsNone(old.poll())
            self.assertFalse(control.suspended)
            self.assertIsNone(control.priority_process)
        finally:
            control.close()
            old.terminate()
            old.wait(timeout=5)

    def test_library_verifier_keeps_running_after_encoder_demand_appears(self):
        gate = unittest.mock.Mock()
        gate.reason.return_value = 'primary_busy'
        gate.reason_for.return_value = 'primary_busy'
        control = library_worker.PriorityControl(
            self.db, 'worker', 'a' * 64, gate=gate
        )
        try:
            control.phase = 'verifying'
            self.assertIsNone(control.wait_reason('running', True))
            control.phase = 'publishing'
            self.assertIsNone(control.wait_reason('running', True))
            control.phase = 'encoding'
            self.assertEqual(control.wait_reason('running', False), 'primary_busy')
            self.assertEqual(control.wait_reason('paused', False), 'paused')
        finally:
            control.close()

    def test_selected_job_stays_first_when_the_waiting_queue_is_sorted(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("INSERT INTO jobs(id,source,size,mtime,status,created) VALUES(?, 'c.ts',0,0,'queued',0)", ('c' * 64,))
            queue_control.migrate(db)
            state = queue_control.snapshot(db)
            selected = queue_control.start_now(db, 'b' * 64, state['order_revision'])
            queue_control.reorder(db, selected['order_revision'], sort='smallest')
            self.assertEqual(queue_control.queued_ids(db), ['b' * 64, 'c' * 64])

    def test_child_foreground_snapshot_does_not_lose_the_suspended_parent(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("UPDATE jobs SET status='running' WHERE id=?", ('b' * 64,))
            db.execute('UPDATE queue_control SET priority_job_id=?', ('b' * 64,))
            queue_control.runtime(db, 'worker', 'priority_wait', 'a' * 64)
            queue_control.runtime(db, 'child', 'encoding', 'b' * 64, priority=True)
            state = queue_control.snapshot(db)
            self.assertEqual(state['current_job_id'], 'b' * 64)
            self.assertEqual(state['suspended_job_id'], 'a' * 64)
            self.assertTrue(state['priority_worker_active'])
            db.execute('UPDATE queue_priority_worker SET heartbeat=0')
            stale = queue_control.snapshot(db)
            self.assertFalse(stale['priority_worker_active'])
            self.assertEqual(stale['current_job_id'], 'a' * 64)


if __name__ == "__main__":
    unittest.main()
