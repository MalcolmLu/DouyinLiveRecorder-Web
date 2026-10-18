"""Integration tests run only against isolated temporary configuration and media."""
import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
from html.parser import HTMLParser
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from app import create_app
from recorder_bridge import RecorderBridge, StopMonitoring
from shared import AppError, Paths, canonical_url, room_id, write_json


INI = """[录制设置]
视频保存格式ts|mkv|flv|mp4|mp3音频|m4a音频 = TS
分段录制是否开启 = 是
视频分段时间(秒) = 3600
原画|超清|高清|标清|流畅 = 原画
循环时间(秒) = 300
录制完成后自动转为mp4格式 = 是
追加格式后删除原文件 = 是
是否使用代理ip(是/否) = 是
代理地址 = http://test-user:test-secret@192.0.2.2:7893
使用代理录制的平台(逗号分隔) = youtu
额外使用代理录制的平台(逗号分隔) =
[Cookie]
unrelated = preserve-this-value
"""


class ConsoleTests(unittest.TestCase):
    def add_queue_job(self, identifier, status):
        source = self.paths.downloads / (identifier[:8] + ".ts")
        source.write_bytes(b"isolated second recording")
        output = source.with_suffix(".mp4")
        if status == "done":
            output.write_bytes(b"isolated encoded recording")
        stat = source.stat()
        with closing(sqlite3.connect(self.paths.queue)) as db, db:
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (identifier, str(source), stat.st_size, stat.st_mtime_ns, 1, 0,
                        status, None, str(output) if output.exists() else None,
                        output.stat().st_size if output.exists() else None, None, time.time(), time.time()))
        return source, output

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="recorder-web-test-")
        root = Path(self.temp.name).resolve()
        self.paths = Paths(root / "config", root / "downloads", root / "config/gui/state")
        self.paths.config.mkdir();self.paths.downloads.mkdir()
        (self.paths.config / "h265").mkdir()
        (self.paths.config / "config.ini").write_text(INI)
        (self.paths.config / "URL_config.ini").write_text(
            "https://live.bilibili.com/100001,主播: B站测试主播\n"
            "https://www.youtube.com/@ExampleChannel/live,主播: 频道测试\n")
        write_json(self.paths.config / "h265/settings.json", {"enabled": True, "encoder": "hevc_qsv", "qsv_quality": 24})
        db = sqlite3.connect(self.paths.queue)
        db.execute("""CREATE TABLE jobs(id TEXT PRIMARY KEY,source TEXT,size INTEGER,mtime INTEGER,
                   delete_source INTEGER,h264 INTEGER,status TEXT,error TEXT,output TEXT,
                   output_size INTEGER,output_mtime INTEGER,created REAL,updated REAL)""")
        source = self.paths.downloads / "fixture.ts";source.write_bytes(b"isolated test media")
        stat = source.stat();self.job_id = "a" * 64
        db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (self.job_id,str(source),stat.st_size,stat.st_mtime_ns,1,0,"failed","safe test failure",None,None,None,time.time(),time.time()))
        db.commit();db.close()
        self.app = create_app(self.paths, testing=True)
        self.parts = self.app.extensions["recorder_web"]
        self.parts["auth"].set_password("Local-Test-Password-42")
        self.client = self.app.test_client()
        login = self.client.post("/api/login", json={"password": "Local-Test-Password-42"})
        self.csrf = login.json["csrf"]
        self.headers = {"X-CSRF-Token": self.csrf}

    def tearDown(self):
        self.temp.cleanup()

    def revision(self):
        return self.parts["config"].revision()

    def test_authentication_origin_csrf_and_logout(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/api/dashboard").status_code, 401)
        self.assertEqual(self.client.post("/api/jobs/" + self.job_id + "/retry", json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/jobs/" + self.job_id + "/retry", json={}, headers=dict(self.headers, Origin="https://other.example")).status_code, 403)
        self.assertEqual(self.client.post("/api/logout", json={}, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/api/dashboard").status_code, 401)

    def test_login_rate_limit(self):
        for _ in range(8):
            self.assertEqual(self.client.post("/api/login", json={"password": "wrong"}).status_code, 401)
        self.assertEqual(self.client.post("/api/login", json={"password": "wrong"}).status_code, 429)

    def test_native_login_form_metadata_redirect_and_session(self):
        class Elements(HTMLParser):
            def __init__(self, html):
                super().__init__(); self.elements = {}; self.feed(html)
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if "id" in attrs:
                    self.elements[attrs["id"]] = (tag, attrs)
        browser = self.app.test_client()
        page = browser.get("/")
        self.assertEqual(page.headers["Referrer-Policy"], "same-origin")
        html = page.get_data(as_text=True)
        elements = Elements(html).elements
        form = elements["login-form"][1]
        self.assertEqual((form["method"], form["action"], form["autocomplete"]), ("post", "/login", "on"))
        self.assertNotIn("hidden", elements["login-view"][1])
        self.assertEqual(elements["login-username"][1]["autocomplete"], "username")
        self.assertEqual(elements["login-username"][1]["value"], "admin")
        self.assertEqual(elements["login-password"][1]["autocomplete"], "current-password")
        self.assertNotIn("value", elements["login-password"][1])
        result = browser.post("/login", data={"username": "admin", "password": "Local-Test-Password-42"},
                              headers={"Origin": "http://localhost"})
        self.assertEqual((result.status_code, result.headers["Location"]), (303, "/"))
        cookie = result.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie); self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn("Local-Test-Password-42", cookie)
        session = browser.get("/api/session").json
        self.assertTrue(session["authenticated"])
        self.assertEqual(browser.post("/api/jobs/delete-records", json={"ids": [self.job_id]},
                                      headers={"X-CSRF-Token": session["csrf"]}).status_code, 200)

    def test_native_login_error_stays_html_without_echoing_password(self):
        browser = self.app.test_client()
        result = browser.post("/login", data={"password": "<script>wrong-password</script>"})
        self.assertEqual(result.status_code, 401)
        self.assertEqual(result.mimetype, "text/html")
        self.assertNotIn("<script>wrong-password</script>", result.get_data(as_text=True))
        self.assertIn('id="login-error"', result.get_data(as_text=True))
        self.assertFalse(browser.get("/api/session").json["authenticated"])
        self.assertEqual(browser.post("/login", data={"password": "Local-Test-Password-42"},
                                      headers={"Origin": "https://other.example"}).status_code, 403)
        self.assertEqual(browser.post("/login", data={"password": "Local-Test-Password-42"},
                                      headers={"Origin": "null"}).status_code, 403)

    def test_native_login_shares_existing_rate_limit(self):
        for _ in range(8):
            self.client.post("/api/login", json={"password": "wrong"})
        result = self.app.test_client().post("/login", data={"password": "Local-Test-Password-42"})
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.mimetype, "text/html")

    def test_proxy_secrets_are_not_returned_and_blank_preserves(self):
        initial = self.client.get("/api/proxy")
        self.assertEqual(initial.status_code, 200, initial.json)
        self.assertNotIn("test-secret", initial.get_data(as_text=True))
        self.assertNotIn("test-user", initial.get_data(as_text=True))
        r = self.client.put("/api/proxy", json={"revision": self.revision(), "server": "http://192.0.2.3:7893",
                                              "username": "", "password": "", "clear_auth": False}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(self.parts["config"].proxy_url(), "http://test-user:test-secret@192.0.2.3:7893")
        self.assertIn("preserve-this-value", (self.paths.config / "config.ini").read_text())

    def test_room_add_normalize_duplicate_update_and_cross_tab(self):
        data = {"revision": self.revision(), "url": "https://www.huya.com/100002", "name": "虎牙测试主播",
                "quality": "原画", "proxy": "direct"}
        r = self.client.post("/api/rooms", json=data, headers=self.headers)
        self.assertEqual(r.status_code, 201, r.json)
        identifier = r.json["id"]
        data["revision"] = self.revision()
        self.assertEqual(self.client.post("/api/rooms", json=data, headers=self.headers).status_code, 409)
        second_tab = self.app.test_client()
        second_tab.post("/api/login", json={"password": "Local-Test-Password-42"})
        self.assertEqual(len(second_tab.get("/api/dashboard").json["rooms"]), 3)
        r = self.client.patch("/api/rooms/" + identifier, json={"revision": self.revision(), "enabled": False},
                              headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertIn("# 原画,https://www.huya.com/100002", (self.paths.config / "URL_config.ini").read_text())
        self.assertEqual(canonical_url("https://www.youtube.com/@ExampleChannel/streams"), "https://www.youtube.com/@ExampleChannel/live")

    def test_input_validation_and_stale_revision(self):
        data = {"revision": self.revision(), "url": "http://127.0.0.1/private", "name": "bad", "quality": "原画", "proxy": "direct"}
        self.assertEqual(self.client.post("/api/rooms", json=data, headers=self.headers).status_code, 422)
        data.update(url="https://www.huya.com/100002", name="line\ninjection")
        self.assertEqual(self.client.post("/api/rooms", json=data, headers=self.headers).status_code, 422)
        data.update(name="valid", revision="stale")
        self.assertEqual(self.client.post("/api/rooms", json=data, headers=self.headers).status_code, 409)

    def test_edit_url_moves_metadata_and_preserves_paused_room(self):
        config = self.parts["config"]
        old = "https://www.youtube.com/@ExampleChannel/live"
        config.update_room(room_id(old), {"revision":self.revision(), "enabled":False,
                                        "proxy":"shared", "quality":"高清", "name":"保留备注"})
        new = "https://www.youtube.com/@ExampleSecondChannel/live"
        response = self.client.patch("/api/rooms/" + room_id(old),
            json={"revision":self.revision(), "url":"https://www.youtube.com/@ExampleSecondChannel/streams"}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["id"], room_id(new))
        room = next(r for r in config.rooms() if r["url"] == new)
        self.assertEqual((room["name"],room["quality"],room["proxy"],room["enabled"]),
                         ("保留备注","高清","shared",False))
        self.assertNotIn(old, config.store.json_read(config.metadata, {}))
        self.assertNotIn(old, config.store.text(config.urls))
        self.assertEqual(self.client.patch("/api/rooms/" + room_id(old),
            json={"revision":self.revision(), "name":"stale"}, headers=self.headers).status_code, 404)

    def test_edit_url_validation_duplicate_and_active_recording_guard(self):
        config = self.parts["config"]
        old = "https://live.bilibili.com/100001"
        path = "/api/rooms/" + room_id(old)
        original = self.revision()
        for url, status in (("http://127.0.0.1/private",422),
                            ("https://www.youtube.com/@ExampleChannel/streams",409)):
            response = self.client.patch(path, json={"revision":self.revision(), "url":url}, headers=self.headers)
            self.assertEqual(response.status_code, status, response.json)
            self.assertEqual(self.revision(), original)
        write_json(self.paths.state / "runtime.json", {"heartbeat":time.time(),
            "rooms":{room_id(old):{"state":"recording"}}})
        data = {"revision":self.revision(), "url":"https://live.bilibili.com/100003"}
        self.assertEqual(self.client.patch(path,json=data,headers=self.headers).json["error"]["code"], "recording")
        config.update_room(room_id(old), {"revision":self.revision(),"enabled":False})
        data["revision"] = self.revision()
        self.assertEqual(self.client.patch(path,json=data,headers=self.headers).status_code, 409)
        write_json(self.paths.state / "runtime.json", {"heartbeat":time.time(),
            "rooms":{room_id(old):{"state":"checking"}}})
        self.assertEqual(self.client.patch(path,json=data,headers=self.headers).status_code, 200)

    def test_retired_url_monitor_stops_and_never_starts_ffmpeg(self):
        config = self.parts["config"]
        old = "https://www.youtube.com/@ExampleChannel/live"
        ns = {"running_list":[old], "monitoring":1}
        bridge = RecorderBridge(ns, self.paths)
        config.update_room(room_id(old), {"revision":self.revision(), "url":"https://www.youtube.com/@ExampleSecondChannel/live"})
        attempts = []
        def upstream(data):
            try:
                bridge.current_room_data(data)
            except Exception:
                attempts.append("retry")
        self.assertIsNone(bridge.wrap_start(upstream)(("原画",old,"旧备注")))
        self.assertEqual(attempts, [])

        self.assertEqual(ns["running_list"], [])
        self.assertEqual(ns["monitoring"], 0)
        with self.assertRaises(StopMonitoring):
            bridge.wrap_record(lambda *a: attempts.append("ffmpeg"))("old",old,["ffmpeg","test.ts"],"TS")
        self.assertEqual(attempts, [])

    def test_delete_and_restore_room_preserve_media_and_proxy(self):
        config = self.parts["config"]
        url = "https://www.youtube.com/@ExampleChannel/live"
        identifier = room_id(url)
        config.update_room(identifier,{"revision":self.revision(),"proxy":"shared","name":"频道备注"})
        response = self.client.delete("/api/rooms/"+identifier,json={"revision":self.revision()},headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)
        self.assertFalse(any(r["id"]==identifier for r in config.rooms()))
        self.assertTrue((self.paths.downloads/"fixture.ts").is_file())
        archive = self.client.get("/api/deleted-rooms").json
        self.assertEqual(archive["items"][0]["proxy"],"shared")
        response = self.client.post("/api/deleted-rooms/"+identifier+"/restore",
            json={"revision":archive["revision"]},headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)
        row = next(r for r in config.rooms() if r["id"]==identifier)
        self.assertEqual((row["enabled"],row["proxy"],row["name"]),(False,"shared","频道备注"))
        self.assertEqual(config.deleted_rooms(),[])

    def test_delete_requires_auth_csrf_revision_and_idle_recording(self):
        url = "https://www.youtube.com/@ExampleChannel/live";identifier=room_id(url)
        route="/api/rooms/"+identifier
        self.assertEqual(self.app.test_client().get("/api/deleted-rooms").status_code,401)
        self.assertEqual(self.client.delete(route,json={"revision":self.revision()}).status_code,403)
        self.assertEqual(self.client.delete(route,json={"revision":"stale"},headers=self.headers).status_code,409)
        write_json(self.paths.state/"runtime.json",{"heartbeat":time.time(),"rooms":{identifier:{"state":"recording"}}})
        self.assertEqual(self.client.delete(route,json={"revision":self.revision()},headers=self.headers).status_code,409)
        self.assertTrue(any(r["id"]==identifier for r in self.parts["config"].rooms()))

    def test_restore_duplicate_does_not_overwrite_existing_room(self):
        config=self.parts["config"];url="https://www.youtube.com/@ExampleChannel/live";identifier=room_id(url)
        config.delete_room(identifier,{"revision":self.revision()})
        config.create_room({"revision":self.revision(),"url":url,"name":"new","proxy":"direct"})
        response=self.client.post("/api/deleted-rooms/"+identifier+"/restore",json={"revision":self.revision()},headers=self.headers)
        self.assertEqual(response.status_code,409,response.json)
        self.assertEqual(next(r for r in config.rooms() if r["id"]==identifier)["name"],"new")

    def test_settings_wait_for_safe_apply_and_do_not_change_cookies(self):
        values = self.client.get("/api/settings").json["values"]
        values.update(segment_seconds=1800, record_format="MKV", delete_original=False)
        r = self.client.put("/api/settings", json={"values": values, "revision": self.revision()}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.json)
        self.assertTrue(self.parts["config"].settings()["pending"])
        self.assertIn("视频分段时间(秒) = 3600", (self.paths.config / "config.ini").read_text())
        self.parts["config"].apply_pending()
        self.assertIn("视频分段时间(秒) = 1800", (self.paths.config / "config.ini").read_text())
        self.assertIn("unrelated = preserve-this-value", (self.paths.config / "config.ini").read_text())
        self.assertFalse(self.parts["config"].settings()["pending"])

    def test_retry_is_atomic_and_source_change_protected(self):
        path = "/api/jobs/" + self.job_id + "/retry"
        self.assertEqual(self.client.post(path, json={}, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post(path, json={}, headers=self.headers).status_code, 409)
        db = sqlite3.connect(self.paths.queue);db.execute("UPDATE jobs SET status='failed'");db.commit();db.close()
        with (self.paths.downloads / "fixture.ts").open("ab") as f:
            f.write(b"changed")
        self.assertEqual(self.client.post(path, json={}, headers=self.headers).status_code, 409)

    def test_delete_job_record_preserves_media_dedup_and_persists_across_clients(self):
        source = self.paths.downloads / "fixture.ts"
        output = source.with_suffix(".mp4")
        output.write_bytes(b"retained encoded video")
        original = (source.read_bytes(), output.read_bytes())
        path = "/api/jobs/" + self.job_id
        self.assertEqual(self.client.delete(path, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.delete(path, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/api/jobs?status=failed&search=fixture").json["total"], 0)
        self.assertEqual(self.client.get("/api/dashboard").json["queue"]["failed"], 0)
        self.assertEqual(self.client.post(path + "/retry", json={}, headers=self.headers).status_code, 404)
        with closing(sqlite3.connect(self.paths.queue)) as db, db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (self.job_id,)).fetchone()
            self.assertIsNotNone(row)
            db.execute("INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        second = create_app(self.paths, testing=True).test_client()
        second.post("/api/login", json={"password":"Local-Test-Password-42"})
        self.assertEqual(second.get("/api/jobs").json["items"], [])
        self.assertEqual((source.read_bytes(), output.read_bytes()), original)

    def test_delete_completed_job_keeps_saved_space_and_active_tasks_visible(self):
        with closing(sqlite3.connect(self.paths.queue)) as db, db:
            db.execute("UPDATE jobs SET status='done',size=100,output_size=40")
        path = "/api/jobs/" + self.job_id
        self.assertEqual(self.client.delete(path, headers=self.headers).status_code, 200)
        queue = self.client.get("/api/dashboard").json["queue"]
        self.assertEqual((queue["done"], queue["saved_bytes"]), (0, 60))
        for status in ("queued", "running", "published"):
            with closing(sqlite3.connect(self.paths.queue)) as db, db:
                db.execute("UPDATE jobs SET status=?", (status,))
            self.assertEqual(self.client.get("/api/jobs").json["items"][0]["status"], status)
            self.assertEqual(self.client.delete(path, headers=self.headers).status_code, 409)
            self.assertEqual(self.client.get("/api/dashboard").json["queue"][status], 1)

    def test_delete_job_auth_validation_and_backward_compatible_migration(self):
        path = "/api/jobs/" + self.job_id
        self.assertEqual(self.app.test_client().delete(path).status_code, 401)
        self.assertEqual(self.client.delete(path).status_code, 403)
        self.assertEqual(self.client.delete(path, headers=dict(self.headers, Origin="https://other.example")).status_code, 403)
        self.assertEqual(self.client.delete("/api/jobs/invalid", headers=self.headers).status_code, 422)
        self.assertEqual(self.client.delete("/api/jobs/" + "b"*64, headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get("/api/jobs").json["total"], 1)
        with closing(sqlite3.connect(self.paths.queue)) as db, db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='deleted_jobs'").fetchone())
        self.assertEqual(self.client.delete(path, headers=self.headers).status_code, 200)
        with closing(sqlite3.connect(self.paths.queue)) as db, db:
            self.assertEqual(db.execute("SELECT count(*) FROM deleted_jobs").fetchone()[0], 1)

    def test_delete_and_retry_serialize_without_hidden_active_work(self):
        queue = self.parts["queue"]
        def attempt(operation):
            try:
                operation(self.job_id)
                return 200
            except AppError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [queue.delete_record, queue.retry]))
        self.assertEqual(results.count(200), 1)
        self.assertIn(next(code for code in results if code != 200), (404, 409))
        data = queue.jobs("all", "", 1)
        with closing(sqlite3.connect(self.paths.queue)) as db, db:
            status = db.execute("SELECT status FROM jobs WHERE id=?", (self.job_id,)).fetchone()[0]
        self.assertEqual(data["total"], 1 if status == "queued" else 0)

    def test_bulk_delete_preserves_media_and_fingerprints_and_is_idempotent(self):
        second = "b" * 64
        source, output = self.add_queue_job(second, "done")
        files = [self.paths.downloads / "fixture.ts", source, output]
        before = [(file.read_bytes(), file.stat().st_mtime_ns) for file in files]
        result = self.client.post("/api/jobs/delete-records", json={"ids": [self.job_id, second, second]}, headers=self.headers)
        self.assertEqual((result.status_code, result.json["deleted"]), (200, 2))
        self.assertEqual(self.client.get("/api/jobs").json["total"], 0)
        self.assertEqual([(file.read_bytes(), file.stat().st_mtime_ns) for file in files], before)
        with closing(sqlite3.connect(self.paths.queue)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM deleted_jobs").fetchone()[0], 2)
        self.assertEqual(self.client.post("/api/jobs/delete-records", json={"ids": [self.job_id, second]}, headers=self.headers).status_code, 200)

    def test_bulk_delete_validates_every_record_before_mutating(self):
        second = "b" * 64
        self.add_queue_job(second, "running")
        for status in ("running", "queued", "published"):
            with closing(sqlite3.connect(self.paths.queue)) as db, db:
                db.execute("UPDATE jobs SET status=? WHERE id=?", (status, second))
            result = self.client.post("/api/jobs/delete-records", json={"ids": [self.job_id, second]}, headers=self.headers)
            self.assertEqual(result.status_code, 409)
            self.assertEqual(self.client.get("/api/jobs").json["total"], 2)
        result = self.client.post("/api/jobs/delete-records", json={"ids": [self.job_id, "c" * 64]}, headers=self.headers)
        self.assertEqual(result.status_code, 404)
        with closing(sqlite3.connect(self.paths.queue)) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='deleted_jobs'").fetchone())

    def test_bulk_delete_auth_csrf_and_input_limits(self):
        path = "/api/jobs/delete-records"
        self.assertEqual(self.app.test_client().post(path, json={"ids": [self.job_id]}).status_code, 401)
        self.assertEqual(self.client.post(path, json={"ids": [self.job_id]}).status_code, 403)
        self.assertEqual(self.client.post(path, json={"ids": [self.job_id]}, headers=dict(self.headers, Origin="https://other.example")).status_code, 403)
        for identifiers in (None, {}, [], ["invalid"], [self.job_id, 17], [self.job_id] * 201):
            self.assertEqual(self.client.post(path, json={"ids": identifiers}, headers=self.headers).status_code, 422)
        self.assertEqual(self.client.get("/api/jobs").json["total"], 1)

    def test_bulk_delete_and_retry_are_serialized_without_partial_deletion(self):
        second = "b" * 64
        self.add_queue_job(second, "done")
        queue = self.parts["queue"]
        def attempt(operation):
            try:
                operation(); return 200
            except AppError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [lambda: queue.delete_records([self.job_id, second]), lambda: queue.retry(self.job_id)]))
        self.assertEqual(results.count(200), 1)
        visible = queue.jobs("all", "", 1)["total"]
        self.assertEqual(visible, 0 if results[0] == 200 else 2)

    def test_status_unknown_without_heartbeat_and_inherited_proxy(self):
        data = self.client.get("/api/dashboard").json
        self.assertFalse(data["recorder"]["connected"])
        self.assertTrue(all(r["state"] == "unknown" for r in data["rooms"]))
        routes = {r["platform"]:r["uses_proxy"] for r in data["rooms"]}
        self.assertEqual(routes, {"哔哩哔哩": False, "YouTube": True})
        serialized = json.dumps(data)
        self.assertNotIn("test-secret", serialized)

    def test_queue_filters_and_progress_from_real_fields(self):
        data = self.client.get("/api/jobs?status=failed&search=fixture").json
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["path"], "fixture.ts")
        write_json(self.paths.config / "h265/current-job.json", {"job_id": self.job_id, "duration":100, "started_at":time.time()-1})
        (self.paths.config / "h265/progress.txt").write_text("out_time_us=25000000\nspeed=2.5x\nprogress=continue\n")
        progress = self.client.get("/api/dashboard").json["progress"]
        self.assertEqual(progress["percent"], 25)
        self.assertEqual(progress["speed"], 2.5)

    def test_bridge_proxy_is_per_room_and_record_finish_resets_status(self):
        ns = {"converts_to_mp4":False, "delete_origin_file":True, "recording":set(), "version":"test", "logger":logging.getLogger("test")}
        bridge = RecorderBridge(ns, self.paths)
        url = "https://live.bilibili.com/100001"
        write_json(self.parts["config"].metadata, {url:{"proxy":"direct"}})
        self.assertIsNone(bridge.resolve_proxy(url, "http://test-proxy:1234"))
        write_json(self.parts["config"].metadata, {url:{"proxy":"shared"}})
        self.assertEqual(bridge.resolve_proxy(url, None), self.parts["config"].proxy_url())
        self.parts["config"].update_room(room_id(url), {"revision":self.revision(), "quality":"高清", "name":"自定义备注"})
        self.assertEqual(bridge.current_room_data(("原画",url,"旧备注")), ("高清",url,"自定义备注"))
        wrapped = bridge.wrap_record(lambda *a: False)
        wrapped("序号1 B站测试主播", url, ["ffmpeg", str(self.paths.downloads / "example_%03d.ts")], "TS")
        self.assertEqual(bridge.rooms[room_id(url)]["state"], "checking")
        self.assertFalse(bridge.active)
        self.assertEqual(bridge.resolve_proxy("https://custom.example/test.m3u8", "fallback"), "fallback")

    def test_no_unprotected_static_or_traversal_access_to_secrets(self):
        self.assertEqual(self.client.get("/static/../../state/web.sqlite3").status_code, 404)
        self.assertEqual(self.client.get("/static/../auth.py").status_code, 404)
        with self.client.get("/") as response:
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
