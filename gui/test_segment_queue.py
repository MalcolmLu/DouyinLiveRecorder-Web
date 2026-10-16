"""Only stable, closed parts of the current recording session may auto-enqueue."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

import queue_control
from segment_queue import ClosedSegmentScanner


class ClosedSegmentScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dlr-segment-queue-test-")
        self.root = Path(self.temp.name).resolve()
        self.config, self.downloads = self.root / "config", self.root / "downloads"
        (self.config / "gui/state").mkdir(parents=True)
        (self.config / "h265").mkdir()
        (self.downloads / "平台/主播").mkdir(parents=True)
        self.database = self.config / "h265/queue.sqlite3"
        self.now, self.tick = time.time(), 0
        self.started = self.now - 30
        self.open_files, self.enqueued = set(), []
        self.runtime = self.config / "gui/state/runtime.json"
        self.policy(True, True)
        with self.connect() as db:
            db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,source TEXT,size INTEGER,mtime INTEGER,"
                       "delete_source INTEGER,h264 INTEGER,status TEXT,error TEXT,output TEXT,output_size INTEGER,"
                       "output_mtime INTEGER,created REAL,updated REAL)")
            db.execute("CREATE TABLE deleted_jobs(id TEXT PRIMARY KEY,deleted_at REAL)")
            queue_control.migrate(db)
        self.publish()
        self.scanner = self.new_scanner()

    def tearDown(self):
        self.temp.cleanup()

    def new_scanner(self):
        scanner = ClosedSegmentScanner(self.config, lambda: [self.downloads], self.connect, self.enqueue,
                                       lambda path: path in self.open_files, clock=lambda: self.now,
                                       monotonic=lambda: self.tick)
        scanner.runtime = self.runtime
        return scanner

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def policy(self, convert, delete):
        (self.config / "config.ini").write_text("[录制设置]\n录制完成后自动转为mp4格式 = " + ("是" if convert else "否") +
                                               "\n追加格式后删除原文件 = " + ("是" if delete else "否") + "\n")

    def publish(self, state="recording", suffix="ts", started=None, heartbeat=None):
        self.runtime.write_text(json.dumps({"heartbeat": self.now if heartbeat is None else heartbeat,
            "rooms": {"room-a": {"state": state, "file": "主播_%Y-%m-%d_%H-%M-%S." + suffix,
                                   "started_at": self.started if started is None else started}}}))

    def advance(self, seconds=6):
        self.now += seconds; self.tick += seconds
        runtime = json.loads(self.runtime.read_text()); runtime["heartbeat"] = self.now
        self.runtime.write_text(json.dumps(runtime))

    def source(self, index=0, suffix="ts", modified=None):
        path = self.downloads / "平台/主播" / ("主播_2026-10-17_10-00-%02d." % index + suffix)
        path.write_bytes(b"isolated unfinished recording " + str(index).encode())
        epoch = self.now - 10 if modified is None else modified
        os.utime(path, (epoch, epoch))
        return path

    def enqueue(self, source, delete):
        stat = source.stat(); identifier = ClosedSegmentScanner.fingerprint(source, stat)
        with self.connect() as db:
            inserted = db.execute("INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (identifier, str(source), stat.st_size, stat.st_mtime_ns, int(delete), 0,
                                   "queued", None, None, None, None, self.now, self.now)).rowcount
        if inserted:
            self.enqueued.append((source, delete))
        return identifier

    def test_stable_closed_part_queues_before_session_end_and_deduplicates(self):
        source = self.source(); self.assertEqual(self.scanner.scan_once(), 0)
        self.advance(); self.assertEqual(self.scanner.scan_once(), 1)
        self.assertEqual(self.enqueued, [(source, True)])
        self.advance(); self.assertEqual(self.scanner.scan_once(), 0)
        self.assertEqual(json.loads(self.runtime.read_text())["rooms"]["room-a"]["state"], "recording")
        self.assertTrue(source.exists())

    def test_open_file_is_protected_even_if_writing_has_stalled(self):
        source = self.source(); self.open_files.add(source)
        self.scanner.scan_once(); self.advance(12)
        self.assertEqual(self.scanner.scan_once(), 0)
        self.open_files.remove(source)
        self.assertEqual(self.scanner.scan_once(), 1)

    def test_growing_file_must_stabilize_again(self):
        source = self.source(); self.scanner.scan_once(); self.advance()
        source.write_bytes(b"newer incomplete recording"); os.utime(source, (self.now - 3, self.now - 3))
        self.assertEqual(self.scanner.scan_once(), 0)
        self.advance(); self.assertEqual(self.scanner.scan_once(), 1)

    def test_old_sessions_other_names_empty_files_and_ended_sessions_are_excluded(self):
        self.source(0, modified=self.started - 1)
        other = self.downloads / "平台/主播/其他主播_2026-10-17_10-00-00.ts"; other.write_bytes(b"other room")
        empty = self.source(1); empty.write_bytes(b"")
        self.scanner.scan_once(); self.advance(); self.assertEqual(self.scanner.scan_once(), 0)
        self.source(2); self.publish(state="checking")
        self.scanner.scan_once(); self.advance(); self.assertEqual(self.scanner.scan_once(), 0)

    def test_stopped_failed_deleted_and_other_known_fingerprints_never_requeue(self):
        for index, status in enumerate(("stopped", "failed", "done", "running", "queued")):
            source = self.source(index); identifier = self.enqueue(source, False)
            with self.connect() as db:
                db.execute("UPDATE jobs SET status=? WHERE id=?", (status, identifier))
                if status == "done":
                    db.execute("INSERT INTO deleted_jobs VALUES(?,?)", (identifier, self.now))
        self.enqueued.clear(); self.scanner.scan_once(); self.advance()
        self.assertEqual(self.scanner.scan_once(), 0)
        self.assertEqual(self.enqueued, [])

    def test_completed_mp4_output_is_not_recursively_reencoded(self):
        self.publish(suffix="mp4"); source = self.source(suffix="mp4"); stat = source.stat()
        for state in ("published", "done"):
            with self.connect() as db:
                db.execute("DELETE FROM jobs")
                db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           ("f" * 64, str(source.with_suffix(".ts")), 100, 1, 0, 0, state, None,
                            str(source), stat.st_size, stat.st_mtime_ns, self.now, self.now))
            self.scanner.scan_once(); self.advance()
            self.assertEqual(self.scanner.scan_once(), 0)

    def test_recording_policy_is_captured_for_each_session(self):
        first = self.source(); self.scanner.scan_once()
        self.policy(False, False); self.advance()
        self.assertEqual(self.scanner.scan_once(), 1)
        self.assertEqual(self.enqueued, [(first, True)])
        self.publish(started=self.now - 2); second = self.source(1, modified=self.now - 1)
        self.scanner.scan_once(); self.advance()
        self.assertEqual(self.scanner.scan_once(), 0)
        self.assertEqual(self.enqueued, [(first, True)])
        self.assertTrue(second.exists())

    def test_keep_original_policy_and_queue_pause_stop_modes_are_respected(self):
        self.policy(True, False)
        for index, mode in enumerate(("paused", "stopped")):
            with self.connect() as db:
                db.execute("UPDATE queue_control SET mode=?", (mode,))
            scanner = self.new_scanner(); source = self.source(index)
            scanner.scan_once(); self.advance()
            self.assertEqual(scanner.scan_once(), 1)
            self.assertEqual(self.enqueued[-1], (source, False))
            with self.connect() as db:
                self.assertEqual(db.execute("SELECT mode FROM queue_control").fetchone()[0], mode)

    def test_stale_runtime_cannot_trigger_discovery(self):
        self.source(); self.publish(heartbeat=self.now - 30)
        self.scanner.scan_once(); self.advance(); self.publish(heartbeat=self.now - 30)
        self.assertEqual(self.scanner.scan_once(), 0)
        self.assertEqual(self.enqueued, [])

    def test_outside_symlink_is_rejected(self):
        outside = self.root / "outside.ts"; outside.write_bytes(b"outside recording")
        target = self.downloads / "平台/主播/主播_2026-10-17_10-00-00.ts"
        try:
            target.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks are unavailable")
        self.scanner.scan_once(); self.advance()
        self.assertEqual(self.scanner.scan_once(), 0)
        self.assertEqual(outside.read_bytes(), b"outside recording")


if __name__ == "__main__":
    unittest.main()
