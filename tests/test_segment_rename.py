"""Rename migration protects media, queue identities and deleted-record tombstones."""
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("rename_segments", Path(__file__).resolve().parents[1] / "scripts/rename_segments.py")
migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)


class SegmentRenameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dlr-rename-test-")
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "downloads"; self.root.mkdir()
        self.config = self.base / "config"; (self.config / "h265").mkdir(parents=True)
        (self.config / "gui/state").mkdir(parents=True)
        self.manifest = self.config / "gui/state/rename.json"
        self.files = [self.root / ("主播_2026-10-10_10-00-00_%03d.mp4" % i) for i in (0, 1, 3)]
        for i, file in enumerate(self.files):
            file.write_bytes(("video %d" % i).encode())
        self.database = self.config / "h265/queue.sqlite3"
        source = self.files[0].with_suffix(".ts")
        self.identifier = hashlib.sha256(f'{source}\0{123}\0{456}'.encode()).hexdigest()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,source TEXT,size INTEGER,mtime INTEGER,output TEXT,status TEXT,updated REAL)")
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,'done',0)", (self.identifier, str(source), 123, 456, str(self.files[0])))
            db.execute("CREATE TABLE deleted_jobs(id TEXT PRIMARY KEY,deleted_at REAL)")
            db.execute("INSERT INTO deleted_jobs VALUES(?,0)", (self.identifier,))

    def tearDown(self):
        self.temp.cleanup()

    def plan(self):
        plan = migration.build_plan(self.root, probe=lambda path: 60.5)
        self.manifest.write_text(json.dumps(plan))
        return plan

    def test_measured_durations_and_missing_segment_fallback(self):
        plan = self.plan()
        names = [Path(e["new"]).name for e in plan["files"]]
        self.assertEqual(names, ["主播_2026-10-10_10-00-00.mp4", "主播_2026-10-10_10-01-00.mp4", "主播_2026-10-10_11-02-01.mp4"])
        self.assertIn("missing segment", plan["sessions"][0]["parts"][2]["method"])

    def test_apply_preserves_media_and_updates_queue_fingerprints_and_tombstones(self):
        plan = self.plan(); before = [p.read_bytes() for p in self.files]
        result = migration.apply_plan(plan, self.config, self.manifest)
        self.assertEqual(result["renamed"], 3)
        self.assertEqual([Path(e["new"]).read_bytes() for e in plan["files"]], before)
        self.assertTrue(all(not p.exists() for p in self.files))
        with closing(sqlite3.connect(self.database)) as db:
            row = db.execute("SELECT id,source,output,status FROM jobs").fetchone()
            new_source = self.root / "主播_2026-10-10_10-00-00.ts"
            expected = hashlib.sha256(f'{new_source}\0{123}\0{456}'.encode()).hexdigest()
            self.assertEqual(row, (expected, str(new_source), str(new_source.with_suffix(".mp4")), "done"))
            self.assertEqual(db.execute("SELECT id FROM deleted_jobs").fetchone()[0], expected)
        self.assertTrue(Path(result["queue_backup"]).is_file())
        self.assertEqual(json.loads(self.manifest.read_text())["status"], "applied")

    def test_existing_target_and_changed_source_are_never_overwritten(self):
        plan = self.plan(); target = Path(plan["files"][0]["new"]); target.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            migration.build_plan(self.root, probe=lambda path: 60.5)
        target.unlink(); self.files[0].write_bytes(b"changed source")
        with self.assertRaises(RuntimeError):
            migration.apply_plan(plan, self.config, self.manifest)
        self.assertEqual(self.files[0].read_bytes(), b"changed source")

    def test_active_queue_prevents_renaming(self):
        plan = self.plan()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE jobs SET status='running'")
        with self.assertRaises(RuntimeError):
            migration.apply_plan(plan, self.config, self.manifest)
        self.assertTrue(all(p.exists() for p in self.files))

    def test_partial_filesystem_failure_rolls_back_files_and_queue(self):
        plan = self.plan(); real_link = migration.os.link
        def failing_link(source, target):
            if str(target) == plan["files"][1]["new"]:
                raise OSError("isolated simulated disk failure")
            return real_link(source, target)
        with patch.object(migration.os, "link", side_effect=failing_link):
            with self.assertRaises(OSError):
                migration.apply_plan(plan, self.config, self.manifest)
        self.assertTrue(all(p.exists() for p in self.files))
        self.assertTrue(all(not Path(e["new"]).exists() for e in plan["files"]))
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT id FROM jobs").fetchone()[0], self.identifier)


if __name__ == "__main__":
    unittest.main()
