"""Active time is independent of wall-clock changes and all suspended phases."""
import sqlite3
import unittest
from unittest.mock import patch

import job_metrics
import queue_control


class JobMetricsTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE jobs(id TEXT,status TEXT,created REAL)')
        self.db.executemany('INSERT INTO jobs VALUES(?,?,0)', [('a', 'running'), ('b', 'queued')])
        queue_control.migrate(self.db)

    def tearDown(self):
        self.db.close()

    def tick(self, seconds, phase, job='a', worker='one', wall=1000):
        with patch.object(job_metrics.time, 'monotonic', return_value=seconds), \
                patch.object(job_metrics.time, 'time', return_value=wall):
            queue_control.runtime(self.db, worker, phase, job)

    def read(self, seconds, wall=1000):
        with patch.object(job_metrics.time, 'monotonic', return_value=seconds), \
                patch.object(job_metrics.time, 'time', return_value=wall):
            return job_metrics.read(self.db, ['a', 'b'])

    def test_pause_and_priority_waits_freeze_then_verification_resumes(self):
        self.tick(10, 'preparing')
        self.tick(20, 'encoding')
        self.assertEqual(self.read(25)['a'], 15)
        self.tick(25, 'paused')
        self.assertEqual(self.read(100)['a'], 15)
        self.tick(125, 'waiting_priority')
        self.tick(225, 'waiting_prerequisites')
        self.assertEqual(self.read(300)['a'], 15)
        self.tick(325, 'verifying')
        self.tick(330, 'publishing')
        self.tick(332, 'idle', None)
        self.db.execute("UPDATE jobs SET status='done' WHERE id='a'")
        self.assertEqual(self.read(1000)['a'], 22)

    def test_retry_starts_a_new_attempt_and_clock_changes_do_not_change_duration(self):
        self.tick(10, 'encoding')
        self.tick(15, 'idle', None, wall=-1000)
        self.assertEqual(self.read(900)['a'], 5)
        self.tick(1000, 'preparing', wall=-1000)
        self.assertEqual(self.read(1003, wall=-1000)['a'], 3)

    def test_worker_crash_does_not_charge_downtime_and_stale_workers_do_not_keep_counting(self):
        self.tick(10, 'encoding')
        self.tick(11, 'encoding')
        self.assertEqual(self.read(800, wall=1016)['a'], 1)
        self.tick(1000, 'starting', None, worker='two')
        self.assertEqual(self.read(1000)['a'], 1)
        self.tick(1001, 'preparing', worker='two')
        self.assertEqual(self.read(1002)['a'], 1)

    def test_published_recovery_keeps_existing_work_but_legacy_tasks_stay_unknown(self):
        self.tick(10, 'encoding')
        self.tick(20, 'offline', None)
        self.db.execute("UPDATE jobs SET status='published' WHERE id='a'")
        self.tick(1000, 'finalizing', worker='two')
        self.assertEqual(self.read(1002)['a'], 12)
        self.tick(1002, 'finalizing', 'b', worker='two')
        self.assertNotIn('b', self.read(1005))
        self.db.execute('DROP TABLE job_timings')
        self.assertEqual(self.read(2000), {})

    def test_priority_child_has_its_own_timer_and_parent_resumes_without_reset(self):
        self.tick(10, 'encoding')
        self.tick(20, 'priority_wait')
        self.db.execute("UPDATE jobs SET status='running' WHERE id='b'")
        with patch.object(job_metrics.time, 'monotonic', return_value=20), \
                patch.object(job_metrics.time, 'time', return_value=1000):
            queue_control.runtime(self.db, 'child', 'encoding', 'b', priority=True)
        self.assertEqual(self.read(25), {'a': 10, 'b': 5})
        self.tick(30, 'priority_wait')
        with patch.object(job_metrics.time, 'monotonic', return_value=30), \
                patch.object(job_metrics.time, 'time', return_value=1000):
            queue_control.runtime(self.db, 'child', 'offline', priority=True)
        self.db.execute("UPDATE jobs SET status='done' WHERE id='b'")
        self.tick(30, 'encoding')
        self.assertEqual(self.read(35), {'a': 15, 'b': 10})


if __name__ == '__main__':
    unittest.main()
