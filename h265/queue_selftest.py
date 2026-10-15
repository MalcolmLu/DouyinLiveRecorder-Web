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
        root = Path(directory)
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
                process = subprocess.Popen([sys.executable, "-c", code, str(processor_path), str(home), str(downloads),
                                            "yes" if paced else "no"], stdin=subprocess.DEVNULL, stdout=log, stderr=log)
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
