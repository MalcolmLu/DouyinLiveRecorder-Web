"""Relocate checksum-verified Homebrew FFmpeg bottles for a portable macOS app."""
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT=Path(__file__).resolve().parent
TOOLS=ROOT/"tools"
ARCH="arm64" if platform.machine()=="arm64" else "x86_64"
TAG="arm64_sequoia" if ARCH=="arm64" else "sequoia"
metadata={}
index={}
provenance={}


def fetch(url,headers=None):
    return urllib.request.urlopen(urllib.request.Request(url,headers=headers or {}),timeout=120).read()


with tempfile.TemporaryDirectory(prefix="dlr-homebrew-tools-") as directory:
    staging=Path(directory)
    def install(name):
        if name in metadata:return
        info=json.loads(fetch("https://formulae.brew.sh/api/formula/"+name+".json"))
        metadata[name]=info
        files=info["bottle"]["stable"]["files"]
        tag=TAG if TAG in files else "all"
        if tag not in files:raise RuntimeError("No "+TAG+" bottle for "+name)
        bottle=files[tag]
        repo="homebrew/core/"+name.replace("@","/")
        token=json.loads(fetch("https://ghcr.io/token?service=ghcr.io&scope=repository:"+repo+":pull"))["token"]
        data=fetch(bottle["url"],{"Authorization":"Bearer "+token})
        if hashlib.sha256(data).hexdigest()!=bottle["sha256"]:raise RuntimeError("Bottle checksum mismatch: "+name)
        archive=staging/(name.replace("/","-")+".tar.gz");archive.write_bytes(data)
        dest=staging/name;dest.mkdir(parents=True)
        with tarfile.open(archive) as bundle:bundle.extractall(dest,filter="data")
        for path in dest.rglob("*"):
            if path.is_file():index.setdefault(path.name,[]).append(path)
        provenance[name]={"version":info["versions"]["stable"],"bottle":bottle,
                          "source":info["urls"].get("stable"),"license":info["license"],
                          "recipe":"https://github.com/Homebrew/homebrew-core/blob/"+info["tap_git_head"]+
                            "/Formula/"+name[0]+"/"+name+".rb"}
        for dep in info["dependencies"]:install(dep)
    install("ffmpeg")
    TOOLS.mkdir(exist_ok=True);lib=TOOLS/"lib";lib.mkdir(exist_ok=True)
    copied={}
    def relocate(source,target):
        target=Path(target)
        if target.name in copied:return
        copied[target.name]=target
        shutil.copy2(source,target);target.chmod(0o755)
        dependencies=subprocess.check_output(["otool","-L",str(target)],text=True).splitlines()[1:]
        for line in dependencies:
            old=line.strip().split(" (",1)[0]
            if old.startswith(("/System/","/usr/lib/")):continue
            name=Path(old).name
            if name==target.name:continue
            candidates=index.get(name,[])
            if not candidates:raise RuntimeError("Missing bottle dependency: "+old)
            destination=lib/name
            relocate(candidates[0],destination)
            relative="@loader_path/"+("lib/" if target.parent==TOOLS else "")+name
            subprocess.run(["install_name_tool","-change",old,relative,str(target)],check=True)
        if target.suffix==".dylib":
            subprocess.run(["install_name_tool","-id","@rpath/"+target.name,str(target)],check=True)
        subprocess.run(["codesign","--force","--sign","-",str(target)],check=True,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    for binary in ("ffmpeg","ffprobe"):relocate(index[binary][0],TOOLS/binary)
    notices=TOOLS/"licenses";notices.mkdir(exist_ok=True)
    for formula in metadata:
        for p in (staging/formula).rglob("*"):
            if p.is_file() and p.name.startswith(("COPYING","LICENSE")):
                dest=notices/(formula.replace("/","-")+"-"+p.name)
                if not dest.exists():shutil.copy2(p,dest)
    for binary in ("ffmpeg","ffprobe"):
        result=subprocess.run([str(TOOLS/binary),"-version"],capture_output=True,text=True,check=True)
        if "--enable-nonfree" in result.stdout:raise RuntimeError("Non-redistributable FFmpeg build rejected")
    (TOOLS/"ffmpeg-provenance.json").write_text(json.dumps(provenance,indent=2))
print("Portable Homebrew FFmpeg and ffprobe prepared for "+ARCH)
