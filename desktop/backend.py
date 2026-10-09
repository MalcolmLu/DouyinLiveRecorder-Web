"""Packaged backend entry point. All writable files live in the user profile."""
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import runpy
import sys
import threading
import time

ROOT=Path(getattr(sys,"_MEIPASS",Path(__file__).resolve().parent.parent))
sys.path[:0]=[str(ROOT),str(ROOT/"gui"),str(ROOT/"desktop"),str(ROOT/"scripts")]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--role",choices=("backend","recorder","postprocess","selftest"),default="backend")
    parser.add_argument("--data-dir")
    parser.add_argument("--tools-dir")
    args,remaining=parser.parse_known_args()
    if args.role=="recorder":
        sys.argv=[str(ROOT/"main.py")]
        runpy.run_path(str(ROOT/"main.py"),run_name="__main__")
        return
    if args.role in ("postprocess","selftest"):
        target=ROOT/"h265"/("selftest.py" if args.role=="selftest" else "postprocess.py")
        sys.argv=[str(target),*remaining]
        runpy.run_path(str(target),run_name="__main__")
        return
    if not args.data_dir:
        parser.error("--data-dir is required")
    from service import DesktopService
    from shared import Paths
    from setup import initialize
    from app import create_app
    from waitress import create_server
    data=Path(args.data_dir).resolve();config=data/"config"
    os.environ.update(DLR_DESKTOP="1",DLR_DESKTOP_WORKER_MANAGED="1",RECORDER_CONFIG_DIR=str(config),
                      RECORDER_DOWNLOADS_DIR=str(data/"recordings"),RECORDER_LOG_DIR=str(data/"logs"),
                      RECORDER_BACKUP_DIR=str(data/"backup_config"))
    paths=Paths.from_env();initialize(paths)
    import certifi
    os.environ["SSL_CERT_FILE"]=certifi.where()
    service=DesktopService(paths,data,Path(args.tools_dir).resolve() if args.tools_dir else None)
    paths=service.paths
    application=create_app(paths,desktop=service)
    server=create_server(application,host="127.0.0.1",port=0,threads=4,expose_tracebacks=False)
    host="127.0.0.1:"+str(server.effective_port)
    application.config["DESKTOP_ALLOWED_HOST"]=host
    auth=application.extensions["recorder_web"]["auth_repo"]
    token,csrf=auth.create_session()
    hashed=hashlib.sha256(token.encode()).hexdigest()
    # stdout is a private pipe to the Electron main process, never a log file.
    print(json.dumps({"event":"ready","url":"http://"+host,"cookie":token}),flush=True)
    def renew():
        while not service.closed.wait(3600):
            with auth.connect() as db:
                db.execute("UPDATE sessions SET expires=? WHERE hash=?",(time.time()+86400,hashed))
    threading.Thread(target=renew,daemon=True).start()
    def watch_parent():
        # The shell owns stdin. EOF means it crashed or was terminated.
        for line in sys.stdin:
            if line.strip()=="shutdown":
                break
        service.shutdown()
        server.close()
        auth.delete_session(hashed)
        os._exit(0)
    threading.Thread(target=watch_parent,daemon=True).start()
    if service.preferences()["auto_start"]:
        from shared import AppError
        try:service.start()
        except AppError:pass  # The first-run settings page can repair missing tools.
    service.start_worker()
    try:
        server.run()
    except OSError:
        if not service.closed.is_set():raise
    finally:
        service.shutdown()
        auth.delete_session(hashed)


if __name__=="__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
