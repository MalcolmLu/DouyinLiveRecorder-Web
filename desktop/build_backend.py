"""Build a self-contained backend on the target OS with PyInstaller."""
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parent.parent
desktop=ROOT/"desktop"
command=[sys.executable,"-m","PyInstaller","--noconfirm","--clean","--onedir","--python-option","X utf8",
         "--name","dlr-backend","--distpath",str(desktop/"backend-dist"),
         "--workpath",str(desktop/"build"),"--specpath",str(desktop/"build"),
         "--paths",str(ROOT),"--paths",str(ROOT/"gui"),"--paths",str(desktop),
         "--paths",str(ROOT/"scripts"),"--hidden-import","main","--hidden-import","app",
         "--hidden-import","service","--hidden-import","setup","--hidden-import","recorder_bridge",
         "--hidden-import","youtube_source","--hidden-import","proxy_tunnel","--hidden-import","platform_utils",
         "--hidden-import","h265.postprocess","--hidden-import","logging.handlers",
         "--collect-submodules","yt_dlp"]
for source,destination in (("main.py","."),("defaults","defaults"),("h265","h265"),
                           ("i18n","i18n"),("src/javascript","src/javascript"),("gui/static","gui/static")):
    command.extend(["--add-data",str(ROOT/source)+":"+destination])
command.append(str(desktop/"backend.py"))
subprocess.run(command,cwd=ROOT,check=True)
