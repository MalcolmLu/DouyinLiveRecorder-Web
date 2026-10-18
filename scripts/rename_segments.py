"""Plan and safely apply legacy segment timestamp names. No media is re-encoded."""
import argparse
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gui"))
from platform_utils import acquire_lock, release_lock
from shared import atomic_write, read_json

OLD_NAME = re.compile(r"^(.*)_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})_(\d+)((?:\.H265|\.converted)?\.(?:mp4|ts|mkv|flv|mp3|m4a))$")
DATE_FORMAT = "%Y-%m-%d_%H-%M-%S"


def probe_duration(path):
    raw = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "json", str(path)], stderr=subprocess.PIPE, timeout=60)
    value = float(json.loads(raw)["format"]["duration"])
    if not math.isfinite(value) or not 0 < value < 7 * 86400:
        raise ValueError("Invalid recording duration")
    return value


def build_plan(root, segment_seconds=3600, probe=probe_duration):
    root = Path(root).resolve()
    groups = defaultdict(lambda: defaultdict(list))
    for path in sorted(root.rglob("*")):
        match = OLD_NAME.fullmatch(path.name)
        if path.is_symlink() or not path.is_file() or not match or path.name.startswith("."):
            continue
        if not path.resolve().is_relative_to(root):
            continue
        index = int(match[3])
        if index > 100000:
            raise ValueError("Unexpected segment index")
        groups[(str(path.parent), match[1], match[2])][index].append(path)
    files, sessions, targets = [], [], set()
    for (directory, author, start), parts in sorted(groups.items()):
        moment, offset, estimates = datetime.strptime(start, DATE_FORMAT), 0.0, []
        for index in range(max(parts) + 1):
            timestamp = (moment + timedelta(seconds=offset)).strftime(DATE_FORMAT)
            candidates = parts.get(index, [])
            for path in candidates:
                match = OLD_NAME.fullmatch(path.name)
                target = path.with_name(author + "_" + timestamp + match[4])
                if target.exists() or str(target) in targets:
                    raise FileExistsError("Destination already exists: " + str(target))
                targets.add(str(target))
                stat = path.stat()
                files.append({"old": str(path), "new": str(target), "size": stat.st_size,
                              "mtime_ns": stat.st_mtime_ns, "inode": stat.st_ino})
            duration, method = float(segment_seconds), "configured-duration fallback (missing segment)"
            if candidates:
                preferred = min(candidates, key=lambda p: (p.suffix.lower() != ".ts", ".H265." in p.name, p.name))
                try:
                    duration, method = probe(preferred), "measured duration"
                except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                    method = "configured-duration fallback (unreadable duration)"
            estimates.append({"index": index, "start": timestamp, "duration": duration, "method": method})
            offset += duration
        sessions.append({"directory": directory, "author": author, "session_start": start, "parts": estimates})
    return {"version": 1, "root": str(root), "created": time.time(), "status": "planned",
            "estimation": "session filename time plus cumulative measured media duration; configured duration for missing/unreadable parts",
            "files": files, "sessions": sessions}


def mapped_path(value, sessions):
    if not value:
        return value
    path = Path(value)
    match = OLD_NAME.fullmatch(path.name)
    if not match:
        return value
    key = (str(path.parent), match[1], match[2], int(match[3]))
    timestamp = sessions.get(key)
    return str(path.with_name(match[1] + "_" + timestamp + match[4])) if timestamp else value


def apply_plan(plan, config, manifest):
    root, config, manifest = Path(plan["root"]).resolve(), Path(config).resolve(), Path(manifest)
    if plan.get("status") != "planned":
        raise ValueError("Plan has already been applied or is not ready")
    runtime = read_json(config / "gui/state/runtime.json", {})
    if any(r.get("state") == "recording" for r in runtime.get("rooms", {}).values()):
        raise RuntimeError("Wait until current recordings finish before renaming")
    wanted = {entry["old"] for entry in plan["files"]}
    if Path("/proc").is_dir():
        for comm in Path("/proc").glob("[0-9]*/comm"):
            try:
                if comm.read_text().strip() == "ffmpeg":
                    for fd in (comm.parent / "fd").iterdir():
                        try:
                            if os.readlink(fd) in wanted:
                                raise RuntimeError("A recording file is still open by FFmpeg")
                        except (FileNotFoundError, PermissionError):
                            continue
            except (FileNotFoundError, PermissionError):
                continue
    for entry in plan["files"]:
        old, new = Path(entry["old"]), Path(entry["new"])
        if not old.resolve().is_relative_to(root) or new.parent.resolve() != old.parent.resolve():
            raise ValueError("Unsafe rename path")
        stat = old.stat()
        if (stat.st_size, stat.st_mtime_ns, stat.st_ino) != (entry["size"], entry["mtime_ns"], entry["inode"]) or new.exists():
            raise RuntimeError("File changed or destination exists; build a new plan")
    sessions = {(s["directory"], s["author"], s["session_start"], p["index"]): p["start"]
                for s in plan["sessions"] for p in s["parts"]}
    lock = (config / "h265/worker.lock").open("a")
    try:
        acquire_lock(lock, blocking=False)
    except BaseException:
        lock.close()
        raise
    db, renamed = None, []
    committed = False
    try:
        database = config / "h265/queue.sqlite3"
        db = sqlite3.connect(database, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running','published')").fetchone()[0]:
            raise RuntimeError("Wait until the transcode queue is idle")
        backup = manifest.with_suffix(".queue-before.sqlite3")
        if backup.exists():
            raise FileExistsError("A queue backup already exists for this plan")
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as reader:
            with closing(sqlite3.connect(backup)) as destination:
                reader.backup(destination)
        updates = []
        for row in db.execute("SELECT * FROM jobs"):
            source, output = mapped_path(row["source"], sessions), mapped_path(row["output"], sessions)
            if source == row["source"] and output == row["output"]:
                continue
            identifier = hashlib.sha256(f'{source}\0{row["size"]}\0{row["mtime"]}'.encode()).hexdigest()
            updates.append((identifier, source, output, row["id"]))
        ids = [row[0] for row in updates]
        old_ids = {row[3] for row in updates}
        if len(ids) != len(set(ids)) or any(db.execute("SELECT id FROM jobs WHERE id=?", (identifier,)).fetchone()
                                         and identifier not in old_ids for identifier in ids):
            raise RuntimeError("Queue identity collision; no files renamed")
        tombstones = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='deleted_jobs'").fetchone()
        for entry in plan["files"]:
            old, new = Path(entry["old"]), Path(entry["new"])
            # Hard-link then unlink avoids overwriting any file created after preflight.
            os.link(old, new)
            try:
                old.unlink()
            except BaseException:
                new.unlink()
                raise
            renamed.append(entry)
        for identifier, source, output, old_id in updates:
            db.execute("UPDATE jobs SET id=?,source=?,output=?,updated=? WHERE id=?",
                       (identifier, source, output, time.time(), old_id))
            if tombstones:
                db.execute("UPDATE deleted_jobs SET id=? WHERE id=?", (identifier, old_id))
        db.commit(); committed = True
        plan.update(status="applied", applied=time.time(), queue_backup=str(backup), updated_jobs=len(updates))
        atomic_write(manifest, json.dumps(plan, ensure_ascii=False, indent=2))
        for entry in renamed:
            stat = Path(entry["new"]).stat()
            assert (stat.st_ino, stat.st_size, stat.st_mtime_ns) == (entry["inode"], entry["size"], entry["mtime_ns"])
        return {"renamed": len(renamed), "updated_jobs": len(updates), "manifest": str(manifest), "queue_backup": str(backup)}
    except BaseException:
        if not committed:
            if db is not None:
                db.rollback()
            for entry in reversed(renamed):
                os.link(entry["new"], entry["old"])
                Path(entry["new"]).unlink()
        raise
    finally:
        if db is not None:
            db.close()
        release_lock(lock); lock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/app/downloads"))
    parser.add_argument("--config", type=Path, default=Path("/app/config"))
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--segment-seconds", type=int, default=3600)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply:
        result = apply_plan(read_json(args.plan), args.config, args.plan)
    else:
        if args.plan.exists():
            raise FileExistsError("Use a new plan filename; existing manifest is preserved")
        plan = build_plan(args.root, args.segment_seconds)
        atomic_write(args.plan, json.dumps(plan, ensure_ascii=False, indent=2))
        result = {"files": len(plan["files"]), "sessions": len(plan["sessions"]),
                  "fallback_parts": sum(p["method"] != "measured duration" for s in plan["sessions"] for p in s["parts"]),
                  "examples": [{"old": Path(e["old"]).name, "new": Path(e["new"]).name} for e in plan["files"][:5]]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
