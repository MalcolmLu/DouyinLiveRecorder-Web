"""Desktop API, portable locks, and saved-path/encoder invariants."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent.parent/"desktop"))
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/"scripts"))
from service import DesktopService
from setup import initialize
from shared import AppError, Paths, SharedLock, atomic_write
from app import create_app
from repository import QueueRepository

class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="dlr-desktop-test-")
        root=Path(self.temp.name).resolve()
        self.paths=Paths(root/"config",root/"recordings",root/"config/gui/state")
        initialize(self.paths)
        self.environment=patch.dict(os.environ,{},clear=False);self.environment.start()
        with patch.object(DesktopService,"apply_encoding"):
            self.service=DesktopService(self.paths,root)
        self.service.preferences()["auto_start"]=False

    def tearDown(self):
        self.service.shutdown()
        self.environment.stop();self.temp.cleanup()

    def test_local_session_requires_cookie_csrf_and_correct_origin(self):
        app=create_app(self.paths,desktop=self.service)
        app.config["DESKTOP_ALLOWED_HOST"]="127.0.0.1:12345"
        client=app.test_client();base="http://127.0.0.1:12345"
        self.assertEqual(client.get("/api/desktop",base_url=base).status_code,401)
        token,csrf=app.extensions["recorder_web"]["auth_repo"].create_session()
        client.set_cookie("recorder_session",token,domain="127.0.0.1")
        self.assertTrue(client.get("/api/session",base_url=base).json["desktop"])
        self.assertEqual(client.get("/api/desktop",base_url="http://localhost:12345").status_code,403)
        self.assertEqual(client.post("/api/desktop/stop",base_url=base,json={}).status_code,403)
        self.assertEqual(client.post("/api/desktop/stop",base_url=base,json={},
             headers={"X-CSRF-Token":csrf,"Origin":"https://malicious.invalid"}).status_code,403)
        self.assertEqual(client.post("/api/desktop/stop",base_url=base,json={},
             headers={"X-CSRF-Token":csrf,"Origin":base}).status_code,202)
        self.assertEqual(client.get("/api/desktop",base_url=base,
             environ_overrides={"REMOTE_ADDR":"192.0.2.10"}).status_code,403)

    def test_nas_does_not_expose_desktop_controls(self):
        app=create_app(self.paths,testing=True);client=app.test_client()
        auth=app.extensions["recorder_web"]["auth"];auth.set_password("Desktop-Test-Password-123")
        client.post("/api/login",json={"password":"Desktop-Test-Password-123"})
        self.assertEqual(client.get("/api/desktop").status_code,404)
        self.assertFalse(client.get("/api/session").json["desktop"])

    def test_directory_changes_preserve_old_files_and_roots(self):
        old=self.paths.downloads;old.mkdir(exist_ok=True)
        source=old/"old.ts";source.write_bytes(b"retained recording")
        values=self.service.preferences()
        values.pop("recording_roots")
        values.update(downloads=str(old.parent/"new-recordings"),auto_start=False)
        with patch.object(self.service,"apply_encoding"),patch.object(self.service,"start_worker"):
            result=self.service.save({"revision":self.service.revision(),"values":values})
        self.assertTrue(result["ok"]);self.assertTrue(source.exists())
        self.assertIn(str(old),self.service.preferences()["recording_roots"])
        self.assertIn(str(old.parent/"new-recordings"),self.service.preferences()["recording_roots"])
        self.assertEqual(source.read_bytes(),b"retained recording")
        with self.assertRaises(AppError):
            self.service.save({"revision":"stale","values":values})

    def test_active_recorder_prevents_folder_change(self):
        values=self.service.preferences();values.pop("recording_roots")
        values["downloads"]=str(self.paths.downloads.parent/"other")
        with patch.object(self.service,"status",return_value={"running":True}):
            with self.assertRaises(AppError) as error:
                self.service.save({"revision":self.service.revision(),"values":values})
        self.assertEqual(error.exception.status,409)
        self.assertNotEqual(self.service.preferences()["downloads"],values["downloads"])

    def test_hardware_selection_does_not_silently_fallback(self):
        values=self.service.preferences();values["encoder"]="hevc_qsv"
        write=self.paths.config/"h265/settings.json"
        with patch.object(self.service,"preferences",return_value=values):
            self.service.apply_encoding()
        self.assertEqual(json.loads(write.read_text())["encoder"],"hevc_qsv")

    def test_portable_nested_lock_and_atomic_write(self):
        target=self.paths.config/"portable.json"
        lock=SharedLock(self.paths.config/"portable.lock")
        with lock:
            with lock:atomic_write(target,'{"saved":true}')
        self.assertEqual(json.loads(target.read_text()),{"saved":True})

    def test_queue_file_before_worker_schema_is_treated_as_empty(self):
        self.paths.queue.touch()
        repository=QueueRepository(self.paths)
        self.assertEqual(repository.summary()["running"],0)
        self.assertEqual(repository.jobs("all","",1)["items"],[])

    def test_exit_requeues_interrupted_job_and_keeps_historical_failure(self):
        source=self.paths.downloads/"interrupted.ts"
        source.parent.mkdir(exist_ok=True);source.write_bytes(b"retained original")
        with closing(sqlite3.connect(self.paths.queue)) as db,db:
            db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,source TEXT,size INTEGER,mtime INTEGER,status TEXT,error TEXT,updated REAL)")
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,0)",("a"*64,str(source),source.stat().st_size,source.stat().st_mtime_ns,"running",None))
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,0)",("b"*64,str(source),source.stat().st_size,source.stat().st_mtime_ns,"failed","historical failure"))
        def interrupted_process(_):
            with closing(sqlite3.connect(self.paths.queue)) as db,db:
                db.execute("UPDATE jobs SET status='failed',error='interrupted' WHERE id=?",("a"*64,))
        with patch.object(self.service,"_terminate",side_effect=interrupted_process):self.service.shutdown()
        with closing(sqlite3.connect(self.paths.queue)) as db:
            rows=dict(db.execute("SELECT id,status FROM jobs"))
        self.assertEqual(rows,{"a"*64:"queued","b"*64:"failed"})
        self.assertEqual(source.read_bytes(),b"retained original")
