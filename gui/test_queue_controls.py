"""Queue commands operate atomically and never delete recorded media."""
from contextlib import closing
import sqlite3
import time
import unittest

import queue_control
import test_console


class QueueControlApiTests(unittest.TestCase):
    tearDown = test_console.ConsoleTests.tearDown
    add_queue_job = test_console.ConsoleTests.add_queue_job

    def setUp(self):
        test_console.ConsoleTests.setUp(self)
        self.ids = [digit * 64 for digit in "bcdef7"]
        self.files = {}
        for identifier, status in zip(self.ids, ["queued", "queued", "queued", "running", "done", "stopped"]):
            self.files[identifier] = self.add_queue_job(identifier, status)[0]
        with closing(sqlite3.connect(self.paths.queue)) as db, db:
            for index, identifier in enumerate(self.ids):
                db.execute("UPDATE jobs SET created=? WHERE id=?", (1000 + index, identifier))
            queue_control.migrate(db)
            queue_control.runtime(db, "isolated-api-worker", "encoding", self.ids[3])

    def queue(self):
        return self.client.get("/api/jobs?status=queued").json

    def move(self, identifier, direction, revision=None):
        return self.client.post("/api/queue/order", headers=self.headers,
                                json={"job_id": identifier, "move": direction,
                                      "revision": self.queue()["order_revision"] if revision is None else revision})

    def command(self, action, current=None, revision=None):
        state = self.parts["queue"].control()
        return self.client.post("/api/queue/control", headers=self.headers,
                                json={"action": action, "revision": state["control_revision"] if revision is None else revision,
                                      "current_job_id": current if current is not None else state["current_job_id"]})

    def test_move_changes_actual_rank_and_preserves_nonqueued_records_and_media(self):
        originals = {identifier: (file.read_bytes(), file.stat().st_mtime_ns) for identifier, file in self.files.items()}
        self.assertEqual([job["id"] for job in self.queue()["items"]], self.ids[:3])
        self.assertEqual(self.move(self.ids[2], "top").status_code, 200)
        self.assertEqual([job["id"] for job in self.queue()["items"]], [self.ids[2], self.ids[0], self.ids[1]])
        self.assertEqual(self.move(self.ids[2], "down").status_code, 200)
        self.assertEqual([job["queue_position"] for job in self.queue()["items"]], [1, 2, 3])
        self.assertEqual(self.move(self.ids[0], "bottom").status_code, 200)
        self.assertEqual([job["id"] for job in self.queue()["items"]], [self.ids[2], self.ids[1], self.ids[0]])
        self.assertEqual(self.move(self.ids[1], "up").status_code, 200)
        self.assertEqual([job["id"] for job in self.queue()["items"]], [self.ids[1], self.ids[2], self.ids[0]])
        self.assertEqual(self.move(self.ids[3], "top").status_code, 409)
        self.assertEqual(originals, {identifier: (file.read_bytes(), file.stat().st_mtime_ns) for identifier, file in self.files.items()})
        with closing(sqlite3.connect(self.paths.queue)) as db:
            self.assertEqual(db.execute("SELECT status FROM jobs WHERE id=?", (self.ids[3],)).fetchone()[0], "running")

    def test_sort_applies_to_all_waiting_jobs_and_new_records_append(self):
        with closing(sqlite3.connect(self.paths.queue)) as db, db:
            for identifier, size in zip(self.ids[:3], [30, 10, 20]):
                db.execute("UPDATE jobs SET size=? WHERE id=?", (size, identifier))
        for mode, expected in [("smallest", [1, 2, 0]), ("largest", [0, 2, 1]),
                               ("newest", [2, 1, 0]), ("oldest", [0, 1, 2]), ("name", [0, 1, 2])]:
            result = self.client.post("/api/queue/order", headers=self.headers,
                                      json={"sort": mode, "revision": self.queue()["order_revision"]})
            self.assertEqual(result.status_code, 200, result.json)
            self.assertEqual([job["id"] for job in self.queue()["items"]], [self.ids[index] for index in expected])
        new = "8" * 64
        self.add_queue_job(new, "queued")
        with closing(sqlite3.connect(self.paths.queue)) as db, db:
            queue_control.migrate(db)
        self.assertEqual(self.queue()["items"][-1]["id"], new)

    def test_revisions_reject_stale_order_and_do_not_change_control_mode(self):
        revision = self.queue()["order_revision"]
        self.assertEqual(self.move(self.ids[2], "top", revision).status_code, 200)
        self.assertEqual(self.move(self.ids[0], "top", revision).status_code, 409)
        self.assertEqual(self.parts["queue"].control()["mode"], "running")
        # Claiming a job competes with ordering using the same SQLite write lock.
        revision = self.queue()["order_revision"]
        with closing(sqlite3.connect(self.paths.queue)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (self.ids[2],))
            db.execute("UPDATE queue_control SET order_revision=order_revision+1")
        self.assertEqual(self.move(self.ids[2], "bottom", revision).status_code, 409)

    def test_pause_resume_and_stop_do_not_interrupt_a_different_task(self):
        before = self.parts["queue"].control()
        paused = self.command("pause").json["control"]
        self.assertEqual((paused["mode"], paused["state"]), ("paused", "pausing"))
        self.assertEqual(self.command("resume", revision=before["control_revision"]).status_code, 409)
        self.assertEqual(self.command("resume").status_code, 200)
        self.assertEqual(self.command("stop", current=self.ids[0]).status_code, 409)
        self.assertEqual(self.parts["queue"].control()["mode"], "running")
        self.assertEqual(self.command("stop").status_code, 200)
        self.assertEqual(self.command("resume").status_code, 409)  # Do not revoke an in-flight stop.
        self.assertEqual(self.parts["queue"].control()["mode"], "stopped")

    def test_stopped_task_can_requeue_at_end_or_delete_record_without_deleting_video(self):
        identifier = self.ids[-1]
        source = self.files[identifier]
        original = (source.read_bytes(), source.stat().st_mtime_ns)
        result = self.client.post("/api/jobs/" + identifier + "/retry", json={}, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(self.queue()["items"][-1]["id"], identifier)
        with closing(sqlite3.connect(self.paths.queue)) as db, db:
            db.execute("UPDATE jobs SET status='stopped' WHERE id=?", (identifier,))
        result = self.client.post("/api/jobs/delete-records", json={"ids": [identifier]}, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual((source.read_bytes(), source.stat().st_mtime_ns), original)
        self.assertEqual(self.client.get("/api/jobs?status=stopped").json["total"], 0)
        self.assertEqual(self.client.post("/api/jobs/" + identifier + "/retry", json={}, headers=self.headers).status_code, 404)
        direct_id = "9" * 64
        direct, _ = self.add_queue_job(direct_id, "stopped")
        original = (direct.read_bytes(), direct.stat().st_mtime_ns)
        self.assertEqual(self.client.delete("/api/jobs/" + direct_id, headers=self.headers).status_code, 200)
        self.assertEqual((direct.read_bytes(), direct.stat().st_mtime_ns), original)

    def test_stop_acknowledgment_then_resume_preserves_stopped_jobs(self):
        self.command("stop")
        with closing(sqlite3.connect(self.paths.queue)) as db, db:
            db.execute("UPDATE jobs SET status='stopped' WHERE id=?", (self.ids[3],))
            queue_control.runtime(db, "isolated-api-worker", "stopped")
        self.assertEqual(self.command("resume").status_code, 200)
        self.assertEqual(self.client.get("/api/jobs?status=stopped").json["total"], 2)

    def test_invalid_payloads_permissions_and_offline_worker_fail_closed(self):
        for endpoint, payload in [("/api/queue/control", {"action": "pause", "revision": 0}),
                                  ("/api/queue/order", {"sort": "oldest", "revision": self.queue()["order_revision"]})]:
            self.assertEqual(self.app.test_client().post(endpoint, json=payload).status_code, 401)
            self.assertEqual(self.client.post(endpoint, json=payload).status_code, 403)
            for origin in ("null", "https://other.example"):
                self.assertEqual(self.client.post(endpoint, json=payload, headers=dict(self.headers, Origin=origin)).status_code, 403)
        for payload in ({"sort": [], "revision": self.queue()["order_revision"]}, {"sort": "unknown", "revision": 0},
                        {"job_id": "bad", "move": "top", "revision": 0}, {"sort": "oldest", "job_id": self.ids[0], "revision": 0},
                        {"sort": "oldest", "revision": True}):
            self.assertIn(self.client.post("/api/queue/order", json=payload, headers=self.headers).status_code, (409, 422))
        self.assertEqual(self.command("terminate-everything").status_code, 422)
        with closing(sqlite3.connect(self.paths.queue)) as db, db:
            db.execute("UPDATE queue_worker SET heartbeat=?", (time.time() - 30,))
        self.assertEqual(self.command("pause").status_code, 503)
        self.assertEqual(self.move(self.ids[0], "top").status_code, 503)


if __name__ == "__main__":
    unittest.main()
