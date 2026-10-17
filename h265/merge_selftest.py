"""Real HEVC concatenation, verification failures and crash recovery in disposable folders."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--processor", type=Path, default=Path(__file__).with_name("postprocess.py"))
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("merge_selftest_processor", args.processor)
    p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
    with tempfile.TemporaryDirectory(prefix="dlr-merge-ffmpeg-test-") as directory:
        root = Path(directory).resolve(); seed = root / "seed"; seed.mkdir()
        subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "6", "-c:v", "libx265",
                        "-preset", "ultrafast", "-x265-params", "pools=1:frame-threads=1:log-level=error", "-g", "20", "-c:a", "aac",
                        "-f", "segment", "-segment_time", "2", "-reset_timestamps", "1", str(seed / "part_%03d.mp4")],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=45)
        fixtures = sorted(seed.glob("*.mp4")); assert len(fixtures) == 3

        def case(name, delete):
            folder = root / name / "recordings"; folder.mkdir(parents=True)
            p.HOME = root / name / "config/h265"; p.HOME.mkdir(parents=True)
            p.ROOT, p.DB = folder, p.HOME / "queue.sqlite3"
            inputs = []
            for index, source in enumerate(fixtures):
                target = folder / ("中文 ' %%25_2026-10-17_10-%02d-00.mp4" % index)
                shutil.copy2(source, target); inputs.append(p.merge_queue.file_snapshot(target, [folder]))
            entry = {"id": name.encode().hex().ljust(64, "0"), "parts": json.dumps([{"source": item["path"]} for item in inputs])}
            with p.connect() as db:
                p.merge_queue.migrate(db)
                identifier = p.merge_queue.enqueue_merge(db, entry, inputs, delete)
                job = dict(db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone())
            return inputs, job

        def state(job):
            with p.connect() as db:
                return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())

        inputs, job = case("success", True)
        p.encode(job); done = state(job)
        assert done["status"] == "done" and all(not Path(item["path"]).exists() for item in inputs)
        assert p.probe(done["output"])[1]["codec_name"] == "hevc"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", done["output"], "-f", "null", "-"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
        print("PASS HEVC + audio parts merge losslessly, all packets verify, output decodes; delete only after verification")

        inputs, job = case("keep", False)
        p.encode(job); assert all(p.merge_queue.file_snapshot(item["path"], [p.ROOT]) == item for item in inputs)
        print("PASS keep-parts policy preserves every input")

        inputs, job = case("deleted-after-queue", True)
        Path(inputs[1]["path"]).unlink()
        p.encode(job); done = state(job)
        assert done["status"] == "done" and "_缺1P" in done["output"]
        assert not Path(inputs[0]["path"]).exists() and not Path(inputs[2]["path"]).exists()
        print("PASS a part deleted after queueing is skipped; remaining parts merge and missing part is recorded")

        inputs, job = case("verification-failure", True)
        original = p.mapped_packet_counts
        with patch.object(p, "mapped_packet_counts", side_effect=lambda file: [1, 1] if ".tmp." in str(file) else original(file)):
            try:
                p.encode(job); raise AssertionError("Incomplete merged output was accepted")
            except ValueError as error:
                assert "packet counts differ" in str(error), error
        assert all(Path(item["path"]).exists() for item in inputs)
        assert not Path(job["source"]).exists() and not list(p.ROOT.glob(".*.tmp.mp4"))
        print("PASS incomplete merged output is rejected and all parts retained")

        inputs, job = case("collision", True)
        Path(job["source"]).write_bytes(b"Existing user recording")
        try:
            p.encode(job); raise AssertionError("Existing merged destination was overwritten")
        except FileExistsError:
            pass
        assert Path(job["source"]).read_bytes() == b"Existing user recording"
        assert all(Path(item["path"]).exists() for item in inputs)
        print("PASS existing destinations are never overwritten")

        inputs, job = case("recover-publication", True)
        with patch.object(p.merge_encoder, "finish", side_effect=RuntimeError("simulated shutdown after publication")):
            try:
                p.encode(job)
            except RuntimeError:
                pass
        published = state(job); assert published["status"] == "published"
        Path(inputs[0]["path"]).unlink()  # Recovery after one verified deletion.
        p.finish_published(published)
        assert state(job)["status"] == "done" and all(not Path(item["path"]).exists() for item in inputs)
        print("PASS published-output crash recovery completes remaining verified deletions without re-merging")


if __name__ == "__main__":
    main()
