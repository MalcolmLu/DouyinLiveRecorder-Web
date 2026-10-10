"""Room isolation and authenticated byte-range playback against disposable files."""
from pathlib import Path
import os
import tempfile
import unittest

from app import create_app
from shared import Paths, room_id, write_json


class RecordingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dlr-library-test-")
        self.root = Path(self.temp.name).resolve()
        config, downloads = self.root / "config", self.root / "downloads"
        config.mkdir(); downloads.mkdir()
        self.paths = Paths(config, downloads, config / "gui/state")
        (config / "config.ini").write_text("[录制设置]\n保存文件夹是否以作者区分 = 是\n", encoding="utf-8")
        (config / "URL_config.ini").write_text(
            "https://live.bilibili.com/100001,主播: 测试主播\n"
            "https://live.bilibili.com/100002,主播: 其他主播\n", encoding="utf-8")
        self.identifier = room_id("https://live.bilibili.com/100001")
        self.folder = downloads / "B站直播/测试主播"
        self.folder.mkdir(parents=True)
        self.name = "测试主播_中文 & %23.mp4"
        self.media = self.folder / self.name
        self.media.write_bytes(b"0123456789abcdef")
        (self.folder / "private.txt").write_text("not a recording")
        (self.folder / ".hidden.mp4").write_bytes(b"hidden")
        (self.folder / "unfinished.tmp.mp4").write_bytes(b"unfinished")
        (self.folder / "2026-10-10").mkdir()
        (self.folder / "2026-10-10/分P_001.ts").write_bytes(b"segment")
        other = downloads / "B站直播/其他主播"
        other.mkdir(); (other / "other.mp4").write_bytes(b"other room")
        self.token = "0/B站直播/测试主播/" + self.name
        self.endpoint = "/api/rooms/" + self.identifier + "/recordings"
        self.app = create_app(self.paths, testing=True)
        self.app.extensions["recorder_web"]["auth"].set_password("Local-Media-Test-42")
        self.client = self.app.test_client()
        self.client.post("/api/login", json={"password": "Local-Media-Test-42"})

    def tearDown(self):
        self.temp.cleanup()

    def test_listing_and_media_require_authentication(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get(self.endpoint).status_code, 401)
        self.assertEqual(anonymous.get(self.endpoint + "/media", query_string={"path": self.token}).status_code, 401)

    def test_room_folder_nested_navigation_and_private_file_exclusion(self):
        response = self.client.get(self.endpoint)
        self.assertEqual(response.status_code, 200, response.json)
        data = response.json
        self.assertEqual(data["folder"], "0/B站直播/测试主播")
        self.assertEqual([i["name"] for i in data["items"]], ["2026-10-10", self.name])
        self.assertIsNone(data["parent"])
        child = self.client.get(self.endpoint, query_string={"folder": data["items"][0]["path"]}).json
        self.assertEqual(child["parent"], data["folder"])
        self.assertEqual(child["items"][0]["name"], "分P_001.ts")
        self.assertEqual(len(child["breadcrumbs"]), 2)

    def test_byte_ranges_head_download_and_unicode_names(self):
        response = self.client.get(self.endpoint + "/media", query_string={"path": self.token}, headers={"Range": "bytes=4-7"})
        try:
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.data, b"4567")
            self.assertEqual(response.headers["Content-Range"], "bytes 4-7/16")
            self.assertEqual(response.headers["Accept-Ranges"], "bytes")
            self.assertEqual(response.mimetype, "video/mp4")
        finally:
            response.close()
        head = self.client.head(self.endpoint + "/media", query_string={"path": self.token})
        self.assertEqual(head.headers["Content-Length"], "16")
        self.assertEqual(head.data, b"")
        head.close()
        download = self.client.get(self.endpoint + "/media", query_string={"path": self.token, "download": "1"})
        self.assertIn("attachment;", download.headers["Content-Disposition"])
        self.assertIn("filename*=UTF-8''", download.headers["Content-Disposition"])
        download.close()

    def test_traversal_other_rooms_hidden_files_and_missing_files_are_denied(self):
        for token in ("../config/config.ini", "/etc/passwd", "0/B站直播/测试主播/../其他主播/other.mp4", "0\\B站直播\\测试主播\\x.mp4"):
            self.assertEqual(self.client.get(self.endpoint + "/media", query_string={"path": token}).status_code, 422)
        for token in ("0/B站直播/其他主播/other.mp4", "0/B站直播/测试主播/private.txt", "0/B站直播/测试主播/.hidden.mp4", "0/B站直播/测试主播/gone.mp4"):
            self.assertEqual(self.client.get(self.endpoint + "/media", query_string={"path": token}).status_code, 404)
        self.assertEqual(self.client.get("/api/rooms/unknown/recordings").status_code, 404)

    @unittest.skipIf(os.name == "nt", "Creating test symlinks requires Windows developer privileges")
    def test_symlinks_cannot_escape_the_recording_folder(self):
        secret = self.root / "outside.mp4"; secret.write_bytes(b"private")
        (self.folder / "escape.mp4").symlink_to(secret)
        self.assertNotIn("escape.mp4", str(self.client.get(self.endpoint).json))
        self.assertEqual(self.client.get(self.endpoint + "/media", query_string={"path": "0/B站直播/测试主播/escape.mp4"}).status_code, 404)

    def test_pagination_and_files_replaced_by_transcoding(self):
        for n in range(30):
            (self.folder / ("part_%03d.mp4" % n)).write_bytes(b"video")
        first = self.client.get(self.endpoint).json
        second = self.client.get(self.endpoint, query_string={"page": 2}).json
        self.assertEqual(first["total"], 32)
        self.assertEqual(len(first["items"]), 25)
        self.assertEqual(len(second["items"]), 7)
        self.media.unlink()
        self.assertEqual(self.client.get(self.endpoint + "/media", query_string={"path": self.token}).status_code, 404)
        self.assertEqual(self.client.get(self.endpoint, query_string={"page": "bad"}).status_code, 422)

    def test_previous_desktop_recording_root_remains_accessible(self):
        old = self.root / "old-recordings"
        folder = old / "B站直播/测试主播"; folder.mkdir(parents=True)
        (folder / "old.mp4").write_bytes(b"previous recording")
        write_json(self.paths.state / "desktop-settings.json", {"recording_roots": [str(old)]})
        data = self.client.get(self.endpoint).json
        self.assertEqual(data["total"], 2)
        old_token = next(i["path"] for i in data["items"] if i["path"].startswith("1/"))
        self.assertEqual(self.client.get(self.endpoint, query_string={"folder": old_token}).json["items"][0]["name"], "old.mp4")

    def test_shared_platform_folder_filters_media_by_author(self):
        (self.paths.config / "config.ini").write_text("[录制设置]\n保存文件夹是否以作者区分 = 否\n", encoding="utf-8")
        base = self.paths.downloads / "B站直播"
        (base / "测试主播_001.mp4").write_bytes(b"mine")
        (base / "其他主播_001.mp4").write_bytes(b"other")
        files = [i["name"] for i in self.client.get(self.endpoint).json["items"] if i["kind"] == "file"]
        self.assertEqual(files, ["测试主播_001.mp4"])
        self.assertEqual(self.client.get(self.endpoint + "/media", query_string={"path": "0/B站直播/其他主播_001.mp4"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
