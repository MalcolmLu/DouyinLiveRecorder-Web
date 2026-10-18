#!/usr/bin/env python3
"""Real FFmpeg queue-control checks, confined to disposable temporary recordings."""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time


def load_processor(path):
    spec = importlib.util.spec_from_file_location("queue_test_processor", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wait(predicate, description, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.1)
    raise AssertionError("Timed out: " + description)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--processor", default=str(Path(__file__).with_name("postprocess.py")))
    args = parser.parse_args()
    processor_path = Path(args.processor).resolve(strict=True)
    processor = load_processor(processor_path)
    controls = processor.queue_control
    children = []
    with tempfile.TemporaryDirectory(prefix="dlr-queue-ffmpeg-test-") as directory:
        # macOS reports open descriptors under /private/var even when tempfile
        # returns the /var alias. Compare canonical paths on every platform.
        root = Path(directory).resolve()
        seed = root / "seed.ts"
        subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-n", "-f", "lavfi",
                        "-i", "testsrc2=size=160x90:rate=12", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                        "-t", "8", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", "-c:a", "aac",
                        "-f", "mpegts", str(seed)], check=True, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)

        def setup(name):
            case = root / name; home = case / "config/h265"; downloads = case / "downloads"
            home.mkdir(parents=True); downloads.mkdir()
            (home / "settings.json").write_text(json.dumps({"enabled": False, "cpu_threads": 1}))
            processor.HOME, processor.ROOT, processor.DB = home, downloads, home / "queue.sqlite3"
            return case, home, downloads

        @contextmanager
        def db(home):
            connection = sqlite3.connect(home / "queue.sqlite3", timeout=5)
            connection.row_factory = sqlite3.Row
            try:
                yield connection
                connection.commit()
            finally:
                connection.close()

        def state(home):
            with db(home) as connection:
                return controls.snapshot(connection)

        def status(home, identifier):
            with db(home) as connection:
                return connection.execute("SELECT status FROM jobs WHERE id=?", (identifier,)).fetchone()[0]

        def command(home, action):
            with db(home) as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = controls.snapshot(connection)
                controls.request_control(connection, action, current["control_revision"], current["current_job_id"])

        def start(home, downloads, paced):
            code = """import importlib.util,sys
from pathlib import Path
path,home,downloads,paced=sys.argv[1:]
spec=importlib.util.spec_from_file_location('isolated_queue_worker',path)
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
p.HOME=Path(home);p.ROOT=Path(downloads);p.DB=p.HOME/'queue.sqlite3'
original=p.run_process
def run(command,**kwargs):
    if paced=='yes' and '-stats_period' in command:
        command=list(command);command.insert(command.index('-i'),'-re')
        command[command.index('-stats_period')+1]='0.1'
    return original(command,**kwargs)
p.run_process=run
p.worker()
"""
            with (home / "test-worker.log").open("ab") as log:
                environment = os.environ.copy()
                environment["GUI_STATE_DIR"] = str(home.parent / "gui/state")
                process = subprocess.Popen([sys.executable, "-c", code, str(processor_path), str(home), str(downloads),
                                            "yes" if paced else "no"], stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=environment)
            children.append(process)
            return process

        def stop_worker(process):
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)
                raise AssertionError("Worker did not release its children and exit")

        try:
            _, home, downloads = setup("order")
            ids = []
            for name in ("first.ts", "second.ts", "third.ts"):
                source = downloads / name; source.write_bytes(seed.read_bytes())
                ids.append(processor.enqueue(source, False))
            with db(home) as connection:
                connection.execute("UPDATE queue_control SET mode='paused'")
            worker = start(home, downloads, False)
            wait(lambda: state(home)["phase"] == "paused", "initial persisted queue pause")
            with db(home) as connection:
                connection.execute("BEGIN IMMEDIATE")
                controls.reorder(connection, controls.snapshot(connection)["order_revision"], ids[2], "top")
            command(home, "resume")
            wait(lambda: all(status(home, identifier) == "done" for identifier in ids), "actual FFmpeg queue completion")
            with db(home) as connection:
                actual = [row[0] for row in connection.execute("SELECT id FROM jobs ORDER BY updated")]
            assert actual == [ids[2], ids[0], ids[1]], actual
            for source in downloads.glob("*.ts"):
                assert source.exists() and source.with_suffix(".mp4").is_file()
                subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(source.with_suffix(".mp4")),
                                "-f", "null", "-"], check=True, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, timeout=20)
            stop_worker(worker)
            print("PASS real FFmpeg execution follows the persisted custom order; outputs decode")

            _, home, downloads = setup("pause-stop")
            ids, originals = [], {}
            for name in ("stop-current.ts", "wait-next.ts"):
                source = downloads / name; source.write_bytes(seed.read_bytes())
                ids.append(processor.enqueue(source, True))
                originals[source] = (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mtime_ns)
            with db(home) as connection:
                connection.execute("UPDATE queue_control SET mode='paused'")
            worker = start(home, downloads, True)
            wait(lambda: state(home)["phase"] == "paused", "worker paused before first claim")
            command(home, "resume")
            progress = home / "progress.txt"
            wait(lambda: state(home)["phase"] == "encoding" and progress.exists() and "progress=" in progress.read_text(),
                 "real encoding progress")
            command(home, "pause")
            wait(lambda: state(home)["phase"] == "paused", "FFmpeg suspension acknowledgment")
            frozen = progress.read_bytes(); time.sleep(.6)
            assert progress.read_bytes() == frozen, "FFmpeg advanced while paused"
            command(home, "resume")
            wait(lambda: progress.read_bytes() != frozen, "FFmpeg resumes its existing progress")
            command(home, "pause")
            wait(lambda: state(home)["phase"] == "paused", "second pause")
            command(home, "stop")
            wait(lambda: status(home, ids[0]) == "stopped" and state(home)["phase"] == "stopped", "stop from suspended FFmpeg")
            assert status(home, ids[1]) == "queued"
            assert not list(downloads.glob("*.mp4"))
            assert not list(downloads.glob(".*.tmp.mp4"))
            assert originals == {source: (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mtime_ns)
                                 for source in originals}
            stop_worker(worker)
            worker = start(home, downloads, True)
            wait(lambda: state(home)["phase"] == "stopped", "stop remains effective after worker restart")
            assert status(home, ids[0]) == "stopped" and status(home, ids[1]) == "queued"
            command(home, "resume")
            wait(lambda: state(home)["current_job_id"] == ids[1], "resume proceeds to remaining queued task")
            assert status(home, ids[0]) == "stopped"
            command(home, "stop")
            wait(lambda: status(home, ids[1]) == "stopped", "second task safely stops")
            stop_worker(worker)
            print("PASS pause freezes real FFmpeg, resume continues, stop reaps child and retains source byte-for-byte")
            print("PASS unfinished output removed, later jobs wait, stop persists after restart, resume skips stopped jobs")

            _, home, downloads = setup("merge-controls")
            originals = {}
            for name in ("merge-first.ts", "merge-second.ts"):
                source = downloads / name; source.write_bytes(seed.read_bytes())
                originals[source] = hashlib.sha256(source.read_bytes()).hexdigest()
            with processor.connect() as connection:
                processor.merge_queue.migrate(connection)
                identifier = processor.merge_queue.enqueue_merge(connection,
                    {"id": "a" * 64, "parts": json.dumps([{"source": str(path)} for path in originals])},
                    [processor.merge_queue.file_snapshot(path, [downloads]) for path in originals], True)
                connection.execute("UPDATE queue_control SET mode='paused'")
            worker = start(home, downloads, True)
            wait(lambda: state(home)["phase"] == "paused", "merge initial queue pause")
            command(home, "resume"); progress = home / "progress.txt"
            wait(lambda: state(home)["phase"] == "encoding" and progress.exists() and "progress=" in progress.read_text(), "merge FFmpeg progress")
            command(home, "pause"); wait(lambda: state(home)["phase"] == "paused", "merge suspension")
            frozen = progress.read_bytes(); time.sleep(.6)
            assert progress.read_bytes() == frozen
            command(home, "stop")
            wait(lambda: status(home, identifier) == "stopped", "merge stop")
            assert originals == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in originals}
            assert not list(downloads.glob(".*.tmp.*"))
            stop_worker(worker)
            print("PASS real merge pauses and stops through queue controls; every part retained and incomplete merge removed")

            _, home, downloads = setup("live-segments")
            state_dir = home.parent / "gui/state"; state_dir.mkdir(parents=True)
            (home.parent / "config.ini").write_text("[录制设置]\n录制完成后自动转为mp4格式 = 是\n追加格式后删除原文件 = 否\n", encoding="utf-8")
            began = time.time()
            pattern = "live_%Y-%m-%d_%H-%M-%S.ts"
            with processor.connect():
                pass
            def heartbeat():
                temporary = state_dir / "runtime.new"
                temporary.write_text(json.dumps({"heartbeat": time.time(), "rooms": {
                    "isolated-live": {"state": "recording", "file": pattern, "started_at": began}}}))
                os.replace(temporary, state_dir / "runtime.json")
            heartbeat()
            with (home / "test-recorder.log").open("ab") as log:
                recorder = subprocess.Popen(["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-re",
                    "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10", "-t", "18", "-c:v", "libx264",
                    "-preset", "ultrafast", "-threads", "1", "-g", "10", "-f", "segment", "-segment_time", "2",
                    "-reset_timestamps", "1", "-strftime", "1", str(downloads / pattern)],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            children.append(recorder)
            worker = start(home, downloads, False)
            converted_during_recording = False
            while recorder.poll() is None:
                heartbeat()
                with db(home) as connection:
                    done = connection.execute("SELECT count(*) FROM jobs WHERE status='done'").fetchone()[0]
                if done:
                    if Path("/proc").is_dir():
                        opened = set()
                        for descriptor in Path("/proc").joinpath(str(recorder.pid), "fd").iterdir():
                            try:
                                opened.add(os.path.normcase(str(Path(os.readlink(descriptor)).resolve())))
                            except OSError:
                                pass
                    else:
                        import psutil
                        opened = {os.path.normcase(str(Path(item.path).resolve()))
                                  for item in psutil.Process(recorder.pid).open_files()}
                    active = [file for file in downloads.glob("*.ts")
                              if os.path.normcase(str(file.resolve())) in opened]
                    if not active:
                        # A segment can close between enumerating the process
                        # and its descriptors. Require an observed open part.
                        time.sleep(.05)
                        continue
                    converted_during_recording = True
                    with db(home) as connection:
                        for file in active:
                            assert not connection.execute("SELECT 1 FROM jobs WHERE source=?", (str(file),)).fetchone(), "Open recording part was queued"
                    break
                time.sleep(.2)
            assert converted_during_recording, "Closed parts did not transcode until the whole session ended"
            while recorder.poll() is None:
                heartbeat(); time.sleep(.2)
            assert recorder.returncode == 0
            # The collector's existing session-end callback queues the final part.
            (state_dir / "runtime.json").write_text(json.dumps({"heartbeat": time.time(), "rooms": {}}))
            parts = list(downloads.glob("*.ts"))
            for file in parts:
                processor.enqueue(file, False)
            wait(lambda: all(status(home, processor.enqueue(file, False)) == "done" for file in parts), "final session-end part completion")
            with db(home) as connection:
                assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == len(parts), "Session-end callbacks duplicated segment jobs"
            stop_worker(worker)
            print("PASS closed segments transcode while real recording continues; open part protected and final callback deduplicated")
        except Exception:
            for log in root.glob("*/config/h265/test-worker.log"):
                print(log.read_text(errors="replace")[-4000:], file=sys.stderr)
            raise
        finally:
            for process in children:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=5)


if __name__ == "__main__":
    main()
