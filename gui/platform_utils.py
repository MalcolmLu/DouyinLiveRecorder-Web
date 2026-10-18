"""Small OS adapters shared by the NAS service and desktop recorder."""
import os
from pathlib import Path
import sys
import time

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def acquire_lock(stream, blocking=True):
    if os.name != "nt":
        fcntl.flock(stream.fileno(),fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    stream.seek(0,os.SEEK_END)
    if not stream.tell():
        stream.write("0")
        stream.flush()
    while True:
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            return
        except OSError as error:
            if not blocking:
                raise BlockingIOError("File is locked") from error
            time.sleep(.05)


def release_lock(stream):
    if os.name == "nt":
        stream.seek(0)
        msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
    else:
        fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def sync_directory(directory):
    if os.name == "nt":
        return  # FlushFileBuffers on files is handled by os.fsync.
    fd=os.open(directory,os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def child_command(role, *args):
    if getattr(sys,"frozen",False):
        return [sys.executable,"--role",role,*args]
    root=Path(__file__).resolve().parent.parent
    return [sys.executable,str(root/"desktop/backend.py"),"--role",role,*args]


def process_options():
    return {"creationflags":0x08000000} if os.name=="nt" else {}


def source_root():
    return Path(getattr(sys,"_MEIPASS",Path(__file__).resolve().parent.parent))


def recording_roots(config, current):
    from shared import read_json
    saved=read_json(Path(config)/"gui/state/desktop-settings.json",{})
    return list(dict.fromkeys([Path(current).resolve(),*[Path(p).resolve() for p in saved.get("recording_roots",[])]]))


def recording_relative(source, roots):
    for root in roots:
        try:
            return source.relative_to(root)
        except ValueError:
            pass
    raise ValueError("Recording is outside the configured recording folders")
