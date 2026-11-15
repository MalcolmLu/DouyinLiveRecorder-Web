"""Exercise the ordinary live-worker entry point with an isolated queue."""
from pathlib import Path
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parent.parent


@unittest.skipIf(os.name == 'nt', 'The NAS worker uses POSIX process and file locks')
class LivePrimaryWorkerTests(unittest.TestCase):
    def test_primary_job_uses_live_control_and_acquires_shared_encoder_lock(self):
        # Run the real worker loop in a separate process.  The tiny encoder is
        # synthetic so this also exercises the entry point without a GPU.
        driver = r'''
from pathlib import Path
import importlib.util, os, sys, time
base, root = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
spec = importlib.util.spec_from_file_location('isolated_processor', base/'h265/postprocess.py')
p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
p.HOME = root/'config/h265'; p.DB = p.HOME/'queue.sqlite3'; p.ROOT = root/'videos'
p.ROOT.mkdir(parents=True); source = p.ROOT/'fixture.flv'; source.write_bytes(b'fixture')
identifier = p.enqueue(source, delete_source=False)
def encode(job):
    assert isinstance(p.JOB_CONTROL, p.LiveControl)
    p.checkpoint('encoding')
    lock = p.HOME.parent/'library/h265/shared-encoder.lock'
    child = ('import fcntl,sys\n'
             'f=open(sys.argv[1],"a+")\n'
             'try: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)\n'
             'except BlockingIOError: pass\n'
             'else: raise SystemExit("encoder lock was not held")\n')
    p.run_process([sys.executable,'-c',child,str(lock)], check=True,
                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    p.checkpoint('verifying')
    with p.connect() as db:
        db.execute("UPDATE jobs SET status='done',updated=? WHERE id=?", (time.time(),identifier))
p.encode = encode
import subprocess
p.worker()
'''
        with tempfile.TemporaryDirectory(prefix='dlr-live-primary-') as folder:
            database = Path(folder)/'config/h265/queue.sqlite3'
            process = subprocess.Popen([sys.executable, '-c', driver, str(ROOT), folder],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
            state = None
            try:
                until = time.monotonic()+20
                while time.monotonic()<until:
                    if process.poll() is not None:
                        break
                    if database.exists():
                        try:
                            with sqlite3.connect(database, timeout=.2) as db:
                                state = db.execute('SELECT status,error FROM jobs LIMIT 1').fetchone()
                            if state and state[0] in ('done','failed'):
                                break
                        except sqlite3.Error:
                            pass
                    time.sleep(.05)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    out, err = process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    out, err = process.communicate(timeout=5)
            self.assertEqual(state, ('done', None), (state, out, err))
            self.assertEqual(process.returncode, 0, (out, err))


if __name__ == '__main__':
    unittest.main()
