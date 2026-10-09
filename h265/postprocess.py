#!/usr/bin/env python3
"""Persistent, single-worker post-recording queue for DouyinLiveRecorder."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

HOME = Path(os.environ.get('RECORDER_CONFIG_DIR', '/app/config')).resolve() / 'h265'
RUNTIME = HOME / 'runtime/usr/lib/x86_64-linux-gnu'
ROOT = Path(os.environ.get('RECORDER_DOWNLOADS_DIR', '/app/downloads')).resolve()
DB = HOME / 'queue.sqlite3'
DEFAULTS = {'enabled': True, 'crf': 24, 'preset': 'veryfast', 'cpu_threads': 2,
            'copy_already_hevc': True, 'encoder': 'libx265',
            'qsv_device': '/dev/dri/renderD128', 'qsv_quality': 24,
            'qsv_preset': 'fast'}
LOG = logging.getLogger('h265')


def settings():
    result = DEFAULTS.copy()
    file = HOME / 'settings.json'
    if file.exists():
        result.update(json.loads(file.read_text(encoding='utf-8-sig')))
    if not 0 <= int(result['crf']) <= 51:
        raise ValueError('CRF must be between 0 and 51')
    if result['preset'] not in ('ultrafast', 'superfast', 'veryfast', 'faster',
                                'fast', 'medium', 'slow', 'slower', 'veryslow'):
        raise ValueError('Unsupported encoding preset')
    result['cpu_threads'] = max(1, min(4, int(result['cpu_threads'])))
    if result['encoder'] not in ('libx265', 'hevc_qsv'):
        raise ValueError('Unsupported HEVC encoder')
    if not 1 <= int(result['qsv_quality']) <= 51:
        raise ValueError('QSV quality must be between 1 and 51')
    if result['qsv_preset'] not in ('veryfast', 'faster', 'fast', 'medium',
                                    'slow', 'slower', 'veryslow'):
        raise ValueError('Unsupported QSV preset')
    if not str(result['qsv_device']).startswith('/dev/dri/renderD') or ',' in str(result['qsv_device']):
        raise ValueError('QSV requires a DRM render device')
    return result


@contextmanager
def connect():
    HOME.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS jobs ('
               'id TEXT PRIMARY KEY, source TEXT NOT NULL, size INTEGER NOT NULL, '
               'mtime INTEGER NOT NULL, delete_source INTEGER NOT NULL, h264 INTEGER NOT NULL, '
               "status TEXT NOT NULL DEFAULT 'queued', error TEXT, output TEXT, "
               'output_size INTEGER, output_mtime INTEGER, '
               'created REAL NOT NULL, updated REAL NOT NULL)')
    db.commit()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def enqueue(file, delete_source=True, h264=False):
    file = Path(file).resolve(strict=True)
    file.relative_to(ROOT)
    if not file.is_file() or file.suffix.lower() not in ('.ts', '.flv', '.mkv', '.mp4'):
        raise ValueError('Only recorded video files are accepted')
    stat = file.stat()
    if not stat.st_size:
        raise ValueError('Empty recording')
    identifier = hashlib.sha256(
        f'{file}\0{stat.st_size}\0{stat.st_mtime_ns}'.encode()).hexdigest()
    with connect() as db:
        db.execute('INSERT OR IGNORE INTO jobs '
                   '(id,source,size,mtime,delete_source,h264,created,updated) '
                   'VALUES (?,?,?,?,?,?,?,?)',
                   (identifier, str(file), stat.st_size, stat.st_mtime_ns,
                    int(delete_source), int(h264), time.time(), time.time()))
    return identifier


def ensure_worker():
    # The worker's flock is authoritative; no PID-file assumptions across reboots.
    HOME.mkdir(parents=True, exist_ok=True)
    with open(HOME / 'worker.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with open(HOME / 'worker-startup.log', 'ab') as log:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker',
                              '--lock-fd', str(lock.fileno())],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True, close_fds=True,
                             pass_fds=(lock.fileno(),))


def probe(file):
    raw = subprocess.check_output(
        ['ffprobe', '-v', 'error', '-show_entries',
         'format=duration:stream=codec_type,codec_name,width,height,pix_fmt', '-of', 'json', str(file)],
        stderr=subprocess.STDOUT, timeout=120)
    info = json.loads(raw)
    videos = [s for s in info.get('streams', []) if s.get('codec_type') == 'video']
    if not videos:
        raise ValueError('No video stream')
    duration = float(info['format']['duration'])
    if duration <= 0:
        raise ValueError('Invalid duration')
    return duration, videos[0], sum(s.get('codec_type') == 'audio' for s in info['streams'])


def opened_by_ffmpeg(file):
    wanted = str(file)
    for name in Path('/proc').glob('[0-9]*/comm'):
        try:
            if name.read_text().strip() != 'ffmpeg':
                continue
            for fd in name.parent.joinpath('fd').iterdir():
                try:
                    if os.readlink(fd) == wanted:
                        return True
                except (OSError, PermissionError):
                    pass
        except (OSError, PermissionError):
            pass
    return False


def unchanged(file, job):
    stat = file.stat()
    return stat.st_size == job['size'] and stat.st_mtime_ns == job['mtime']


def encode(job):
    source = Path(job['source']).resolve(strict=True)
    source.relative_to(ROOT)
    if not unchanged(source, job):
        raise ValueError('Source changed; original retained')
    if opened_by_ffmpeg(source):
        raise ValueError('Source is still open; original retained')
    cfg = settings()
    destination = source.with_suffix('.mp4')
    if source == destination and not job['delete_source']:
        suffix = '.H265.mp4' if cfg['enabled'] else '.converted.mp4'
        destination = source.with_name(source.stem + suffix)
    if destination != source and destination.exists():
        raise FileExistsError('MP4 already exists; both files retained')
    temporary = source.parent / ('.' + source.stem + '.h265-' + job['id'][:12] + '.tmp.mp4')
    if temporary.exists():
        temporary.unlink()  # Only this job's incomplete, unpublished temporary output.
    original_duration, video, audio_count = probe(source)
    if cfg['enabled']:
        mode = 'copy' if video['codec_name'] == 'hevc' and cfg['copy_already_hevc'] else cfg['encoder']
        expected_codec = 'hevc'
    else:
        mode = 'libx264' if job['h264'] else 'copy'
        expected_codec = 'h264' if job['h264'] else video['codec_name']
    cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-n',
           '-threads', '1', '-filter_threads', '1', '-fflags', '+genpts']
    if mode == 'hevc_qsv':
        device = Path(cfg['qsv_device'])
        if not device.is_char_device() or not os.access(device, os.R_OK | os.W_OK):
            raise ValueError('QSV device unavailable; original retained')
        formats = {'yuv420p': 'nv12', 'yuvj420p': 'nv12', 'nv12': 'nv12',
                   'yuv420p10le': 'p010le', 'p010le': 'p010le'}
        if video.get('pix_fmt') not in formats:
            raise ValueError('QSV pixel format unsupported; original retained')
        cmd += ['-init_hw_device', f'vaapi=va:{device}',
                '-init_hw_device', 'qsv=qs@va', '-filter_hw_device', 'qs']
    cmd += ['-i', str(source), '-map', '0:v:0', '-map', '0:a?',
           '-map_metadata', '0', '-c:v', mode, '-c:a', 'copy', '-fps_mode', 'passthrough']
    if mode == 'libx265':
        cmd += ['-preset', cfg['preset'], '-crf', str(cfg['crf']), '-x265-params',
                f"pools={cfg['cpu_threads']}:frame-threads=1:log-level=error"]
    elif mode == 'libx264':
        cmd += ['-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p']
    elif mode == 'hevc_qsv':
        cmd += ['-vf', f"format={formats[video['pix_fmt']]},hwupload=extra_hw_frames=64",
                '-preset', cfg['qsv_preset'], '-global_quality', str(cfg['qsv_quality']),
                '-look_ahead', '0', '-async_depth', '4']
    if expected_codec == 'hevc':
        cmd += ['-tag:v', 'hvc1']
    cmd += ['-avoid_negative_ts', 'make_zero', '-movflags', '+faststart', str(temporary)]
    write_progress = {'job_id': job['id'], 'duration': original_duration,
                      'started_at': time.time(), 'encoder': mode}
    progress_state = HOME / 'current-job.json'
    progress_state.with_suffix('.new').write_text(json.dumps(write_progress))
    progress_state.with_suffix('.new').replace(progress_state)
    cmd[1:1] = ['-progress', str(HOME / 'progress.txt'), '-stats_period', '2']

    def limit_resources():
        os.nice(10)
        if hasattr(os, 'sched_getaffinity'):
            allowed = sorted(os.sched_getaffinity(0))[:cfg['cpu_threads']]
            if allowed:
                os.sched_setaffinity(0, allowed)

    LOG.info('Encoding %s mode=%s', source.name, mode)
    try:
        environment = os.environ.copy()
        if mode == 'hevc_qsv' and RUNTIME.is_dir():
            environment['ONEVPL_SEARCH_PATH'] = str(RUNTIME)
            if (RUNTIME / 'dri/iHD_drv_video.so').is_file():
                environment['LIBVA_DRIVERS_PATH'] = str(RUNTIME / 'dri')
                environment['LIBVA_DRIVER_NAME'] = 'iHD'
        with open(HOME / 'ffmpeg-last.log', 'wb') as errors:
            subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=errors,
                           preexec_fn=limit_resources, env=environment)
        duration, output_video, output_audio_count = probe(temporary)
        if output_video['codec_name'] != expected_codec:
            raise ValueError('Output codec verification failed')
        if (output_video.get('width'), output_video.get('height')) != (video.get('width'), video.get('height')):
            raise ValueError('Output dimensions changed')
        if output_audio_count != audio_count:
            raise ValueError('Audio stream count changed')
        if abs(duration - original_duration) > max(1.0, original_duration * .001):
            raise ValueError('Output duration verification failed')
        if not unchanged(source, job) or opened_by_ffmpeg(source):
            raise ValueError('Source became active or changed; original retained')
        if destination != source and destination.exists():
            raise FileExistsError('Destination appeared; original retained')
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        published_stat = destination.stat()
        with connect() as db:
            db.execute("UPDATE jobs SET status='published',output=?,output_size=?,output_mtime=?,updated=? WHERE id=?",
                       (str(destination), published_stat.st_size, published_stat.st_mtime_ns,
                        time.time(), job['id']))
        if job['delete_source'] and source != destination:
            if unchanged(source, job) and not opened_by_ffmpeg(source):
                source.unlink()
            else:
                raise ValueError('Original changed after publication; retained')
        with connect() as db:
            db.execute("UPDATE jobs SET status='done',error=NULL,updated=? WHERE id=?",
                       (time.time(), job['id']))
        LOG.info('Done %s codec=%s duration=%.2f', destination.name, expected_codec, duration)
    except Exception as exc:
        if temporary.exists():
            temporary.unlink()
        if isinstance(exc, subprocess.CalledProcessError):
            detail = (HOME / 'ffmpeg-last.log').read_text(errors='replace')[-2000:]
            raise RuntimeError(f'FFmpeg failed ({exc.returncode}): {detail}') from exc
        raise


def finish_published(job):
    destination = Path(job['output']).resolve(strict=True)
    destination.relative_to(ROOT)
    stat = destination.stat()
    if (stat.st_size, stat.st_mtime_ns) != (job['output_size'], job['output_mtime']):
        raise ValueError('Published output changed; originals retained')
    probe(destination)
    source = Path(job['source'])
    if job['delete_source'] and source != destination and source.exists():
        if not unchanged(source, job) or opened_by_ffmpeg(source):
            raise ValueError('Original changed after publication; retained')
        source.unlink()
    with connect() as db:
        db.execute("UPDATE jobs SET status='done',error=NULL,updated=? WHERE id=?",
                   (time.time(), job['id']))
    LOG.info('Recovered published output %s', destination.name)


def worker(inherited_fd=None):
    HOME.mkdir(parents=True, exist_ok=True)
    lock = os.fdopen(inherited_fd, 'a') if inherited_fd is not None else open(HOME / 'worker.lock', 'a')
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        handler = RotatingFileHandler(HOME / 'postprocess.log', maxBytes=1024 * 1024, backupCount=2)
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        LOG.addHandler(handler)
        LOG.setLevel(logging.INFO)
        (HOME / 'worker.pid').write_text(str(os.getpid()))
        with connect() as db:
            db.execute("UPDATE jobs SET status='queued',updated=? WHERE status='running'", (time.time(),))
        LOG.info('Worker ready; one job at a time')
        while True:
            if (HOME / 'paused').exists():
                time.sleep(3)
                continue
            with connect() as db:
                job = db.execute("SELECT * FROM jobs WHERE status IN ('published','queued') ORDER BY CASE status WHEN 'published' THEN 0 ELSE 1 END,created LIMIT 1").fetchone()
                if job and job['status'] != 'published':
                    db.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), job['id']))
            if not job:
                time.sleep(3)
                continue
            try:
                if job['status'] == 'published':
                    finish_published(job)
                else:
                    encode(job)
            except Exception as exc:
                with connect() as db:
                    db.execute("UPDATE jobs SET status='failed',error=?,updated=? WHERE id=?",
                               (str(exc), time.time(), job['id']))
                LOG.error('Failed %s: %s; available video retained', Path(job['source']).name, exc)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source')
    parser.add_argument('--delete-original', choices=('yes', 'no'), default='yes')
    parser.add_argument('--h264', choices=('yes', 'no'), default='no')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--lock-fd', type=int)
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--retry-failed', action='store_true')
    args = parser.parse_args()
    if args.worker:
        worker(args.lock_fd)
        return
    if args.source:
        print('H265 queued:', enqueue(args.source, args.delete_original == 'yes', args.h264 == 'yes')[:12])
    if args.retry_failed:
        with connect() as db:
            db.execute("UPDATE jobs SET status='queued',error=NULL,updated=? WHERE status='failed'", (time.time(),))
    if args.status:
        with connect() as db:
            print(json.dumps({r['status']: r['n'] for r in db.execute('SELECT status,count(*) n FROM jobs GROUP BY status')}, ensure_ascii=False))
        return
    ensure_worker()


if __name__ == '__main__':
    main()
