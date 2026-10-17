"""One-shot library HEVC batch that yields to every recorder queue operation.

Run in a separate container with only the requested library mounted read/write.
No Docker socket, network access or recorder restart is required.
"""
import argparse
import hashlib
import importlib.util
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

import queue_control
from platform_utils import acquire_lock
from shared import read_json, write_json

VIDEO_SUFFIXES = {'.mp4', '.mkv', '.flv', '.ts', '.mts', '.m2ts', '.mov', '.avi', '.webm', '.m4v', '.wmv'}
LOG = logging.getLogger('library_h265')


class PriorityGate:
    def __init__(self, database, required_sessions=()):
        self.database = Path(database)
        if not isinstance(required_sessions, (list, tuple)) or len(required_sessions)>10000 or any(
                not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{64}',value) for value in required_sessions):
            raise ValueError('Invalid prerequisite merge sessions')
        self.required_sessions = list(dict.fromkeys(required_sessions))

    def reason(self):
        try:
            with sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True, timeout=.2) as db:
                db.row_factory = sqlite3.Row
                state = queue_control.snapshot(db)
                if not state['online'] or state['phase'] == 'maintenance':
                    return 'primary_unavailable'
                if state['mode'] != 'running':
                    return 'primary_paused'
                if db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running','published') LIMIT 1").fetchone():
                    return 'primary_busy'
                if state['phase'] != 'idle':
                    return 'primary_busy'
                if self.required_sessions:
                    placeholders=','.join('?' for _ in self.required_sessions)
                    completed=db.execute("SELECT count(*) FROM merge_sessions JOIN jobs ON jobs.id=merge_sessions.job_id "
                                         "WHERE merge_sessions.id IN ("+placeholders+") AND jobs.status='done'",
                                         self.required_sessions).fetchone()[0]
                    if completed!=len(self.required_sessions):
                        return 'primary_prerequisites'
            return None
        except (OSError, sqlite3.Error, KeyError):
            # Missing/stale primary state never permits competing GPU work.
            return 'primary_unavailable'


class PriorityControl(queue_control.JobControl):
    def __init__(self, *args, gate, **kwargs):
        super().__init__(*args, **kwargs)
        self.gate = gate
        self.last_reason = None

    def checkpoint(self, process=None, phase=None):
        if phase:
            self.phase = phase
        paused_since = None
        while True:
            if self.shutdown():
                raise queue_control.WorkerShutdown()
            with self.connect() as db:
                row = db.execute('SELECT mode FROM queue_control WHERE id=1').fetchone()
            if row['mode'] == 'stopped':
                raise queue_control.JobStopped()
            reason = 'user_paused' if row['mode'] == 'paused' else self.gate.reason()
            if reason != self.last_reason:
                if reason or self.last_reason:
                    LOG.info('Priority %s job=%s', reason or 'resumed', self.job_id)
                self.last_reason = reason
            if reason is None:
                if self.suspended and process is not None:
                    self.suspend(process, False)
                    self.suspended = False
                self.report(self.phase, force=paused_since is not None)
                return time.monotonic() - paused_since if paused_since is not None else 0
            if paused_since is None:
                paused_since = time.monotonic()
                if process is not None:
                    self.suspend(process, True)
                    self.suspended = True
            self.report('paused' if reason == 'user_paused' else 'waiting_prerequisites' if reason == 'primary_prerequisites' else 'waiting_priority', force=False)
            time.sleep(.2)


def safe_path(root, file, strict=True):
    root, file = Path(root).resolve(strict=True), Path(file)
    file.relative_to(root)
    # Reject symlink ancestors too; resolving first would hide them.
    for part in [file, *file.parents]:
        if part == root:
            break
        if part.is_symlink():
            raise ValueError('Symbolic links are excluded; original retained')
    resolved = file.resolve(strict=strict)
    resolved.relative_to(root)
    return resolved


def processor(config, root, home=None):
    config, root = Path(config).resolve(), Path(root).resolve(strict=True)
    bundled = Path(__file__).with_name('library_processor.py')
    source = Path(os.environ.get('LIBRARY_PROCESSOR', str(bundled if bundled.is_file() else Path(__file__).parent.parent / 'h265/postprocess.py')))
    spec = importlib.util.spec_from_file_location('library_processor_instance', source)
    p = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p)
    p.HOME = Path(home) if home else config / 'library/h265'
    p.DB, p.ROOT = p.HOME / 'queue.sqlite3', root
    p.RUNTIME = config / 'h265/runtime/usr/lib/x86_64-linux-gnu'
    p.LOG = LOG
    p.recording_roots = lambda *_: [root]
    return p


def migrate(p):
    with p.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS library_batch(id INTEGER PRIMARY KEY CHECK(id=1),'
                   'root TEXT NOT NULL,label TEXT NOT NULL,created REAL NOT NULL,sidecars INTEGER NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS library_files(id TEXT PRIMARY KEY,destination TEXT,'
                   'verification TEXT,already_hevc INTEGER NOT NULL DEFAULT 0,retry_requested INTEGER NOT NULL DEFAULT 0)')


def seed(p, label='录播姬历史录像'):
    migrate(p)
    with p.connect() as db:
        if db.execute('SELECT 1 FROM library_batch').fetchone():
            raise ValueError('This batch was already imported; no rescan was performed')
    videos, sidecars = [], 0
    for base, dirs, files in os.walk(p.ROOT, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not Path(base, d).is_symlink())
        for name in sorted(files):
            path = Path(base, name)
            if name.startswith('.') or path.is_symlink():
                continue
            if path.suffix.lower() not in VIDEO_SUFFIXES:
                sidecars += 1
                continue
            path = safe_path(p.ROOT, path)
            stat = path.stat()
            if path.is_file():
                videos.append((path, stat))
    created = time.time()
    videos.sort(key=lambda item: (item[1].st_size, str(item[0])))
    with p.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT INTO library_batch VALUES(1,?,?,?,?)', (str(p.ROOT), label, created, sidecars))
        for path, stat in videos:
            identifier = hashlib.sha256(f'{path}\0{stat.st_size}\0{stat.st_mtime_ns}'.encode()).hexdigest()
            db.execute('INSERT INTO jobs(id,source,size,mtime,delete_source,h264,created,updated) VALUES(?,?,?,?,1,0,?,?)',
                       (identifier, str(path), stat.st_size, stat.st_mtime_ns, created, created))
            db.execute('INSERT INTO library_files(id) VALUES(?)', (identifier,))
            queue_control.append_job(db, identifier)
    return {'videos': len(videos), 'bytes': sum(s.st_size for _, s in videos), 'sidecars_retained': sidecars}


def stream_info(p, file, packets=False):
    command = ['ffprobe', '-v', 'error', '-analyzeduration', '60000000', '-probesize', '100000000']
    if packets:
        command += ['-count_packets']
    command += ['-show_streams', '-show_format', '-of', 'json', str(file)]
    try:
        result = p.run_process(command, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=7200)
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b'').decode(errors='replace')[-2000:]
        raise ValueError(f'FFprobe failed ({exc.returncode}): {detail}; original retained') from exc


def destination_for(source, info, identifier):
    streams = info.get('streams', [])
    # Matroska preserves subtitle/attachment tracks and audio such as PCM/Vorbis.
    extra = any(s.get('codec_type') not in ('video', 'audio') or
                (s.get('codec_type') == 'audio' and s.get('codec_name') not in ('aac', 'mp3', 'ac3', 'eac3', 'alac')) for s in streams)
    suffix = '.mkv' if extra or source.suffix.lower() == '.mkv' else '.mp4'
    destination = source.with_suffix(suffix)
    if destination != source and destination.exists():
        destination = source.with_name(source.stem + '.H265-' + identifier[:8] + suffix)
    if destination != source and destination.exists():
        raise FileExistsError('H.265 destination already exists; both files retained')
    return destination


def verify_streams(p, job, source, output):
    before, after = stream_info(p, source, True), stream_info(p, output, True)
    original, encoded = before['streams'], after['streams']
    if len(original) != len(encoded):
        raise ValueError('Library verification: stream count changed; original retained')
    counts = []
    for first, second in zip(original, encoded):
        kind = first.get('codec_type')
        if second.get('codec_type') != kind:
            raise ValueError('Library verification: stream order changed; original retained')
        keys = ('width', 'height') if kind == 'video' else ('codec_name', 'sample_rate', 'channels', 'channel_layout')
        if any(first.get(k) != second.get(k) for k in keys):
            raise ValueError(f'Library verification: {kind} parameters changed; original retained')
        if kind == 'video' and second.get('codec_name') != 'hevc':
            raise ValueError('Library verification: video is not HEVC; original retained')
        a, b = first.get('nb_read_packets'), second.get('nb_read_packets')
        if a != b or (kind in ('video', 'audio') and (a is None or int(a) <= 0)):
            raise ValueError(f'Library verification: {kind} packet counts differ ({a}/{b}); original retained')
        counts.append(a)
    p.checkpoint('verifying')
    decoded = p.run_process(['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', '-xerror',
                             '-err_detect', 'explode', '-threads', '1', '-i', str(output),
                             '-map', '0:v:0', '-map', '0:a?', '-f', 'null', '-'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            timeout=86400)
    if decoded.returncode or decoded.stderr.strip():
        raise ValueError('Library verification: output decode failed: ' + decoded.stderr.decode(errors='replace')[-1600:])
    proof = {'verified_at': time.time(), 'packet_counts': counts, 'full_output_decode': True, 'codec': 'hevc'}
    with p.connect() as db:
        db.execute('UPDATE library_files SET verification=? WHERE id=?', (json.dumps(proof), job['id']))


class SourceBusy(Exception):
    pass


def encode(p, job):
    source = safe_path(p.ROOT, job['source'])
    if time.time() - source.stat().st_mtime < 300 or p.opened_by_ffmpeg(source):
        raise SourceBusy()
    if not p.unchanged(source, job):
        raise ValueError('Source changed since batch import; original retained. Retry after recording finishes.')
    info = stream_info(p, source)
    videos = [s for s in info.get('streams', []) if s.get('codec_type') == 'video']
    if len(videos) != 1 or videos[0].get('disposition', {}).get('attached_pic'):
        raise ValueError('Expected one video stream; multiple videos/cover tracks require review; original retained')
    if videos[0].get('codec_name') == 'hevc':
        p.probe(source)
        # The only existing HEVC copy is already the requested final format.
        with p.publication() as db:
            if not p.unchanged(source, job):
                raise ValueError('Source changed while checking HEVC; original retained')
            db.execute("UPDATE jobs SET status='done',output=source,output_size=size,output_mtime=mtime,error=NULL,updated=? WHERE id=?",
                       (time.time(), job['id']))
            db.execute('UPDATE library_files SET already_hevc=1 WHERE id=?', (job['id'],))
        LOG.info('Already HEVC; retained final file %s', source.relative_to(p.ROOT))
        return
    if shutil.disk_usage(source.parent).free < job['size'] * 2 + 512 * 1024 * 1024:
        raise ValueError('Not enough free space for safe conversion; original retained')
    destination = destination_for(source, info, job['id'])
    with p.connect() as db:
        db.execute('UPDATE library_files SET destination=? WHERE id=?', (str(destination), job['id']))
    p.encode(job, destination_override=destination, preserve_streams=True,
             verify_output=lambda a, b: verify_streams(p, job, a, b))


def retry_requested(p):
    with p.connect() as db:
        rows = db.execute("SELECT jobs.* FROM jobs JOIN library_files USING(id) WHERE retry_requested=1 AND status IN ('failed','stopped')").fetchall()
        for job in rows:
            try:
                source = safe_path(p.ROOT, job['source'])
                stat = source.stat()
                db.execute("UPDATE jobs SET size=?,mtime=?,status='queued',error=NULL,updated=? WHERE id=?",
                           (stat.st_size, stat.st_mtime_ns, time.time(), job['id']))
                queue_control.append_job(db, job['id'])
                db.execute('DELETE FROM deleted_jobs WHERE id=?', (job['id'],)) if db.execute("SELECT 1 FROM sqlite_master WHERE name='deleted_jobs'").fetchone() else None
                LOG.info('Retry requested %s', source.relative_to(p.ROOT))
            except (OSError, ValueError) as exc:
                db.execute('UPDATE jobs SET error=?,updated=? WHERE id=?', (str(exc), time.time(), job['id']))
                LOG.error('Retry rejected %s: %s', Path(job['source']).name, exc)
            db.execute('UPDATE library_files SET retry_requested=0 WHERE id=?', (job['id'],))


def worker(p, primary):
    migrate(p)
    p.HOME.mkdir(parents=True, exist_ok=True)
    with (p.HOME / 'worker.lock').open('a') as lock:
        try:
            acquire_lock(lock, blocking=False)
        except BlockingIOError:
            return
        handler = RotatingFileHandler(p.HOME / 'postprocess.log', maxBytes=5*1024*1024, backupCount=5)
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        LOG.addHandler(handler); LOG.setLevel(logging.INFO)
        token, stopped = uuid.uuid4().hex, [False]
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stopped.__setitem__(0, True))
        (p.HOME / 'worker.pid').write_text(str(os.getpid()))
        prerequisites = read_json(p.HOME / 'prerequisites.json', {}).get('sessions', [])
        gate = PriorityGate(primary, prerequisites)
        with p.connect() as db:
            state = db.execute('SELECT mode,stop_job_id FROM queue_control WHERE id=1').fetchone()
            if state['mode'] == 'stopped' and state['stop_job_id']:
                db.execute("UPDATE jobs SET status='stopped' WHERE status='running' AND id=?", (state['stop_job_id'],))
            db.execute("UPDATE jobs SET status='queued' WHERE status='running'")
        LOG.info('Library worker ready; recorder transcodes and merges have highest priority')
        try:
            while not stopped[0]:
                retry_requested(p)
                with p.connect() as db:
                    mode = db.execute('SELECT mode FROM queue_control WHERE id=1').fetchone()[0]
                    reason = gate.reason() if mode == 'running' else None
                    job = None
                    if mode == 'running' and reason is None:
                        ids = queue_control.queued_ids(db)
                        job = db.execute("SELECT * FROM jobs WHERE status='published' ORDER BY created LIMIT 1").fetchone()
                        if not job and ids:
                            job = db.execute('SELECT * FROM jobs WHERE id=?', (ids[0],)).fetchone()
                            db.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), job['id']))
                    phase = mode if mode != 'running' else 'waiting_prerequisites' if reason == 'primary_prerequisites' else 'waiting_priority' if reason else 'preparing' if job else 'idle'
                    queue_control.runtime(db, token, phase, job['id'] if job else None)
                if not job:
                    time.sleep(.5); continue
                p.JOB_CONTROL = PriorityControl(p.DB, token, job['id'], lambda: stopped[0], gate=gate)
                try:
                    if job['status'] == 'published':
                        with p.connect() as db:
                            proof = db.execute('SELECT verification FROM library_files WHERE id=?', (job['id'],)).fetchone()[0]
                        if not proof:
                            raise ValueError('Published batch output has no verification proof; sources retained')
                        p.finish_published(job)
                    else:
                        encode(p, job)
                except SourceBusy:
                    with p.connect() as db:
                        db.execute("UPDATE jobs SET status='queued',updated=? WHERE id=?", (time.time(), job['id']))
                        queue_control.append_job(db, job['id'])
                    time.sleep(1)
                except queue_control.JobStopped:
                    with p.connect() as db:
                        db.execute("UPDATE jobs SET status='stopped',error=NULL,updated=? WHERE id=? AND status='running'", (time.time(), job['id']))
                    LOG.info('Stopped %s; source retained', job['source'])
                except queue_control.WorkerShutdown:
                    with p.connect() as db:
                        db.execute("UPDATE jobs SET status='queued',error=NULL,updated=? WHERE id=? AND status='running'", (time.time(), job['id']))
                    break
                except Exception as exc:
                    with p.connect() as db:
                        db.execute("UPDATE jobs SET status='failed',error=?,updated=? WHERE id=?", (str(exc), time.time(), job['id']))
                    LOG.error('Failed file=%s job=%s reason=%s; available originals retained', job['source'], job['id'], exc)
                finally:
                    p.JOB_CONTROL = None
        finally:
            with p.connect() as db:
                queue_control.runtime(db, token, 'offline')
            LOG.removeHandler(handler); handler.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=Path(os.environ.get('RECORDER_CONFIG_DIR', '/app/config')))
    parser.add_argument('--root', type=Path, default=Path(os.environ.get('LIBRARY_ROOT', '/app/library')))
    parser.add_argument('--seed', action='store_true')
    parser.add_argument('--worker', action='store_true')
    args = parser.parse_args()
    p = processor(args.config, args.root)
    if args.seed:
        cfg = json.loads((args.config / 'h265/settings.json').read_text())
        cfg['enabled'] = True
        write_json(p.HOME / 'settings.json', cfg)
        print(json.dumps(seed(p), ensure_ascii=False))
    if args.worker:
        worker(p, args.config / 'h265/queue.sqlite3')


if __name__ == '__main__':
    main()
