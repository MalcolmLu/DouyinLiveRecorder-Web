"""Checks for standalone initialization and upstream adapter integration."""
import ast
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from shared import Paths
from repository import QueueRepository
from settings_service import ConfigService
from repository import ConfigRepository

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("project_setup", ROOT / "scripts/setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class PackagingTests(unittest.TestCase):
    def test_initialize_defaults_and_preserve_existing_private_config(self):
        with tempfile.TemporaryDirectory(prefix="dlr-setup-test-") as name:
            root = Path(name)
            paths = Paths(root / "config", root / "downloads", root / "config/gui/state")
            setup.initialize(paths)
            config = ConfigService(ConfigRepository(paths))
            values = config.settings()["values"]
            self.assertEqual((values["segment_seconds"],values["record_format"],values["post_format"]),
                             (3600,"TS","mp4"))
            self.assertTrue(values["delete_original"] and values["h265"])
            self.assertEqual(config.rooms(), [])
            self.assertFalse(config.proxy_public()["configured"])
            self.assertEqual(config.settings()["encoder"], "hevc_qsv")
            private = paths.config / "config.ini"
            private.write_text(private.read_text() + "\n# private edit\n")
            saved = private.read_bytes()
            setup.initialize(paths)
            self.assertEqual(private.read_bytes(), saved)
            self.assertEqual(QueueRepository(paths).jobs("all","",1)["total"], 0)

    def test_concurrent_first_start_uses_complete_templates(self):
        with tempfile.TemporaryDirectory(prefix="dlr-setup-test-") as name:
            root = Path(name)
            paths = Paths(root / "config", root / "downloads", root / "config/gui/state")
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(setup.initialize, [paths,paths]))
            self.assertEqual((paths.config / "config.ini").read_bytes(), (ROOT / "defaults/config.ini").read_bytes())
            self.assertEqual(json.loads((paths.config / "h265/settings.json").read_text())["encoder"], "hevc_qsv")

    def test_main_installs_adapter_before_monitor_loop(self):
        source = (ROOT / "main.py").read_text()
        tree = ast.parse(source)
        names = {node.name for node in tree.body if isinstance(node,ast.FunctionDef)}
        self.assertTrue({"start_record","check_subprocess","read_config_value","converts_mp4"}.issubset(names))
        self.assertEqual(source.count("_web = _recorder_web_bridge.install(globals())"),1)
        self.assertIn("os.path.join(script_path, 'gui')",source)
        self.assertIn("_web.current_room_data(url_data)",source)
        self.assertIn("_web.resolve_proxy(record_url, proxy_address)",source)
        self.assertIn("_web.observed(record_url,",source)
        self.assertLess(source.index("_web = _recorder_web_bridge.install"),source.index('options = {"是": True'))
        self.assertTrue((ROOT / "gui/static/index.html").is_file())
        self.assertTrue((ROOT / "h265/postprocess.py").is_file())

    def test_public_defaults_have_no_cookie_or_proxy_credentials(self):
        import configparser
        config = configparser.RawConfigParser()
        config.read(ROOT / "defaults/config.ini")
        self.assertEqual(config.get("录制设置","代理地址"),"")
        self.assertTrue(all(not value for _,value in config.items("Cookie")))
        self.assertTrue(all(not value for _,value in config.items("Authorization")))

    def test_certificate_creation_preserves_existing_key_pair(self):
        with tempfile.TemporaryDirectory(prefix="dlr-certificate-test-") as name:
            state = Path(name)
            setup.certificate(state,"127.0.0.1")
            cert, key = state / "tls/server.crt", state / "tls/server.key"
            saved = (cert.read_bytes(),key.read_bytes())
            self.assertEqual(key.stat().st_mode & 0o777,0o600)
            setup.certificate(state,"127.0.0.1")
            self.assertEqual((cert.read_bytes(),key.read_bytes()),saved)

    def test_certificate_rejects_invalid_host_without_writing(self):
        with tempfile.TemporaryDirectory(prefix="dlr-certificate-test-") as name:
            state = Path(name)
            with self.assertRaises(ValueError):
                setup.certificate(state,"not-an-ip")
            self.assertEqual(list(state.iterdir()),[])


if __name__ == "__main__":
    unittest.main()
