"""Lossless part concatenation with full packet-count verification before deletion."""
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from merge_queue import file_snapshot
from platform_utils import sync_directory


def roots(processor):
    if hasattr(processor, "recording_roots"):
        return processor.recording_roots(processor.HOME.parent, processor.ROOT)
    return [processor.ROOT]


def unchanged(part, processor, missing=False):
    path = Path(part["path"])
    if missing and not path.exists():
        return
    if file_snapshot(path, roots(processor)) != part or processor.opened_by_ffmpeg(path):
        raise ValueError("Merge input changed or is still open; parts retained")


def media_info(processor, path):
    # Some HE-AAC parts reveal their SBR extension after FFprobe's default
    # analysis window; short probing can misreport 44.1 kHz audio as 22.05 kHz.
    raw = processor.process_output(["ffprobe", "-v", "error", "-analyzeduration", "60000000", "-probesize", "100000000", "-show_format", "-show_streams",
                                    "-show_data_hash", "sha256", "-of", "json", str(path)],
                                   stderr=subprocess.STDOUT, timeout=120)
    info = json.loads(raw)
    streams = info.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("Merge input has no video; parts retained")
    duration = float(info["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Merge input has invalid duration; parts retained")
    mapped = [video, *[s for s in streams if s.get("codec_type") == "audio"]]
    keys = ("codec_type", "codec_name", "profile", "width", "height", "pix_fmt", "sample_rate", "channels", "channel_layout")
    signature = [tuple(stream.get(key) for key in keys) for stream in mapped]
    return duration, signature, [s.get("extradata_hash") for s in mapped]


def finish(processor, job, record):
    destination = Path(record["destination"])
    actual = file_snapshot(destination, roots(processor))
    if (actual["size"], actual["mtime"]) != (job["output_size"], job["output_mtime"]) or not record["verification"]:
        raise ValueError("Published merged output changed; remaining parts retained")
    processor.probe(destination)
    inputs = json.loads(record["inputs"])
    if record["delete_parts"]:
        # Check every remaining input before removing any; missing inputs permit
        # recovery after a crash between two verified part deletions.
        for part in inputs:
            unchanged(part, processor, missing=True)
        for part in inputs:
            path = Path(part["path"])
            if path.exists():
                unchanged(part, processor)
                path.unlink()
        sync_directory(destination.parent)
    with processor.connect() as db:
        db.execute("UPDATE jobs SET status='done',error=NULL,updated=? WHERE id=?", (time.time(), job["id"]))
    processor.LOG.info("Merged %s parts=%d; complete output verified", destination.name, len(inputs))


def encode(processor, job, record):
    inputs = json.loads(record["inputs"])
    missing = [part for part in inputs if not Path(part["path"]).exists()]
    if missing and not record["verification"] and not Path(record["destination"]).exists():
        # A user may remove a part after the merge was queued.
        inputs = [part for part in inputs if Path(part["path"]).exists()]
        if len(inputs) < 2:
            raise ValueError("Fewer than two remaining parts; remaining recording retained")
        skipped = list(dict.fromkeys([*json.loads(record["skipped"]), *[part["path"] for part in missing]]))
        previous = Path(record["destination"])
        stem = re.sub(r"_(?:完整|合并_缺\d+P)$", "", previous.stem)
        record = dict(record, inputs=json.dumps(inputs), skipped=json.dumps(skipped),
                      destination=str(previous.with_name(stem + "_合并_缺%dP" % len(skipped) + previous.suffix)))
        with processor.connect() as db:
            db.execute("UPDATE merge_jobs SET inputs=?,skipped=?,destination=? WHERE id=?",
                       (record["inputs"], record["skipped"], record["destination"], job["id"]))
            db.execute("UPDATE jobs SET source=?,size=? WHERE id=?", (record["destination"], sum(p["size"] for p in inputs), job["id"]))
    if len(inputs) < 2:
        raise ValueError("Merge requires at least two parts")
    destination = Path(record["destination"])
    if destination.exists():
        raise FileExistsError("Merged destination exists; parts retained")
    for part in inputs:
        unchanged(part, processor)
    if len({str(Path(part["path"]).parent) for part in inputs}) != 1 or destination.parent != Path(inputs[0]["path"]).parent:
        raise ValueError("Merge parts must share a recording folder")
    if shutil.disk_usage(destination.parent).free < sum(p["size"] for p in inputs) + 1024**3:
        raise ValueError("Insufficient free space for verified merge; parts retained")
    processor.checkpoint("preparing")
    infos = [media_info(processor, p["path"]) for p in inputs]
    if any(info[1] != infos[0][1] for info in infos):
        raise ValueError("Part codec, resolution or audio parameters differ; parts retained")
    if any(info[2] != infos[0][2] for info in infos):
        raise ValueError("Part codec initialization differs; parts retained")
    duration = sum(info[0] for info in infos)
    temporary = destination.with_name("." + destination.stem + ".merge-" + job["id"][:12] + ".tmp" + destination.suffix)
    listing = processor.HOME / (".merge-" + job["id"] + ".ffconcat")
    if temporary.exists():
        temporary.unlink()  # Only this job's unpublished temporary output.
    lines = ["ffconcat version 1.0"]
    for part, info in zip(inputs, infos):
        path = str(Path(part["path"]).as_posix())
        if any(c in path for c in "\r\n\0"):
            raise ValueError("Part filename cannot be represented safely")
        lines += ["file '" + path.replace("'", "'\\''") + "'", "option analyzeduration 60000000", "option probesize 100000000", "duration %.9f" % info[0]]
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (processor.HOME / "current-job.json").write_text(json.dumps({"job_id": job["id"], "duration": duration,
                "started_at": time.time(), "encoder": "copy", "operation": "merge"}))
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-n", "-progress", str(processor.HOME / "progress.txt"),
               "-stats_period", "2", "-f", "concat", "-safe", "0", "-auto_convert", "0", "-i", str(listing),
               "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-avoid_negative_ts", "make_zero"]
    if destination.suffix.lower() == ".mp4":
        command += ["-movflags", "+faststart"]
        if infos[0][1][0][1] == "hevc":
            command += ["-tag:v", "hvc1"]
    command.append(str(temporary))
    try:
        processor.checkpoint("encoding")
        with (processor.HOME / "merge-last.log").open("wb") as errors:
            processor.run_process(command, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors)
        processor.checkpoint("verifying")
        output = media_info(processor, temporary)
        if output[1] != infos[0][1] or abs(output[0] - duration) > max(1.0, len(inputs) * .25, duration * .0001):
            raise ValueError("Merged codec, audio or duration verification failed; parts retained")
        counts = [processor.mapped_packet_counts(p["path"]) for p in inputs]
        expected = [sum(values) for values in zip(*counts)]
        actual = processor.mapped_packet_counts(temporary)
        if actual != expected:
            raise ValueError(f"Merged packet counts differ: expected={expected} actual={actual}; parts retained")
        for part in inputs:
            unchanged(part, processor)
        if destination.exists():
            raise FileExistsError("Merged destination appeared; parts retained")
        with temporary.open("rb+") as stream:
            os.fsync(stream.fileno())
        verification = json.dumps({"duration": output[0], "expected_duration": duration, "packet_counts": actual,
                                   "parts": len(inputs), "skipped": json.loads(record["skipped"]), "verified_at": time.time()})
        with processor.publication() as db:
            # Exclusive creation protects a user file created after preflight.
            os.link(temporary, destination)
            temporary.unlink()
            sync_directory(destination.parent)
            stat = destination.stat()
            db.execute("UPDATE merge_jobs SET verification=? WHERE id=?", (verification, job["id"]))
            db.execute("UPDATE jobs SET status='published',output=?,output_size=?,output_mtime=?,updated=? WHERE id=?",
                       (str(destination), stat.st_size, stat.st_mtime_ns, time.time(), job["id"]))
        with processor.connect() as db:
            published = dict(db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())
            record = dict(db.execute("SELECT * FROM merge_jobs WHERE id=?", (job["id"],)).fetchone())
        finish(processor, published, record)
    except subprocess.CalledProcessError as error:
        log = processor.HOME / "merge-last.log"
        detail = log.read_text(errors="replace")[-2000:] if log.exists() else ""
        raise RuntimeError(f"FFmpeg merge failed ({error.returncode}): {detail}; parts retained") from error
    finally:
        if temporary.exists():
            temporary.unlink()
        if listing.exists():
            listing.unlink()
