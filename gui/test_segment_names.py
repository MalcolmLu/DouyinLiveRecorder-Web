"""Real-time FFmpeg splitting and compatibility with the postprocess file lookup."""
import datetime
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import time
import unittest

from recorder_bridge import RecorderBridge
from segment_names import TIME_PATTERN, filename_matcher, pattern_directory, timestamp_segments
from shared import Paths


class SegmentNamesTests(unittest.TestCase):
    def test_each_segment_uses_strftime_and_preserves_other_command_options(self):
        original = ["ffmpeg", "-http_proxy", "http://192.0.2.1:7893", "-i", "source", "-f", "segment",
                    "-segment_time", "3600", "/recordings/主播_标题_2026-10-10_10-00-00_%03d.ts"]
        result = timestamp_segments(original)
        self.assertEqual(result[-1], "/recordings/主播_标题_" + TIME_PATTERN + ".ts")
        self.assertEqual(result[-3:-1], ["-strftime", "1"])
        self.assertEqual(result[:-3], original[:-1])
        self.assertTrue(original[-1].endswith("_%03d.ts"))

    def test_non_segmented_files_and_unknown_patterns_are_preserved(self):
        for command in (["ffmpeg", "-f", "mp4", "主播_2026-10-10_10-00-00.mp4"],
                        ["ffmpeg", "-f", "segment", "custom_%03d.ts"]):
            self.assertEqual(timestamp_segments(command), command)

    def test_percent_names_and_old_numeric_patterns_match_safely(self):
        pattern = timestamp_segments(["ffmpeg", "-f", "segment", "/tmp/100%/主播%Y_2026-10-10_10-00-00_%03d.mp4"])[-1]
        self.assertEqual(str(pattern_directory(pattern)), str(Path("/tmp/100%").resolve()))
        self.assertTrue(filename_matcher(pattern)("主播%Y_2026-10-10_10-00-02.mp4"))
        self.assertFalse(filename_matcher(pattern)("其他主播_2026-10-10_10-00-02.mp4"))
        self.assertTrue(filename_matcher("/tmp/主播_%03d.ts")("主播_001.ts"))
        self.assertFalse(filename_matcher("/tmp/主播_%03d.ts")("主播_001Xts"))

    def test_source_lookup_excludes_old_sessions_from_automatic_transcoding(self):
        with tempfile.TemporaryDirectory(prefix="dlr-segment-test-") as temporary:
            root = Path(temporary).resolve()
            bridge = RecorderBridge.__new__(RecorderBridge)
            bridge.paths = Paths(root / "config", root, root / "state")
            old = root / "主播_2026-10-09_10-00-00.ts"; old.write_bytes(b"old")
            os.utime(old, (1, 1))
            started = time.time() - 1
            new = root / "主播_2026-10-10_10-00-00.ts"; new.write_bytes(b"new")
            pattern = str(root / ("主播_" + TIME_PATTERN + ".ts"))
            self.assertEqual(bridge.source_files(pattern, since=started), [new])

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg not installed in this test environment")
    def test_real_time_files_have_distinct_actual_open_times(self):
        with tempfile.TemporaryDirectory(prefix="dlr-segment-real-") as temporary:
            root = Path(temporary)
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi", "-re",
                       "-i", "color=black:size=160x90:rate=10", "-t", "5", "-c:v", "mpeg2video", "-g", "10",
                       "-f", "segment", "-segment_time", "2", "-reset_timestamps", "1",
                       str(root / "测试_2000-01-01_00-00-00_%03d.ts")]
            began = time.time()
            subprocess.run(timestamp_segments(command), check=True, capture_output=True, timeout=20)
            files = sorted(root.glob("*.ts"))
            self.assertEqual(len(files), 3)
            dates = [datetime.datetime.strptime(p.stem.removeprefix("测试_"), "%Y-%m-%d_%H-%M-%S").timestamp() for p in files]
            self.assertGreaterEqual(dates[0], began - 1)
            self.assertGreaterEqual(dates[1] - dates[0], 1)
            self.assertGreaterEqual(dates[2] - dates[1], 1)
            self.assertTrue(all(p.stat().st_size > 0 for p in files))


if __name__ == "__main__":
    unittest.main()
