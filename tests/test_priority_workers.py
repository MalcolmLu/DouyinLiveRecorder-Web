"""Run both production priority entry points against disposable real media."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'gui'))
import job_metrics
import library_worker as library
import queue_control
from shared import Paths, write_json
from services import DashboardService

FFMPEG, FFPROBE = shutil.which('ffmpeg'), shutil.which('ffprobe')
ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(FFMPEG and FFPROBE and os.name != 'nt', 'Requires FFmpeg/FFprobe and POSIX process suspension')
class PriorityWorkerMediaTests(unittest.TestCase):
    def wait(self, predicate, description, timeout=20):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(.03)
        self.fail('Timed out: ' + description)

    def exercise(self, historical, stop=False):
        with tempfile.TemporaryDirectory(prefix='dlr-priority-media-') as folder:
            root = Path(folder).resolve()
            config, videos, tools = root / 'config', root / 'videos', root / 'bin'
            videos.mkdir(); tools.mkdir()
            source = videos / 'selected.flv'
            subprocess.run([FFMPEG, '-nostdin', '-hide_banner', '-v', 'error', '-f', 'lavfi',
                            '-i', 'testsrc2=size=160x90:rate=10', '-f', 'lavfi', '-i', 'sine=sample_rate=48000',
                            '-t', '3', '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '1',
                            '-c:a', 'aac', str(source)], check=True, capture_output=True, timeout=30)
            old_source = videos / 'paused.flv'
            shutil.copyfile(source, old_source)
            for file in (source, old_source):
                os.utime(file, (time.time() - 600,) * 2)
            original = old_source.read_bytes(), old_source.stat().st_mtime_ns
            home = config / ('library/h265' if historical else 'h265')
            p = library.processor(config, videos, home)
            write_json(p.HOME / 'settings.json', {'enabled': True, 'encoder': 'libx265',
                       'preset': 'ultrafast', 'cpu_threads': 1, 'crf': 28})
            library.seed(p)
            with p.connect() as db:
                ids = {Path(row['source']).name: row['id'] for row in db.execute('SELECT * FROM jobs')}
                selected, old = ids[source.name], ids[old_source.name]
                db.execute("UPDATE jobs SET status='running' WHERE id=?", (old,))
                queue_control.runtime(db, 'parent', 'encoding', old)

            gate = None
            if historical:
                primary = library.processor(config, videos, config / 'h265')
                with primary.connect() as db:
                    queue_control.runtime(db, 'primary', 'idle')
                gate = library.PriorityGate(primary.DB)
                command = [sys.executable, str(ROOT / 'gui/library_worker.py'), '--config', str(config),
                           '--root', str(videos), '--priority-worker']
            else:
                command = [sys.executable, str(ROOT / 'h265/postprocess.py'), '--worker-priority']

            # Pace only the test encoder, so the suspension is observable even
            # on a fast machine. Verification still uses the real full decoder.
            wrapper = tools / 'ffmpeg'
            wrapper.write_text('#!' + sys.executable + '\nimport os,sys\na=sys.argv[1:]\n'
                               'if "-progress" in a and a[-1] != "-": a.insert(a.index("-i"),"-re")\n'
                               'os.execv(' + repr(FFMPEG) + ',[' + repr(FFMPEG) + ',*a])\n')
            wrapper.chmod(0o755)
            (tools / 'ffprobe').symlink_to(FFPROBE)
            counter, pid_file = root / 'counter', root / 'old-pid'
            counter_script = ('import os,time\nfrom pathlib import Path\n'
                              f'Path({str(pid_file)!r}).write_text(str(os.getpid()))\n'
                              f'p=Path({str(counter)!r})\nn=0\n'
                              'while True:\n n+=1;p.write_text(str(n));time.sleep(.03)\n')
            stopped = threading.Event()
            kwargs = dict(priority_command=command, priority_log=root / 'priority.log')
            control = (library.PriorityControl(p.DB, 'parent', old, stopped.is_set, gate=gate, **kwargs)
                       if historical else queue_control.JobControl(p.DB, 'parent', old, stopped.is_set, **kwargs))
            control.phase = 'encoding'
            environment = {'PATH': str(tools) + os.pathsep + os.environ['PATH'],
                           'RECORDER_CONFIG_DIR': str(config), 'RECORDER_DOWNLOADS_DIR': str(videos)}
            with patch.dict(os.environ, environment), ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(control.run, [sys.executable, '-c', counter_script],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    self.wait(lambda: pid_file.exists() and counter.exists(), 'old process ready')
                    old_pid = pid_file.read_text()
                    with p.connect() as db:
                        queue_control.start_now(db, selected, queue_control.snapshot(db)['order_revision'])

                    def priority_encoding():
                        with p.connect() as db:
                            state = queue_control.snapshot(db)
                        return state['priority_worker_active'] and state['phase'] == 'encoding'

                    self.wait(priority_encoding, 'production priority encoder')
                    with p.connect() as db:
                        state = queue_control.snapshot(db)
                        frozen_runtime = job_metrics.read(db, [old])[old]
                    self.assertEqual((state['current_job_id'], state['suspended_job_id']), (selected, old))
                    frozen_counter = counter.read_bytes()
                    time.sleep(.5)
                    self.assertEqual(counter.read_bytes(), frozen_counter)
                    with p.connect() as db:
                        self.assertEqual(job_metrics.read(db, [old])[old], frozen_runtime)
                        self.assertGreater(job_metrics.read(db, [selected])[selected], 0)
                    dashboard_paths = Paths(config / 'library' if historical else config, videos, root / 'state')
                    dashboard = DashboardService(dashboard_paths, None, None)
                    self.wait(lambda: dashboard.progress(state) is not None, 'priority progress file')
                    self.assertEqual(dashboard.progress(state)['job_id'], selected)

                    if stop:
                        with p.connect() as db:
                            live = queue_control.snapshot(db)
                            queue_control.request_control(db, 'stop', live['control_revision'], selected)
                        with self.assertRaises(queue_control.JobStopped):
                            future.result(timeout=15)
                        with p.connect() as db:
                            self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?', (selected,)).fetchone()[0], 'stopped')
                        self.assertTrue(source.is_file())
                        self.assertEqual((old_source.read_bytes(), old_source.stat().st_mtime_ns), original)
                        self.assertIsNone(control.priority_process)
                        self.assertFalse(list(videos.glob('.*.tmp*')))
                        return

                    def completed():
                        with p.connect() as db:
                            row = db.execute('SELECT * FROM jobs WHERE id=?', (selected,)).fetchone()
                        if row['status'] == 'failed':
                            self.fail(row['error'])
                        return row['status'] == 'done'

                    self.wait(completed, 'verified priority output')
                    self.wait(lambda: counter.read_bytes() != frozen_counter, 'same old process resumes')
                    self.assertEqual(pid_file.read_text(), old_pid)
                    with p.connect() as db:
                        self.assertIsNone(queue_control.snapshot(db)['priority_job_id'])
                        self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 2)
                        row = dict(db.execute('SELECT * FROM jobs WHERE id=?', (selected,)).fetchone())
                        self.assertGreater(job_metrics.read(db, [selected])[selected], 0)
                        if historical:
                            proof = json.loads(db.execute('SELECT verification FROM library_files WHERE id=?', (selected,)).fetchone()[0])
                            self.assertTrue(proof['full_output_decode'])
                    self.assertEqual(p.probe(row['output'])[1]['codec_name'], 'hevc')
                    self.assertEqual((old_source.read_bytes(), old_source.stat().st_mtime_ns), original)
                    self.assertFalse((home / 'priority-state/queue.sqlite3').exists())
                finally:
                    stopped.set()
                    try:
                        future.result(timeout=15)
                    except (queue_control.WorkerShutdown, queue_control.JobStopped):
                        pass
                    control.close()
                    if gate:
                        gate.close()

    def test_recorder_priority_preserves_old_process_and_publishes_verified_hevc(self):
        self.exercise(False)

    def test_historical_priority_shares_the_queue_and_resumes_old_process(self):
        self.exercise(True)

    def test_stop_reaps_priority_encoders_and_retains_both_originals(self):
        for historical in (False, True):
            with self.subTest(historical=historical):
                self.exercise(historical, stop=True)


if __name__ == '__main__':
    unittest.main()
