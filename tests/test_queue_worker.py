"""Exercise real owned subprocesses, persisted pauses, and publication races."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gui"))
import queue_control


class ProcessControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dlr-queue-process-test-")
        self.root = Path(self.temp.name)
        self.database = self.root / "queue.sqlite3"
        self.identifier = "a" * 64
        self.worker = "isolated-process-worker"
        with self.db() as db:
            db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,status TEXT,created REAL)")
            db.execute("INSERT INTO jobs VALUES(?,?,?)", (self.identifier, "running", time.time()))
            queue_control.migrate(db)
            queue_control.runtime(db, self.worker, "encoding", self.identifier)
        self.shutdown = threading.Event()
        self.control = queue_control.JobControl(self.database, self.worker, self.identifier, self.shutdown.is_set)
        self.control.phase = "encoding"
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.processes = []
        self.Popen = subprocess.Popen
        self.spawn_patch = patch.object(queue_control.subprocess, "Popen", side_effect=self.spawn)
        self.spawn_patch.start()

    def tearDown(self):
        self.shutdown.set()
        self.pool.shutdown(wait=True)
        for process in self.processes:
            if process.poll() is None:
                process.kill(); process.wait(timeout=5)
        self.spawn_patch.stop()
        self.temp.cleanup()

    def spawn(self, *args, **kwargs):
        process = self.Popen(*args, **kwargs)
        self.processes.append(process)
        return process

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def wait(self, predicate, timeout=5):
        limit = time.monotonic() + timeout
        while time.monotonic() < limit:
            if predicate():
                return
            time.sleep(.02)
        self.fail("Owned test subprocess did not reach the expected state")

    def phase(self):
        with self.db() as db:
            return queue_control.snapshot(db)["phase"]

    def command(self, action):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = queue_control.snapshot(db)
            return queue_control.request_control(db, action, state["control_revision"], state["current_job_id"])

    def counter_process(self, duration=10, timeout=None):
        counter = self.root / "ticks.txt"
        script = ("from pathlib import Path; import time\n"
                  f"p=Path({str(counter)!r})\n"
                  f"limit=time.monotonic()+{duration}\n"
                  "n=0\nwhile time.monotonic()<limit:\n n+=1; p.write_text(str(n)); time.sleep(.03)\n"
                  "print('finished')\n")
        future = self.pool.submit(self.control.run, [sys.executable, "-c", script], check=True,
                                  timeout=timeout, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.wait(lambda: counter.exists() and counter.stat().st_size > 0)
        return counter, future

    @unittest.skipIf(os.name == "nt", "POSIX suspension integration runs on macOS/Linux")
    def test_pause_freezes_child_then_resume_advances_and_stop_reaps_it(self):
        counter, future = self.counter_process()
        self.command("pause")
        self.wait(lambda: self.phase() == "paused")
        before = counter.read_bytes()
        time.sleep(.4)
        self.assertEqual(counter.read_bytes(), before)
        self.assertFalse(future.done())
        self.command("resume")
        self.wait(lambda: counter.read_bytes() != before)
        self.command("stop")
        with self.assertRaises(queue_control.JobStopped):
            future.result(timeout=5)
        self.assertTrue(all(process.poll() is not None for process in self.processes))

    @unittest.skipIf(os.name == "nt", "POSIX suspension integration runs on macOS/Linux")
    def test_stop_while_paused_resumes_before_termination_and_never_orphans_child(self):
        _, future = self.counter_process()
        self.command("pause")
        self.wait(lambda: self.phase() == "paused")
        self.command("stop")
        with self.assertRaises(queue_control.JobStopped):
            future.result(timeout=5)
        self.assertIsNotNone(self.processes[0].poll())

    def test_worker_shutdown_reaps_owned_children(self):
        _, future = self.counter_process()
        self.shutdown.set()
        with self.assertRaises(queue_control.WorkerShutdown):
            future.result(timeout=5)
        self.assertIsNotNone(self.processes[0].poll())

    @unittest.skipIf(os.name == "nt", "POSIX suspension integration runs on macOS/Linux")
    def test_paused_time_is_not_counted_towards_verification_timeout(self):
        # A paused child retains elapsed monotonic time, so use work iterations.
        script = "import time\nfor _ in range(8): time.sleep(.04)\nprint('finished')"
        future = self.pool.submit(self.control.run, [sys.executable, "-c", script], check=True,
                                  timeout=.7, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.wait(lambda: bool(self.processes))
        self.command("pause")
        self.wait(lambda: self.phase() == "paused")
        time.sleep(.8)
        self.command("resume")
        self.assertEqual(future.result(timeout=5).stdout.strip(), b"finished")

    def test_child_failure_and_timeout_keep_standard_error_behavior(self):
        with self.assertRaises(subprocess.CalledProcessError) as result:
            self.control.run([sys.executable, "-c", "import sys; print('reason',file=sys.stderr);sys.exit(7)"],
                             check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual((result.exception.returncode, result.exception.stderr.strip()), (7, b"reason"))
        with self.assertRaises(subprocess.TimeoutExpired):
            self.control.run([sys.executable, "-c", "import time;time.sleep(10)"], timeout=.2,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertTrue(all(process.poll() is not None for process in self.processes))

    def test_stop_before_publication_cannot_replace_source(self):
        original = self.root / "original.ts"; original.write_bytes(b"original recording")
        output = self.root / "verified.tmp.mp4"; output.write_bytes(b"candidate video")
        self.command("stop")
        with self.assertRaises(queue_control.JobStopped), self.control.publication(self.db):
            os.replace(output, original)
        self.assertEqual(original.read_bytes(), b"original recording")
        self.assertTrue(output.exists())

    def test_publication_and_stop_are_serialized_by_the_same_database_transaction(self):
        requested = threading.Event()
        results = []
        def stop():
            requested.set()
            results.append(self.command("stop"))
        with self.control.publication(self.db) as db:
            thread = threading.Thread(target=stop)
            thread.start(); requested.wait(timeout=2)
            db.execute("UPDATE jobs SET status='done' WHERE id=?", (self.identifier,))
            time.sleep(.1)
            self.assertEqual(results, [])
        thread.join(timeout=5)
        self.assertEqual(results[0]["mode"], "stopped")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT status FROM jobs").fetchone()[0], "done")


if __name__ == "__main__":
    unittest.main()
