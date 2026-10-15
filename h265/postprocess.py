#!/usr/bin/env python3
"""Persistent, single-worker post-recording queue for DouyinLiveRecorder."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import sqlite3
import signal
import uuid
import subprocess
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/"gui"))
import queue_control
from platform_utils import acquire_lock, child_command, process_options, recording_relative, recording_roots, sync_directory

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
    result['cpu_threads'] = max(1, min(16, int(result['cpu_threads'])))
    result['bitrate_kbps'] = max(500,min(100000,int(result.get('bitrate_kbps',4000))))
    if result['encoder'] not in ('libx265', 'hevc_qsv', 'hevc_videotoolbox', 'hevc_nvenc', 'hevc_amf'):
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
    queue_control.migrate(db)
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
    recording_relative(file,recording_roots(HOME.parent,ROOT))
    if not file.is_file() or file.suffix.lower() not in ('.ts', '.flv', '.mkv', '.mp4'):
        raise ValueError('Only recorded video files are accepted')
    stat = file.stat()
    if not stat.st_size:
        raise ValueError('Empty recording')
    identifier = hashlib.sha256(
        f'{file}\0{stat.st_size}\0{stat.st_mtime_ns}'.encode()).hexdigest()
    with connect() as db:
        inserted = db.execute('INSERT OR IGNORE INTO jobs '
                   '(id,source,size,mtime,delete_source,h264,created,updated) '
                   'VALUES (?,?,?,?,?,?,?,?)',
                   (identifier, str(file), stat.st_size, stat.st_mtime_ns,
                    int(delete_source), int(h264), time.time(), time.time()))
        if inserted.rowcount:
            queue_control.append_job(db, identifier)
    return identifier


def ensure_worker():
    if os.environ.get("DLR_DESKTOP_WORKER_MANAGED") == "1":
        return
    # The worker lock is authoritative; no PID-file assumptions across reboots.
    HOME.mkdir(parents=True, exist_ok=True)
    with open(HOME / 'worker.lock', 'a') as lock:
        try:
            acquire_lock(lock,blocking=False)
        except BlockingIOError:
            return
        with open(HOME / 'worker-startup.log', 'ab') as log:
            if os.name == "nt":
                subprocess.Popen(child_command("postprocess","--worker"),stdin=subprocess.DEVNULL,
                                 stdout=log,stderr=log,close_fds=True,**process_options())
            else:
                subprocess.Popen([sys.executable,str(Path(__file__).resolve()),"--worker",
                                  "--lock-fd",str(lock.fileno())],stdin=subprocess.DEVNULL,
                                 stdout=log,stderr=log,start_new_session=True,close_fds=True,
                                 pass_fds=(lock.fileno(),))


JOB_CONTROL = None
SHUTDOWN = False


def run_process(command, **kwargs):
    if JOB_CONTROL is not None:
        return JOB_CONTROL.run(command, **kwargs)
    return subprocess.run(command, **kwargs)


def process_output(command, **kwargs):
    if JOB_CONTROL is not None:
        kwargs.setdefault('stdin', subprocess.DEVNULL)
        return JOB_CONTROL.run(command, check=True, stdout=subprocess.PIPE, **kwargs).stdout
    return subprocess.check_output(command, **kwargs)


def checkpoint(phase):
    if JOB_CONTROL is not None:
        JOB_CONTROL.checkpoint(phase=phase)


@contextmanager
def publication():
    if JOB_CONTROL is not None:
        with JOB_CONTROL.publication(connect) as db:
            yield db
    else:
        with connect() as db:
            yield db


def probe(file):
    raw = process_output(
        ['ffprobe', '-v', 'error', '-show_entries',
         'format=duration:stream=codec_type,codec_name,width,height,pix_fmt,color_range', '-of', 'json', str(file)],
        stderr=subprocess.STDOUT, timeout=120)
    info = json.loads(raw)
    videos = [s for s in info.get('streams', []) if s.get('codec_type') == 'video']
    if not videos:
        raise ValueError('No video stream')
    duration = float(info['format']['duration'])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Invalid duration')
    return duration, videos[0], sum(s.get('codec_type') == 'audio' for s in info['streams'])


def mapped_packet_counts(file):
    """Read the entire file, counting the first video and every mapped audio stream."""
    raw = process_output(
        ['ffprobe', '-v', 'error', '-count_packets', '-show_entries',
         'stream=codec_type,nb_read_packets', '-of', 'json', str(file)],
        stderr=subprocess.STDOUT, timeout=1800, **process_options())
    streams = json.loads(raw).get('streams', [])
    videos = [s for s in streams if s.get('codec_type') == 'video']
    if not videos:
        raise ValueError('Full-file verification found no video stream')
    counts = [int(s['nb_read_packets']) for s in
              [videos[0], *[s for s in streams if s.get('codec_type') == 'audio']]]
    if any(n <= 0 for n in counts):
        raise ValueError('Full-file verification found an empty stream')
    return counts


def normalized_reference(file):
    """Read the full input; null-muxer progress is a preliminary timeline estimate."""
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error',
               '-threads', '1', '-fflags', '+genpts', '-i', str(file),
               '-map', '0:v:0', '-map', '0:a?', '-c', 'copy',
               '-fps_mode', 'passthrough', '-progress', 'pipe:1', '-f', 'null', '-']
    with tempfile.TemporaryFile() as errors:
        result = run_process(command, check=True, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=errors,
                                timeout=1800, **process_options())
    progress = dict(line.split('=', 1) for line in result.stdout.decode().splitlines() if '=' in line)
    duration = int(progress.get('out_time_us', progress.get('out_time_ms', '0'))) / 1_000_000
    # FFmpeg 6.1 omits frame progress for stream-copy outputs (no video filter).
    # A missing counter requires the full MP4 reference check below.
    frames = int(progress['frame']) if 'frame' in progress else None
    if progress.get('progress') != 'end' or duration <= 0 or (frames is not None and frames <= 0):
        raise ValueError('Full-file timestamp verification did not finish')
    return duration, frames


def mapped_stream_timings(file):
    """Read MP4 track lengths, including audio that outlasts the final video packet."""
    raw = process_output(
        ['ffprobe', '-v', 'error', '-show_entries',
         'format=duration:stream=codec_type,start_time,duration', '-of', 'json', str(file)],
        stderr=subprocess.STDOUT, timeout=120, **process_options())
    info = json.loads(raw)
    streams = info.get('streams', [])
    videos = [s for s in streams if s.get('codec_type') == 'video']
    if not videos:
        raise ValueError('MP4 reference has no video stream')
    duration = float(info['format']['duration'])
    tracks = [(s['codec_type'], float(s['start_time']), float(s['duration']))
              for s in [videos[0], *[s for s in streams if s.get('codec_type') == 'audio']]]
    if not math.isfinite(duration) or duration <= 0 or any(
            not math.isfinite(start) or not math.isfinite(length) or length <= 0
            for _, start, length in tracks):
        raise ValueError('MP4 reference has invalid track timing')
    return duration, tracks


def verify_mp4_timeline(source, output, source_counts, output_duration):
    # A null muxer reports the last packet's clock, whereas MP4 uses every
    # track's duration. Compare like containers without re-encoding the source.
    with tempfile.TemporaryDirectory(prefix='dlr-duration-reference-') as directory:
        reference = Path(directory) / 'reference.mp4'
        command = ['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', '-n',
                   '-threads', '1', '-fflags', '+genpts', '-i', str(source),
                   '-map', '0:v:0', '-map', '0:a?', '-c', 'copy',
                   '-fps_mode', 'passthrough', '-avoid_negative_ts', 'make_zero', str(reference)]
        with tempfile.TemporaryFile() as errors:
            run_process(command, check=True, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=errors,
                           timeout=1800, **process_options())
        if mapped_packet_counts(reference) != source_counts:
            raise ValueError('MP4 reference did not preserve every mapped packet')
        reference_duration, reference_tracks = mapped_stream_timings(reference)
        _, output_tracks = mapped_stream_timings(output)
        if abs(output_duration - reference_duration) > max(1.0, reference_duration * .001):
            raise ValueError(f'MP4 reference={reference_duration:.3f}s differs from output')
        if len(reference_tracks) != len(output_tracks):
            raise ValueError('MP4 reference track count differs from output')
        for index, (expected, actual) in enumerate(zip(reference_tracks, output_tracks)):
            kind, start, duration = expected
            tolerance = max(1.0, duration * .001)
            if actual[0] != kind or abs(actual[1] - start) > 1.0 or abs(actual[2] - duration) > tolerance:
                raise ValueError(f'MP4 reference {kind} track {index} timing differs from output')
        return reference_duration


def verify_duration(source, output, original_duration, output_duration):
    tolerance = max(1.0, original_duration * .001)
    if abs(output_duration - original_duration) <= tolerance:
        return
    detail = f'source={original_duration:.3f}s output={output_duration:.3f}s'
    # MPEG-TS duration is often estimated from the file's ends. A full pass
    # also applies FFmpeg's discontinuity correction, just as encoding does.
    try:
        reference_duration, reference_frames = normalized_reference(source)
        source_counts = mapped_packet_counts(source)
        output_counts = mapped_packet_counts(output)
        if source_counts != output_counts or (reference_frames is not None and reference_frames != source_counts[0]):
            raise ValueError(f'mapped packet counts differ: source={source_counts} output={output_counts}')
        if reference_frames is None or abs(output_duration - reference_duration) > max(1.0, reference_duration * .001):
            reference_duration = verify_mp4_timeline(source, output, source_counts, output_duration)
    except (ValueError, KeyError, subprocess.SubprocessError, OSError) as exc:
        raise ValueError(f'Output duration verification failed: {detail}; {exc}; original retained') from exc
    LOG.warning('Duration estimate corrected for %s: %s normalized=%.3fs; all mapped packet counts match %s',
                source.name, detail, reference_duration, source_counts)


def opened_by_ffmpeg(file):
    wanted = os.path.normcase(str(Path(file).resolve()))
    if os.name != "nt" and Path("/proc").is_dir():
        for name in Path("/proc").glob("[0-9]*/comm"):
            try:
                if name.read_text().strip() != "ffmpeg":
                    continue
                for fd in name.parent.joinpath("fd").iterdir():
                    try:
                        if os.path.normcase(os.readlink(fd)) == wanted:
                            return True
                    except (OSError,PermissionError):
                        pass
            except (OSError,PermissionError):
                pass
    else:
        import psutil
        for process in psutil.process_iter(["name"]):
            try:
                if (process.info["name"] or "").lower() not in ("ffmpeg","ffmpeg.exe"):
                    continue
                if any(os.path.normcase(item.path)==wanted for item in process.open_files()):
                    return True
            except (psutil.NoSuchProcess,psutil.AccessDenied):
                pass
    return False


def unchanged(file, job):
    stat = file.stat()
    return stat.st_size == job['size'] and stat.st_mtime_ns == job['mtime']


def encode(job):
    checkpoint('preparing')
    source = Path(job['source']).resolve(strict=True)
    recording_relative(source,recording_roots(HOME.parent,ROOT))
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
        if os.name != 'nt' and (not device.is_char_device() or not os.access(device, os.R_OK | os.W_OK)):
            raise ValueError('QSV device unavailable; original retained')
        formats = {'yuv420p': 'nv12', 'yuvj420p': 'nv12', 'nv12': 'nv12',
                   'yuv420p10le': 'p010le', 'p010le': 'p010le'}
        if video.get('pix_fmt') not in formats:
            raise ValueError('QSV pixel format unsupported; original retained')
        if os.name == 'nt':
            cmd += ['-init_hw_device','qsv=qs:hw,child_device_type=d3d11va','-filter_hw_device','qs']
        else:
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
        # Normalize software frames before QSV; let the encoder own GPU uploads.
        # Reinitializing hwupload creates a new pool the existing encoder cannot use.
        color_range = 'pc' if video.get('color_range') == 'pc' or video['pix_fmt'] == 'yuvj420p' else 'tv'
        cmd += ['-noautoscale', '-vf',
                f"scale=w={video['width']}:h={video['height']}:flags=bicubic:out_range={color_range},"
                f"format={formats[video['pix_fmt']]}",
                '-preset', cfg['qsv_preset'], '-global_quality', str(cfg['qsv_quality']),
                '-look_ahead', '0', '-async_depth', '4']
    elif mode == 'hevc_videotoolbox':
        cmd += ['-b:v',str(cfg['bitrate_kbps'])+'k','-allow_sw','0','-pix_fmt','yuv420p']
    elif mode == 'hevc_nvenc':
        cmd += ['-preset','p4','-rc','vbr','-cq',str(cfg['qsv_quality']),'-b:v','0','-pix_fmt','yuv420p']
    elif mode == 'hevc_amf':
        cmd += ['-quality','balanced','-rc','cqp','-qp_i',str(cfg['qsv_quality']),
                '-qp_p',str(cfg['qsv_quality']),'-pix_fmt','yuv420p']
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
        if hasattr(os,'nice'):
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
        checkpoint('encoding')
        with open(HOME / 'ffmpeg-last.log', 'wb') as errors:
            run_process(cmd, check=True, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=errors,
                           env=environment,**({"preexec_fn":limit_resources} if os.name!="nt" else process_options()))
        checkpoint('verifying')
        duration, output_video, output_audio_count = probe(temporary)
        if output_video['codec_name'] != expected_codec:
            raise ValueError('Output codec verification failed')
        if (output_video.get('width'), output_video.get('height')) != (video.get('width'), video.get('height')):
            raise ValueError('Output dimensions changed')
        if output_audio_count != audio_count:
            raise ValueError('Audio stream count changed')
        verify_duration(source, temporary, original_duration, duration)
        if not unchanged(source, job) or opened_by_ffmpeg(source):
            raise ValueError('Source became active or changed; original retained')
        if destination != source and destination.exists():
            raise FileExistsError('Destination appeared; original retained')
        # Windows FlushFileBuffers requires a writable file handle.
        with temporary.open('rb+') as stream:
            os.fsync(stream.fileno())
        with publication() as db:
            os.replace(temporary, destination)
            sync_directory(destination.parent)
            published_stat = destination.stat()
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
    recording_relative(destination,recording_roots(HOME.parent,ROOT))
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
    global JOB_CONTROL, SHUTDOWN
    HOME.mkdir(parents=True, exist_ok=True)
    lock = os.fdopen(inherited_fd, 'a') if inherited_fd is not None else open(HOME / 'worker.lock', 'a')
    with lock:
        try:
            acquire_lock(lock,blocking=False)
        except BlockingIOError:
            return
        handler = RotatingFileHandler(HOME / 'postprocess.log', maxBytes=1024 * 1024, backupCount=2)
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        LOG.addHandler(handler)
        LOG.setLevel(logging.INFO)
        (HOME / 'worker.pid').write_text(str(os.getpid()))
        token = uuid.uuid4().hex
        SHUTDOWN = False
        def shutdown(signum, frame):
            global SHUTDOWN
            SHUTDOWN = True
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, shutdown)
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute("SELECT mode,stop_job_id FROM queue_control WHERE id=1").fetchone()
            if state['mode'] == 'stopped' and state['stop_job_id']:
                db.execute("UPDATE jobs SET status='stopped',error=NULL,updated=? WHERE status='running' AND id=?",
                           (time.time(), state['stop_job_id']))
            db.execute("UPDATE jobs SET status='queued',updated=? WHERE status='running'", (time.time(),))
            queue_control.runtime(db, token, 'starting')
        LOG.info('Worker ready; cooperative controls and persistent queue order enabled')
        try:
            while not SHUTDOWN:
                with connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    mode = db.execute("SELECT mode FROM queue_control WHERE id=1").fetchone()[0]
                    if (HOME / 'paused').exists():
                        job = None
                        queue_control.runtime(db, token, 'maintenance')
                    elif mode != 'running':
                        job = None
                        queue_control.runtime(db, token, mode)
                    else:
                        job = db.execute("SELECT jobs.* FROM jobs LEFT JOIN queue_order ON job_id=jobs.id "
                                         "WHERE status IN ('published','queued') ORDER BY "
                                         "CASE status WHEN 'published' THEN 0 ELSE 1 END,"
                                         "position IS NULL,position,created,jobs.id LIMIT 1").fetchone()
                        if job and job['status'] != 'published':
                            db.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), job['id']))
                            db.execute("UPDATE queue_control SET order_revision=order_revision+1 WHERE id=1")
                        queue_control.runtime(db, token, ('finalizing' if job['status'] == 'published' else 'preparing') if job else 'idle',
                                              job['id'] if job else None)
                if not job:
                    time.sleep(.5)
                    continue
                try:
                    if job['status'] == 'published':
                        finish_published(job)
                    else:
                        JOB_CONTROL = queue_control.JobControl(DB, token, job['id'], lambda: SHUTDOWN)
                        encode(job)
                except queue_control.JobStopped:
                    with connect() as db:
                        db.execute("UPDATE jobs SET status='stopped',error=NULL,updated=? WHERE id=? AND status='running'",
                                   (time.time(), job['id']))
                    LOG.info('Stopped %s; original retained', Path(job['source']).name)
                except queue_control.WorkerShutdown:
                    with connect() as db:
                        db.execute("UPDATE jobs SET status='queued',error=NULL,updated=? WHERE id=? AND status='running'",
                                   (time.time(), job['id']))
                    return
                except Exception as exc:
                    with connect() as db:
                        db.execute("UPDATE jobs SET status='failed',error=?,updated=? WHERE id=?",
                                   (str(exc), time.time(), job['id']))
                    LOG.error('Failed %s: %s; available video retained', Path(job['source']).name, exc)
                finally:
                    JOB_CONTROL = None
        finally:
            JOB_CONTROL = None
            with connect() as db:
                queue_control.runtime(db, token, 'offline')
            LOG.removeHandler(handler)
            handler.close()


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
            failed = db.execute("SELECT id FROM jobs WHERE status='failed' ORDER BY created,id").fetchall()
            db.execute("UPDATE jobs SET status='queued',error=NULL,updated=? WHERE status='failed'", (time.time(),))
            for row in failed:
                queue_control.append_job(db, row[0])
    if args.status:
        with connect() as db:
            print(json.dumps({r['status']: r['n'] for r in db.execute('SELECT status,count(*) n FROM jobs GROUP BY status')}, ensure_ascii=False))
        return
    ensure_worker()


if __name__ == '__main__':
    main()
