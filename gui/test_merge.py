"""Merge timing, room exceptions, restart persistence and authenticated requests."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from app import create_app
import merge_queue
import queue_control
from shared import Paths, room_id, write_json


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dlr-merge-test-")
        self.root = Path(self.temp.name).resolve()
        self.config, self.downloads = self.root / "config", self.root / "downloads"
        self.state = self.config / "gui/state"; self.state.mkdir(parents=True)
        self.home = self.config / "h265"; self.home.mkdir()
        self.folder = self.downloads / "B站直播/测试主播"; self.folder.mkdir(parents=True)
        (self.config / "config.ini").write_text("[录制设置]\n保存文件夹是否以作者区分 = 是\n录制完成后自动转为mp4格式 = 是\n")
        (self.config / "URL_config.ini").write_text("https://live.bilibili.com/100001,主播: 测试主播\nhttps://live.bilibili.com/100002,主播: 其他主播\n")
        self.room = room_id("https://live.bilibili.com/100001")
        self.now = time.time(); self.started = self.now - 30
        self.database = self.home / "queue.sqlite3"
        self.opened = set()
        with self.connect() as db:
            db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,source TEXT,size INTEGER,mtime INTEGER,delete_source INTEGER,h264 INTEGER,"
                       "status TEXT,error TEXT,output TEXT,output_size INTEGER,output_mtime INTEGER,created REAL,updated REAL)")
            queue_control.migrate(db)
        self.publish()
        self.manager = self.new_manager()

    def tearDown(self):
        self.temp.cleanup()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database); db.row_factory = sqlite3.Row
        try:
            yield db; db.commit()
        finally:
            db.close()

    def new_manager(self, hevc=True):
        manager = merge_queue.MergeManager(self.config, lambda: [self.downloads], self.connect,
                                          lambda path: Path(path) in self.opened, clock=lambda: self.now,
                                          is_hevc=lambda path: hevc)
        manager.runtime = self.state / "runtime.json"
        return manager

    def publish(self, active=True, started=None):
        write_json(self.state / "runtime.json", {"heartbeat": self.now, "rooms": {self.room: {
            "state": "recording" if active else "offline", "name": "测试主播",
            "started_at": self.started if started is None else started,
            "file": "测试主播_%Y-%m-%d_%H-%M-%S.ts"}}})

    def part(self, index, status="done", updated=None):
        source = self.folder / f"测试主播_2026-10-17_10-00-{index:02d}.ts"
        output = source.with_suffix(".mp4"); output.write_bytes(b"isolated encoded recording" + bytes([index]))
        stat = output.stat(); identifier = str(index).zfill(64)
        with self.connect() as db:
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (identifier, str(source), 100,
                       int((self.started + 1 + index) * 1e9), 1, 0, status, None, str(output), stat.st_size,
                       stat.st_mtime_ns, self.now - 10, self.now if updated is None else updated))
        return output, identifier

    def sessions(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM merge_sessions")]

    def merge_count(self):
        with self.connect() as db:
            return db.execute("SELECT count(*) FROM merge_jobs").fetchone()[0]

    def finish(self):
        self.publish(False); self.manager.scan_once()

    def advance(self, seconds):
        self.now += seconds; self.publish(False)

    def test_waits_for_end_and_two_minutes_after_last_success_not_first_part(self):
        self.part(1, updated=self.now - 1000); _, last = self.part(2, "running")
        self.manager.scan_once(); self.assertEqual(self.merge_count(), 0)
        self.finish(); self.advance(200); self.manager.scan_once(); self.assertEqual(self.merge_count(), 0)
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='done',updated=? WHERE id=?", (self.now, last))
        self.manager.scan_once(); self.advance(119); self.manager.scan_once(); self.assertEqual(self.merge_count(), 0)
        self.advance(2); self.manager.scan_once(); self.assertEqual(self.merge_count(), 1)
        self.manager.scan_once(); self.assertEqual(self.merge_count(), 1)

    def test_room_exception_keeps_parts_and_allows_explicit_manual_request(self):
        write_json(self.config / "gui/rooms.json", {"https://live.bilibili.com/100001": {"auto_merge": False}})
        self.part(1); self.part(2); self.manager.scan_once(); self.finish(); self.advance(500)
        self.manager.scan_once(); self.assertEqual(self.merge_count(), 0)
        with self.connect() as db:
            db.execute("UPDATE merge_sessions SET requested=1")
        self.manager.scan_once(); self.assertEqual(self.merge_count(), 1)

    def test_pause_stop_mode_is_not_changed_when_merge_is_added(self):
        self.part(1); self.part(2); self.manager.scan_once(); self.finish(); self.advance(121)
        with self.connect() as db: db.execute("UPDATE queue_control SET mode='stopped'")
        self.manager.scan_once()
        with self.connect() as db:
            self.assertEqual(db.execute("SELECT mode FROM queue_control").fetchone()[0], "stopped")
        self.assertEqual(self.merge_count(), 1)

    def test_explicit_history_batch_also_waits_two_minutes_for_hevc(self):
        self.part(1); self.part(2); self.manager.scan_once(); self.finish()
        with self.connect() as db:
            db.execute("UPDATE merge_sessions SET requested=2,auto_merge=0")
        self.manager.scan_once(); self.assertEqual(self.merge_count(), 0)
        self.advance(119); self.manager.scan_once(); self.assertEqual(self.merge_count(), 0)
        self.advance(2); self.manager.scan_once(); self.assertEqual(self.merge_count(), 1)

    def test_h264_failed_and_open_parts_never_auto_merge(self):
        one, _ = self.part(1); self.part(2)
        self.manager = self.new_manager(hevc=False)
        self.manager.scan_once(); self.finish(); self.advance(121); self.manager.scan_once()
        self.assertEqual(self.merge_count(), 0)
        self.manager = self.new_manager(); self.opened.add(one); self.manager.scan_once(); self.assertEqual(self.merge_count(), 0)
        self.opened.clear()
        with self.connect() as db: db.execute("UPDATE jobs SET status='failed' WHERE id=?", (str(2).zfill(64),))
        self.manager.scan_once(); self.assertEqual(self.merge_count(), 0)

    def test_restart_recovers_wait_and_stale_heartbeat_cannot_end_recording(self):
        self.part(1); self.part(2); self.manager.scan_once()
        self.now += 30; self.manager = self.new_manager(); self.manager.scan_once()
        self.assertIsNone(self.sessions()[0]["ended"])
        self.finish(); self.advance(121); self.manager = self.new_manager(); self.manager.scan_once()
        self.assertEqual(self.merge_count(), 1)

    def test_short_reconnect_joins_previous_session_and_resets_wait(self):
        self.part(1); self.part(2); self.manager.scan_once(); self.finish()
        self.now += 30; self.publish(True, started=self.now - 1); self.manager.scan_once()
        self.assertEqual(len(self.sessions()), 1); self.assertIsNone(self.sessions()[0]["ended"])
        self.assertEqual(self.merge_count(), 0)

    def test_manually_deleted_failed_part_is_skipped_but_existing_failed_part_blocks(self):
        self.part(1); self.part(2); deleted, failed = self.part(3, "failed")
        self.manager.scan_once(); self.finish(); self.advance(121)
        self.manager.scan_once(); self.assertEqual(self.merge_count(), 0)
        deleted.unlink(); self.manager.scan_once(); self.assertEqual(self.merge_count(), 1)
        with self.connect() as db:
            record = db.execute("SELECT * FROM merge_jobs").fetchone()
            self.assertEqual(len(json.loads(record["inputs"])), 2)
            self.assertEqual(len(json.loads(record["skipped"])), 1)
            self.assertIn("_缺1P", record["destination"])

    def test_manual_api_requires_auth_csrf_revision_and_room_scope(self):
        self.part(1); self.part(2); self.manager.scan_once(); self.finish()
        app = create_app(Paths(self.config, self.downloads, self.state), testing=True)
        app.extensions["recorder_web"]["auth"].set_password("Merge-Local-Test-42")
        client = app.test_client(); endpoint = f"/api/rooms/{self.room}/sessions"
        self.assertEqual(client.get(endpoint).status_code, 401)
        csrf = client.post("/api/login", json={"password": "Merge-Local-Test-42"}).json["csrf"]
        row = client.get(endpoint).json["items"][0]; target = endpoint + "/" + row["id"] + "/merge"
        self.assertTrue(row["can_merge"])
        self.assertEqual(client.post(target, json={"revision": row["revision"]}).status_code, 403)
        headers = {"X-CSRF-Token": csrf}
        self.assertEqual(client.post(target, json={"revision": "stale"}, headers=headers).status_code, 409)
        other = target.replace(self.room, room_id("https://live.bilibili.com/100002"))
        self.assertEqual(client.post(other, json={"revision": row["revision"]}, headers=headers).status_code, 404)
        self.assertEqual(client.post(target, json={"revision": row["revision"]}, headers=headers).status_code, 200)
        self.assertEqual(client.post(target, json={"revision": row["revision"]}, headers=headers).status_code, 409)

    def test_defaults_and_per_room_exception_persist_through_settings_update(self):
        app = create_app(Paths(self.config, self.downloads, self.state), testing=True)
        config = app.extensions["recorder_web"]["config"]
        self.assertTrue(config.settings()["values"]["merge_parts"])
        config.update_room(self.room, {"revision": config.revision(), "auto_merge": False})
        self.assertFalse(config.rooms()[0]["auto_merge"])
        values = config.settings()["values"]; values.update(merge_parts=False, delete_merged_parts=False)
        config.save_settings({"revision": config.revision(), "values": values})
        config.apply_pending()
        self.assertFalse(config.settings()["values"]["merge_parts"])
        self.assertFalse(config.rooms()[0]["auto_merge"])


if __name__ == "__main__":
    unittest.main()
