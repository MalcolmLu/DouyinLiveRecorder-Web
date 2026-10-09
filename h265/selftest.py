"""Real FFmpeg encoding and data-preservation tests; isolated temporary files only."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

selected_encoder = (sys.argv[sys.argv.index('--encoder')+1] if '--encoder' in sys.argv else
                    'hevc_qsv' if '--hardware' in sys.argv else 'libx265')
script = Path(__file__).with_name('postprocess.py')
spec = importlib.util.spec_from_file_location('h265_test_module', script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

with tempfile.TemporaryDirectory(prefix='h265-selftest-') as directory:
    root = Path(directory).resolve()
    module.HOME = root / 'state'
    module.DB = module.HOME / 'queue.sqlite3'
    module.ROOT = root / 'recordings'
    module.ROOT.mkdir()
    module.HOME.mkdir()
    (module.HOME / 'settings.json').write_text(json.dumps({
        'enabled': True, 'crf': 24, 'preset': 'veryfast', 'cpu_threads': 2,
        'copy_already_hevc': True,
        'encoder': selected_encoder}))
    source = module.ROOT / 'success.ts'
    subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
                    '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=15',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                    '-t', '5', '-c:v', 'libx264', '-preset', 'ultrafast',
                    '-c:a', 'aac', '-f', 'mpegts', str(source)], check=True)
    original = root / 'fixture.ts'
    shutil.copy2(source, original)
    identifier = module.enqueue(source, True)
    assert identifier == module.enqueue(source, True)
    with module.connect() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
        job = db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone()
    module.encode(job)
    output = source.with_suffix('.mp4')
    assert not source.exists() and output.is_file()
    duration, video, audio = module.probe(output)
    assert video['codec_name'] == 'hevc' and audio == 1 and duration >= 4.9
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(output),
                    '-f', 'null', '-'], check=True, stdout=subprocess.DEVNULL)
    print('PASS real HEVC+audio output, playback, delete only after validation, duplicate enqueue')

    bad = module.ROOT / 'corrupt.ts'
    bad.write_bytes(b'Not a valid recording')
    identifier = module.enqueue(bad, True)
    with module.connect() as db:
        job = db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone()
    try:
        module.encode(job)
        raise AssertionError('Corrupt source was accepted')
    except subprocess.CalledProcessError:
        assert bad.read_bytes() == b'Not a valid recording'
        assert not bad.with_suffix('.mp4').exists()
    print('PASS failed validation retains original')

    collision = module.ROOT / 'collision.ts'
    shutil.copy2(original, collision)
    destination = collision.with_suffix('.mp4')
    destination.write_bytes(b'Existing user file')
    identifier = module.enqueue(collision, True)
    with module.connect() as db:
        job = db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone()
    try:
        module.encode(job)
        raise AssertionError('Existing output was overwritten')
    except FileExistsError:
        assert collision.is_file() and destination.read_bytes() == b'Existing user file'
    print('PASS existing destination and source are protected')

    kept = module.ROOT / 'kept.ts'
    shutil.copy2(original, kept)
    identifier = module.enqueue(kept, False)
    with module.connect() as db:
        job = db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone()
    module.encode(job)
    assert kept.exists() and module.probe(kept.with_suffix('.mp4'))[1]['codec_name'] == 'hevc'
    print('PASS keep-original option')

    kept_mp4 = module.ROOT / 'kept-original.mp4'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-n', '-i', str(original),
                    '-c', 'copy', str(kept_mp4)], check=True)
    assert module.probe(kept_mp4)[1]['codec_name'] == 'h264'
    old_bytes = kept_mp4.read_bytes()
    old_mtime = kept_mp4.stat().st_mtime_ns
    identifier = module.enqueue(kept_mp4, False)
    with module.connect() as db:
        job = db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone()
    module.encode(job)
    assert kept_mp4.read_bytes() == old_bytes and kept_mp4.stat().st_mtime_ns == old_mtime
    assert module.probe(kept_mp4.with_name('kept-original.H265.mp4'))[1]['codec_name'] == 'hevc'
    progress = json.loads((module.HOME / 'current-job.json').read_text())
    assert progress['job_id'] == identifier and progress['duration'] > 0
    assert progress['encoder'] == (selected_encoder)
    assert 'progress=end' in (module.HOME / 'progress.txt').read_text()
    print('PASS MP4 original preserved with separate H265 output; real progress emitted')

    changed = module.ROOT / 'changed.ts'
    shutil.copy2(original, changed)
    identifier = module.enqueue(changed, True)
    with module.connect() as db:
        job = db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone()
    with changed.open('ab') as file:
        file.write(b'changed')
    try:
        module.encode(job)
        raise AssertionError('Changed source was accepted')
    except ValueError:
        assert changed.exists() and not changed.with_suffix('.mp4').exists()
    print('PASS changed source is retained')

    if '--hardware' in sys.argv:
        cfg_path = module.HOME / 'settings.json'
        cfg = json.loads(cfg_path.read_text())
        cfg['qsv_device'] = '/dev/dri/renderD999999'
        cfg_path.write_text(json.dumps(cfg))
        unavailable = module.ROOT / 'unavailable.ts'
        shutil.copy2(original, unavailable)
        identifier = module.enqueue(unavailable, True)
        with module.connect() as db:
            job = db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone()
        try:
            module.encode(job)
            raise AssertionError('Unavailable GPU was accepted')
        except ValueError:
            assert unavailable.is_file() and not unavailable.with_suffix('.mp4').exists()
        print('PASS unavailable QSV retains original; no software fallback')

print('ALL H265 TESTS PASSED')
